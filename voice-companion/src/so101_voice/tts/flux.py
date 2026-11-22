"""Deepgram Flux TTS v2: one streaming speech turn per reply, Interrupt on barge-in.

Audio is accepted only between SpeechStarted and SpeechMetadata for our current generation.
Flushed is NOT completion. Renewal happens between turns before the one-hour provider limit.
No interrupted or disconnected text is replayed. See docs/SOL_FLUX.md for protocol sources.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.asyncio.client import ClientConnection

from so101_voice.cost import CostLedger

log = logging.getLogger(__name__)
Connector = Callable[..., Any]


class TTSError(Exception):
    pass


@dataclass(frozen=True)
class SpeechTurn:
    gen: int
    index: int = 0
    final: bool = True


class FluxTTS:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        sample_rate: int,
        on_audio: Callable[[bytes, int, int], None],
        on_group_done: Callable[[SpeechTurn], None],
        on_notice: Callable[[str, str], None] | None = None,
        on_lost: Callable[[str], None] | None = None,
        on_interrupted: Callable[[int, str], None] | None = None,
        played_bytes: Callable[[], int] | None = None,
        playback_latency_s: float = 0.0,
        ledger: CostLedger | None = None,
        connect_timeout_s: float = 10.0,
        interrupt_timeout_s: float = 1.0,
        max_connection_s: float = 55 * 60,
        expressivity: int = 0,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        params = {
            "model": model,
            "encoding": "linear16",
            "sample_rate": sample_rate,
            "expressivity": expressivity,
        }
        self.url = f"{url}?{urlencode(params)}"
        self.model, self.sample_rate = model, sample_rate
        self._api_key = api_key
        self._on_audio, self._on_group_done = on_audio, on_group_done
        self._on_notice = on_notice or (lambda level, text: None)
        self._on_lost = on_lost or (lambda reason: None)
        self._on_interrupted = on_interrupted or (lambda gen, text: None)
        self._played_bytes = played_bytes
        self._playback_latency_s = playback_latency_s
        self.ledger = ledger
        self.connect_timeout_s, self.interrupt_timeout_s = connect_timeout_s, interrupt_timeout_s
        self.max_connection_s = max_connection_s
        self._connector, self._clock = connector or websockets.connect, clock
        self._lock = asyncio.Lock()
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._interrupt_task: asyncio.Task[None] | None = None
        self._connected_at = 0.0
        self._played_origin = 0
        self._last_offset = -1.0
        self._gen: int | None = None
        self._turn: SpeechTurn | None = None
        self._speech_id: str | None = None
        self._has_text = False
        self._flushed = False
        self._dropping = False
        self._interrupted = asyncio.Event()
        self._interrupted.set()
        self._cancel_gen: int | None = None
        self._offset_sent = False
        self._reports: dict[str, tuple[int, int]] = {}
        self._billing_session = ""
        self._pcm_tail = b""
        self.request_id = ""
        self.connections = 0
        self.discarded_audio_bytes = 0
        self.warnings: list[str] = []
        self.last_interruption: dict[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def begin(self, gen: int) -> None:
        self._gen = gen

    async def connect(self) -> None:
        async with self._lock:
            await self._ensure_connected()

    async def wait_interrupted(self) -> None:
        """Also used before inference so a valid heard-text report can update history."""
        if self._interrupted.is_set():
            return
        try:
            await asyncio.wait_for(self._interrupted.wait(), self.interrupt_timeout_s)
        except TimeoutError:
            async with self._lock:
                if not self._interrupted.is_set():
                    log.warning("TTS Interrupt unacknowledged; replacing socket without replay")
                    await self._drop_connection("interrupt timeout")

    async def speak(self, gen: int, text: str) -> bool:
        if not text:
            return True
        await self.wait_interrupted()  # never hold the send lock while awaiting Interrupt
        async with self._lock:
            if gen != self._gen:
                return False
            if self._ws is not None and self._turn is None:
                if self._clock() - self._connected_at >= self.max_connection_s:
                    await self._drop_connection("scheduled renewal", graceful=True)
            await self._ensure_connected()
            if gen != self._gen:
                return False
            if self._turn is None:
                self._turn = SpeechTurn(gen)
                self._speech_id = None
                self._has_text = self._flushed = False
            if self._turn.gen != gen or self._flushed:
                raise TTSError("previous TTS turn has not completed")
            # TextChunker emits stripped phrases; v2 never inserts separators itself.
            part = (" " if self._has_text and not text[0].isspace() else "") + text
            self._has_text = True
            # Account before send: a failed/cancelled send may already have reached the provider.
            if self.ledger is not None:
                self.ledger.tts_sent(len(part))
            await self._send({"type": "Speak", "text": part})
            return True

    async def flush(self, gen: int, index: int = 0, *, final: bool = True) -> bool:
        if not final:
            raise TTSError("Flux Flush ends a turn; only the final reply boundary may flush")
        await self.wait_interrupted()
        async with self._lock:
            if gen != self._gen:
                return False
            if self._turn is None:  # empty response: Flush would only cause a warning
                self._on_group_done(SpeechTurn(gen))
                return True
            if not self._flushed:
                self._flushed = True
                await self._send({"type": "Flush"})
            return True

    def cancel(self, gen: int | None = None) -> None:
        if gen is not None and gen != self._gen:
            return
        cancelled_gen, self._gen = self._gen, None
        if self._dropping or self._ws is None:
            return
        self._cancel_gen = cancelled_gen
        self._dropping = True
        self._pcm_tail = b""
        self._interrupted.clear()
        offset = None
        if self._played_bytes is not None:
            # The v2 wire schema requires an integer, including whole-valued
            # offsets. Truncate nonnegative milliseconds to avoid overstating playback.
            offset = max(
                0,
                int(
                    (self._played_bytes() - self._played_origin) * 500 / self.sample_rate
                    - self._playback_latency_s * 1000
                ),
            )
        self._interrupt_task = asyncio.create_task(
            self._send_interrupt(self._ws, offset), name="tts-interrupt"
        )

    async def _send_interrupt(self, socket: ClientConnection, offset: int | None) -> None:
        async with self._lock:
            if self._ws is not socket:
                return
            if self._turn is None:
                # Synthesis ended but local playback was interrupted. The server no
                # longer has an active turn to align; don't invent a heard-text split.
                await self._drop_connection("buffered playback interrupted")
                return
            # An invalid/non-advancing offset is ignored by Flux. Replace the socket
            # instead of inventing playback progress or allowing stale audio through.
            if offset is not None and offset <= self._last_offset:
                await self._drop_connection("non-advancing playback offset")
                return
            self._offset_sent = offset is not None
            message: dict[str, Any] = {"type": "Interrupt"}
            if offset is not None:
                message["playback_offset"] = {"type": "time_ms", "value": offset}
                self._last_offset = offset
            with contextlib.suppress(TTSError):
                await self._send(message)

    async def aclose(self) -> None:
        self._gen = None
        task = self._interrupt_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        async with self._lock:
            await self._drop_connection("closed", graceful=self._turn is None)

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        try:
            ws = await asyncio.wait_for(
                self._connector(
                    self.url,
                    additional_headers={"Authorization": f"Token {self._api_key}"},
                    max_size=2**22,
                    open_timeout=self.connect_timeout_s,
                    ping_interval=20,
                    ping_timeout=20,
                ),
                self.connect_timeout_s + 1,
            )
        except websockets.InvalidStatus as e:
            raise TTSError(f"Deepgram TTS rejected the connection (HTTP {e.response.status_code})") from e
        except (OSError, TimeoutError, websockets.InvalidHandshake) as e:
            raise TTSError(f"cannot reach Deepgram TTS: {type(e).__name__}: {e}") from e
        self._ws = ws
        self._connected_at = self._clock()
        self._played_origin = self._played_bytes() if self._played_bytes else 0
        self._last_offset = -1
        self._turn = None
        self._speech_id = None
        self._pcm_tail = b""
        self._dropping = False
        self._interrupted.set()
        self._reports = {}
        self.connections += 1
        self._billing_session = uuid.uuid4().hex
        self._reader = asyncio.create_task(self._read(ws), name="tts-reader")
        log.info("Deepgram Flux TTS connected (%s), connection %s", self.model, self.connections)

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            raise TTSError("TTS socket is not connected")
        try:
            await self._ws.send(json.dumps(message))
        except websockets.ConnectionClosed as e:
            await self._drop_connection("send failed")
            raise TTSError(f"TTS connection lost: {e}") from e

    async def _read(self, ws: ClientConnection) -> None:
        reason = "closed by server"
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    self._audio(raw)
                else:
                    self._message(raw)
        except websockets.ConnectionClosed as e:
            reason = f"connection lost: {e}"
        except Exception as e:
            log.exception("TTS reader failed")
            reason = f"TTS reader error: {e}"
        finally:
            if self._ws is ws:
                speaking = self._turn is not None and not self._dropping
                self._ws = None
                self._turn = None
                self._speech_id = None
                self._pcm_tail = b""
                self._dropping = False
                self._interrupted.set()
                if speaking:
                    self._on_lost(reason)

    def _audio(self, pcm: bytes) -> None:
        turn = self._turn
        if self._dropping or turn is None or turn.gen != self._gen or self._speech_id is None:
            self.discarded_audio_bytes += len(pcm)
            return
        pcm = self._pcm_tail + pcm
        n = len(pcm) - len(pcm) % 2
        self._pcm_tail = pcm[n:]
        if n:
            self._on_audio(pcm[:n], turn.gen, 0)

    def _report(self, message: dict[str, Any]) -> None:
        sid = str(message.get("speech_id") or "")
        if sid and "input_character_count" in message and "billable_character_count" in message:
            self._reports[sid] = (
                int(message["input_character_count"]),
                int(message["billable_character_count"]),
            )
            if self.ledger is not None:
                self.ledger.tts_report(
                    self._billing_session,
                    sum(x[0] for x in self._reports.values()),
                    sum(x[1] for x in self._reports.values()),
                )

    def _message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("unparsable TTS message")
            return
        kind = message.get("type")
        if kind == "Connected":
            self.request_id = str(message.get("request_id", ""))
        elif kind == "SpeechStarted":
            if not self._dropping and self._turn is not None:
                self._speech_id = str(message["speech_id"])
        elif kind == "SpeechMetadata":
            self._report(message)
            if self._dropping or message.get("speech_id") != self._speech_id:
                return
            turn, self._turn = self._turn, None
            self._speech_id = None
            self._pcm_tail = b""
            if turn is not None and turn.gen == self._gen:
                self._on_group_done(turn)
        elif kind == "SpeechInterrupted":
            self._report(message.get("metadata") or {})
            if not self._dropping:
                return
            self.last_interruption = message
            if self._cancel_gen is not None and self._offset_sent:
                self._on_interrupted(self._cancel_gen, str(message.get("text_spoken") or ""))
            self._turn = None
            self._speech_id = None
            self._dropping = False
            self._interrupted.set()
        elif kind == "SessionMetadata":
            if self.ledger is not None:
                self.ledger.tts_report(
                    self._billing_session,
                    int(message["total_input_character_count"]),
                    int(message["total_billable_character_count"]),
                )
        elif kind in ("Warning", "ConfigureFailure", "Error"):
            text = f"TTS {kind} {message.get('code', '')}: {message.get('description', '')}"
            self.warnings.append(text)
            self._on_notice("error" if kind == "Error" else "warning", text)
            log.warning("%s", text)
            # Warnings about ignored Interrupts cannot open the stale-audio gate.
            # wait_interrupted will reconnect if no acknowledgement follows.
            if kind == "Error":
                socket = self._ws
                self._dropping = True
                self._interrupted.clear()
                self._on_lost(text)
                if socket is not None:
                    self._interrupt_task = asyncio.create_task(
                        self._retire_failed_socket(socket), name="tts-error-close"
                    )
        # Flushed is only a flush acknowledgement; completion is SpeechMetadata.

    async def _retire_failed_socket(self, socket: ClientConnection) -> None:
        async with self._lock:
            if self._ws is socket:
                await self._drop_connection("provider error")

    async def _drop_connection(self, reason: str, *, graceful: bool = False) -> None:
        ws = self._ws
        reader = self._reader
        self._dropping = True
        if ws is not None:
            if graceful:
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps({"type": "Close"}))
                    await asyncio.wait_for(ws.wait_closed(), 1.0)
            self._ws = None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 2.0)
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        self._reader = None
        self._turn = None
        self._speech_id = None
        self._pcm_tail = b""
        self._dropping = False
        self._interrupted.set()
        log.info("TTS connection dropped: %s", reason)

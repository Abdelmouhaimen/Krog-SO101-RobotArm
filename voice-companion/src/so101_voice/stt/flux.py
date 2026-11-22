"""Deepgram Flux streaming speech recognition (``wss://api.deepgram.com/v2/listen``).

Protocol, as verified against the docs on 2026-11-08 (see ``docs/ARCHITECTURE.md`` §1):

* **Connect:** ``?model=flux-general-en&encoding=linear16&sample_rate=16000`` with optional
  ``eot_threshold`` (0.5–1.0), ``eager_eot_threshold`` (0.3–0.9, enables EagerEndOfTurn), ``eot_timeout_ms``
  (500–60000) and repeated ``keyterm`` (a paid add-on). Auth is ``Authorization: Token <key>``.
* **Audio:** binary frames; **80 ms chunks are strongly recommended** (2560 bytes at 16 kHz).
* **Client messages:** ``CloseStream``, ``ForceEndTurn`` and ``Configure``. There is **no KeepAlive**
  on v2, and an unknown message closes the stream with ``UNPARSABLE_CLIENT_MESSAGE``.
* **Server messages:** ``Connected``, ``TurnInfo`` (``event`` = StartOfTurn / Update / EagerEndOfTurn /
  TurnResumed / EndOfTurn, ``turn_index``, ``transcript``, ``audio_window_*``, ``end_of_turn_confidence``,
  ``trigger`` on EndOfTurn), ``ConfigureSuccess``, ``ConfigureFailure``, ``Warning`` and ``Error``
  (fatal; the server closes).

This client: one connection at a time, chunked sending, metering of the audio actually sent (the
billing basis), reconnection with backoff while the stream is wanted, and an EndOfTurn latency
measured from when the turn's last audio was sent. With no eager threshold configured,
EagerEndOfTurn never occurs, so nothing speculative reaches the LLM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.asyncio.client import ClientConnection

from so101_voice.cost import CostLedger

log = logging.getLogger(__name__)

SUPPORTED_SAMPLE_RATES = (8000, 16000, 24000, 44100, 48000)
Connector = Callable[..., Any]


class STTConnectError(Exception):
    def __init__(self, message: str, *, fatal: bool) -> None:
        super().__init__(message)
        self.fatal = fatal


@dataclass(frozen=True)
class TurnEvent:
    event: str  # StartOfTurn | Update | EagerEndOfTurn | TurnResumed | EndOfTurn
    turn_index: int
    transcript: str
    end_of_turn_confidence: float
    audio_window_end: float
    connection: int  # turn indexes restart on every connection
    trigger: str | None = None  # EndOfTurn only: model | manual | timeout
    latency_s: float | None = None  # EndOfTurn: receipt time - time the turn's last audio was sent


TurnHandler = Callable[[TurnEvent], None]
StateHandler = Callable[[str, str], None]  # (state, detail): connected | disconnected | failed


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class FluxSTT:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        sample_rate: int,
        on_turn: TurnHandler,
        on_state: StateHandler | None = None,
        eot_threshold: float | None = None,
        eager_eot_threshold: float | None = None,
        eot_timeout_ms: int | None = None,
        keyterms: list[str] | None = None,
        chunk_ms: int = 80,
        ledger: CostLedger | None = None,
        connect_timeout_s: float = 10.0,
        reconnect_initial_s: float = 1.0,
        reconnect_max_s: float = 15.0,
        reconnect_max_attempts: int = 8,
        backlog_s: float = 2.0,
        connector: Connector | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(f"Flux does not accept sample_rate={sample_rate}")
        params: list[tuple[str, str]] = [
            ("model", model),
            ("encoding", "linear16"),
            ("sample_rate", str(sample_rate)),
        ]
        if eot_threshold is not None:
            params.append(("eot_threshold", str(eot_threshold)))
        if eager_eot_threshold is not None:
            params.append(("eager_eot_threshold", str(eager_eot_threshold)))
        if eot_timeout_ms is not None:
            params.append(("eot_timeout_ms", str(eot_timeout_ms)))
        params += [("keyterm", k) for k in keyterms or []]
        self.url = f"{url}?{urlencode(params)}"
        self._api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.chunk_bytes = int(sample_rate * chunk_ms / 1000) * 2
        self._on_turn = on_turn
        self._on_state = on_state or (lambda s, d: None)
        self.ledger = ledger
        self.connect_timeout_s = connect_timeout_s
        self.reconnect_initial_s = reconnect_initial_s
        self.reconnect_max_s = reconnect_max_s
        self.reconnect_max_attempts = reconnect_max_attempts
        self._connector = connector or websockets.connect
        self._clock = clock

        self._pending = bytearray()
        self._queue: deque[bytes] = deque(maxlen=max(1, int(backlog_s * 1000 / chunk_ms)))
        self._have_audio = asyncio.Event()
        self._want = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._sent_s = 0.0  # audio seconds sent on the current connection
        self._sent_marks: deque[tuple[float, float]] = deque(maxlen=2000)  # (audio s, monotonic t)
        self.connection = 0
        self.request_id = ""
        self.failed: str | None = None
        self.dropped_chunks = 0
        self.sent_bytes = 0

    # ------------------------------------------------------------------ lifecycle
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._supervise(), name="flux-stt")

    def want(self, on: bool) -> None:
        """Keep a stream open (True) or close it gracefully when idle (False)."""
        if on:
            self.failed = None
            self._want.set()
        else:
            self._want.clear()
            self._have_audio.set()  # wake the sender so it can close

    async def wait_connected(self, timeout: float) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout)
        return self.connected

    async def aclose(self) -> None:
        self._want.clear()
        task, self._task = self._task, None
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send(json.dumps({"type": "CloseStream"})), 1.0)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close_ws()

    # ------------------------------------------------------------------ audio in
    def send_audio(self, pcm: bytes) -> None:
        """Queue microphone PCM (any length); it is sent in ``chunk_ms`` frames while connected."""
        if len(pcm) % 2:
            raise ValueError("linear16 audio must have an even number of bytes")
        self._pending.extend(pcm)
        while len(self._pending) >= self.chunk_bytes:
            if len(self._queue) == self._queue.maxlen:
                self.dropped_chunks += 1  # network stalled: keep latency bounded, drop the oldest
            self._queue.append(bytes(self._pending[: self.chunk_bytes]))
            del self._pending[: self.chunk_bytes]
        if self._queue:
            self._have_audio.set()

    def flush_partial(self) -> None:
        """Send what is left (< one chunk) at the end of an utterance."""
        if self._pending:
            self._queue.append(bytes(self._pending))
            self._pending.clear()
            self._have_audio.set()

    def drop_backlog(self) -> None:
        self._queue.clear()
        self._pending.clear()

    async def force_end_turn(self) -> None:
        await self._send_text({"type": "ForceEndTurn"})

    # ------------------------------------------------------------------ internals
    async def _send_text(self, msg: dict[str, Any]) -> None:
        ws = self._ws
        if ws is not None and self.connected:
            with contextlib.suppress(websockets.ConnectionClosed):
                await ws.send(json.dumps(msg))

    async def _supervise(self) -> None:
        delay, failures = self.reconnect_initial_s, 0
        while True:
            await self._want.wait()
            try:
                await self._connect()
            except STTConnectError as e:
                failures += 1
                if e.fatal or (self.reconnect_max_attempts and failures >= self.reconnect_max_attempts):
                    self.failed = str(e)
                    self._want.clear()
                    self._on_state("failed", str(e))
                    continue
                self._on_state("disconnected", f"{e}; retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_s)
                continue
            reason = await self._serve()
            await self._close_ws()
            self._on_state("disconnected", reason)
            if reason.startswith("Deepgram STT error"):
                failures += 1  # the server rejected something: back off instead of spinning
                if self.reconnect_max_attempts and failures >= self.reconnect_max_attempts:
                    self.failed = reason
                    self._want.clear()
                    self._on_state("failed", reason)
                    continue
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.reconnect_max_s)
            else:
                delay, failures = self.reconnect_initial_s, 0

    async def _connect(self) -> None:
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
            status = e.response.status_code
            raise STTConnectError(
                f"Deepgram STT rejected the connection (HTTP {status})"
                + (": check DEEPGRAM_API_KEY" if status in (401, 403) else ""),
                fatal=status in (400, 401, 402, 403, 404),
            ) from e
        except (OSError, TimeoutError, websockets.InvalidHandshake) as e:
            raise STTConnectError(f"cannot reach Deepgram STT: {type(e).__name__}: {e}", fatal=False) from e
        self._ws = ws
        self.connection += 1
        self._sent_s = 0.0
        self._sent_marks.clear()
        self._connected.set()
        if self.ledger is not None:
            self.ledger.stt_connected()
        self._on_state("connected", "")
        log.info("Flux STT connected (%s, connection %d)", self.model, self.connection)

    async def _serve(self) -> str:
        assert self._ws is not None
        ws = self._ws
        sender = asyncio.get_running_loop().create_task(self._sender(ws), name="flux-sender")
        try:
            async for raw in ws:
                if isinstance(raw, str):
                    fatal = self._message(raw)
                    if fatal:
                        return fatal
            return f"closed by server (code={ws.close_code})"
        except websockets.ConnectionClosed as e:
            return f"connection lost: {e}"
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    async def _sender(self, ws: ClientConnection) -> None:
        bytes_per_s = self.sample_rate * 2
        while True:
            await self._have_audio.wait()
            self._have_audio.clear()
            while self._queue:
                chunk = self._queue.popleft()
                await ws.send(chunk)
                secs = len(chunk) / bytes_per_s
                self._sent_s += secs
                self.sent_bytes += len(chunk)
                self._sent_marks.append((self._sent_s, self._clock()))
                if self.ledger is not None:
                    self.ledger.stt_audio(secs)
            if not self._want.is_set():
                log.info("Flux STT idle: closing stream")
                await ws.send(json.dumps({"type": "CloseStream"}))
                return

    def _message(self, raw: str) -> str | None:
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("unparsable STT message: %.200s", raw)
            return None
        kind = m.get("type")
        if kind == "TurnInfo":
            ev = str(m.get("event", ""))
            end = _f(m.get("audio_window_end"))
            latency = None
            if ev == "EndOfTurn":
                sent_t = next((t for s, t in self._sent_marks if s >= end - 1e-6), None)
                latency = None if sent_t is None else round(self._clock() - sent_t, 3)
            self._on_turn(
                TurnEvent(
                    event=ev,
                    turn_index=int(_f(m.get("turn_index"), -1)),
                    transcript=str(m.get("transcript") or ""),
                    end_of_turn_confidence=_f(m.get("end_of_turn_confidence")),
                    audio_window_end=end,
                    connection=self.connection,
                    trigger=m.get("trigger"),
                    latency_s=latency,
                )
            )
        elif kind == "Connected":
            self.request_id = str(m.get("request_id", ""))
        elif kind == "Error":
            msg = f"Deepgram STT error {m.get('code', '')}: {m.get('description', '')}"
            log.error("%s", msg)
            return msg
        elif kind in ("Warning", "ConfigureFailure"):
            log.warning("STT %s %s: %s", kind, m.get("code", ""), m.get("description", ""))
            self._on_state("warning", f"{m.get('code', '')}: {m.get('description', '')}")
        else:
            log.debug("STT message %s", kind)
        return None

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if self._connected.is_set():
            self._connected.clear()
            if self.ledger is not None:
                self.ledger.stt_disconnected()
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 2.0)

"""Local fakes of the three paid providers (no network beyond 127.0.0.1, nothing billable).

* ``FakeFlux``   — Deepgram ``/v2/listen`` WebSocket: auth, query params, audio accounting, TurnInfo.
* ``FakeFluxTTS``   — Deepgram ``/v2/speak`` WebSocket: Speak/Flush/Interrupt/Close, audio per character.
  Every Flush job's audio uses a distinct int16 sample value (``job_value``), so a test can tell
  exactly which job's audio reached the speaker, and prove that no stale audio was ever played.
* ``FakeOpenAI`` — Responses API over SSE via ``httpx.MockTransport``: scripted text, tool calls,
  delays, usage, HTTP errors; records requests and aborted streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

DG_KEY = "dg_test_key_0123456789"
OA_KEY = "sk-test-0123456789"


def _auth(expected: str):
    def process_request(conn: ServerConnection, request: Request) -> Response | None:
        if request.headers.get("Authorization") != expected:
            return conn.respond(401, "unauthorized\n")
        return None

    return process_request


# ============================================================================================ Flux STT
class FakeFlux:
    def __init__(self, *, http_status: int | None = None, keep_audio: bool = False) -> None:
        self.http_status = http_status
        self.keep_audio = keep_audio
        self.received: list[tuple[float, bytes]] = []  # (monotonic time, chunk) when keep_audio
        self.queries: list[dict[str, list[str]]] = []
        self.audio_bytes: list[int] = []  # per connection
        self.chunks: list[list[int]] = []  # chunk sizes per connection
        self.client_msgs: list[dict[str, Any]] = []
        self.conns: list[ServerConnection] = []
        self.closed_by_client = 0
        self._server: Any = None
        self.turn_index = 0
        self.connected = asyncio.Event()

    async def __aenter__(self) -> FakeFlux:
        def process_request(conn: ServerConnection, request: Request) -> Response | None:
            if self.http_status:
                return conn.respond(self.http_status, "nope\n")
            return _auth(f"Token {DG_KEY}")(conn, request)

        self._server = await websockets.serve(self._handler, "127.0.0.1", 0, process_request=process_request)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/v2/listen"

    async def _handler(self, ws: ServerConnection) -> None:
        self.queries.append(parse_qs(urlparse(ws.request.path).query))
        self.audio_bytes.append(0)
        self.chunks.append([])
        self.conns.append(ws)
        self.connected.set()
        idx = len(self.conns) - 1
        await ws.send(json.dumps({"type": "Connected", "request_id": f"flux-{idx}", "sequence_id": 0}))
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    self.audio_bytes[idx] += len(msg)
                    self.chunks[idx].append(len(msg))
                    if self.keep_audio:
                        self.received.append((asyncio.get_running_loop().time(), msg))
                    continue
                m = json.loads(msg)
                self.client_msgs.append(m)
                if m.get("type") == "CloseStream":
                    self.closed_by_client += 1
                    await ws.close()
                    return
        except websockets.ConnectionClosed:
            pass

    @property
    def ws(self) -> ServerConnection:
        return self.conns[-1]

    async def wait_connections(self, n: int = 1, timeout: float = 3.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.conns) < n:
                await asyncio.sleep(0.005)

    async def turn(
        self, event: str, transcript: str = "", *, turn_index: int | None = None, **extra: Any
    ) -> None:
        await self.ws.send(
            json.dumps(
                {
                    "type": "TurnInfo",
                    "request_id": "flux",
                    "sequence_id": 1,
                    "event": event,
                    "turn_index": self.turn_index if turn_index is None else turn_index,
                    "audio_window_start": 0.0,
                    "audio_window_end": 0.0,
                    "transcript": transcript,
                    "words": [],
                    "end_of_turn_confidence": 0.9 if event == "EndOfTurn" else 0.1,
                    **extra,
                }
            )
        )
        if event == "EndOfTurn" and turn_index is None:
            self.turn_index += 1

    async def utter(self, text: str) -> None:
        """A complete user turn: StartOfTurn → Update → EndOfTurn."""
        await self.turn("StartOfTurn", text.split()[0])
        await self.turn("Update", text)
        await self.turn("EndOfTurn", text, trigger="model")

    async def send_json(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg))

    async def drop(self) -> None:
        await self.ws.close(code=1011, reason="test drop")


# ============================================================================================ Aura TTS
@dataclass
class _Job:
    value: int
    text: str
    cancelled: bool = False
    finalized: bool = False


class FakeFluxTTS:
    def __init__(
        self,
        *,
        bytes_per_char: int = 480,  # 10 ms of 24 kHz audio per character
        frame_bytes: int = 1920,
        frame_delay_s: float = 0.0,
        first_audio_delay_s: float = 0.0,
        stale_frames_after_interrupt: int = 0,
        interrupted_delay_s: float = 0.0,
        ack_interrupt: bool = True,
        synth: Any = None,  # optional (job_no, text) -> PCM bytes, instead of constant-value frames
    ) -> None:
        self.synth = synth
        self.bytes_per_char = bytes_per_char
        self.frame_bytes = frame_bytes
        self.frame_delay_s = frame_delay_s
        self.first_audio_delay_s = first_audio_delay_s
        self.stale_frames_after_interrupt = stale_frames_after_interrupt
        self.interrupted_delay_s = interrupted_delay_s
        self.ack_interrupt = ack_interrupt
        self.queries: list[dict[str, list[str]]] = []
        self.speaks: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.flushes = 0
        self.interrupts = 0
        self.jobs: list[_Job] = []
        self.conns: list[ServerConnection] = []
        self._server: Any = None

    async def __aenter__(self) -> FakeFluxTTS:
        self._server = await websockets.serve(
            self._handler, "127.0.0.1", 0, process_request=_auth(f"Token {DG_KEY}")
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/v2/speak"

    @staticmethod
    def job_value(job_no: int) -> int:
        return 1000 + job_no  # sample value carried by every frame of Flush job ``job_no`` (0-based)

    async def _handler(self, ws: ServerConnection) -> None:
        self.queries.append(parse_qs(urlparse(ws.request.path).query))
        self.conns.append(ws)
        await ws.send(
            json.dumps(
                {"type": "Connected", "request_id": f"tts-{len(self.conns)}", "model_name": "flux-cliff-en"}
            )
        )
        pending: _Job | None = None
        queue: asyncio.Queue[_Job] = asyncio.Queue()
        current: list[_Job | None] = [None]
        send_lock = asyncio.Lock()
        session_ms = 0.0
        inputs = billable = 0

        def metadata(job):
            return {
                "speech_id": f"speech-{job.value}",
                "audio_duration_ms": len(job.text) * 10,
                "input_character_count": len(job.text),
                "billable_character_count": len(job.text),
            }

        async def worker():
            nonlocal session_ms, inputs, billable
            while True:
                job = await queue.get()
                current[0] = job
                await ws.send(json.dumps({"type": "SpeechStarted", "speech_id": f"speech-{job.value}"}))
                if self.first_audio_delay_s:
                    await asyncio.sleep(self.first_audio_delay_s)
                sent = 0
                while not job.cancelled:
                    pcm = self.synth(job.value - 1000, job.text) if self.synth else None
                    total = len(pcm) if pcm is not None else len(job.text) * self.bytes_per_char
                    if sent < total:
                        n = min(self.frame_bytes, total - sent)
                        frame = struct.pack("<h", job.value) * (self.frame_bytes // 2)
                        async with send_lock:
                            await ws.send(pcm[sent : sent + n] if pcm is not None else frame[:n])
                        sent += n
                        await asyncio.sleep(self.frame_delay_s)
                    elif job.finalized:
                        async with send_lock:
                            await ws.send(json.dumps({"type": "Flushed", "speech_id": f"speech-{job.value}"}))
                            await ws.send(json.dumps({"type": "SpeechMetadata", **metadata(job)}))
                        session_ms += sent / 48
                        inputs += len(job.text)
                        billable += len(job.text)
                        break
                    else:
                        await asyncio.sleep(0.001)
                current[0] = None

        w = asyncio.create_task(worker())
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.messages.append(message)
                kind = message["type"]
                if kind == "Speak":
                    self.speaks.append(message["text"])
                    if pending is None:
                        pending = _Job(self.job_value(len(self.jobs)), "")
                        self.jobs.append(pending)
                        queue.put_nowait(pending)
                    pending.text += message["text"]
                elif kind == "Flush":
                    self.flushes += 1
                    if pending is not None:
                        pending.finalized = True
                        pending = None
                elif kind == "Interrupt":
                    offset = message.get("playback_offset")
                    # The official SDK uses an integer millisecond offset. JSON
                    # 40.0 is a float even though Python compares it equal to 40.
                    if offset is not None and (
                        offset.get("type") != "time_ms"
                        or type(offset.get("value")) is not int
                        or offset["value"] < 0
                    ):
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "Error",
                                    "code": "MESSAGE-0000",
                                    "description": "The message could not be parsed.",
                                }
                            )
                        )
                        await ws.close(1008)
                        return
                    self.interrupts += 1
                    victim = current[0] or pending
                    while not queue.empty():
                        queued = queue.get_nowait()
                        queued.cancelled = True
                        victim = victim or queued
                    pending = None
                    if victim is not None:
                        victim.cancelled = True
                    async with send_lock:
                        stale = struct.pack("<h", victim.value if victim else 999) * (self.frame_bytes // 2)
                        for _ in range(self.stale_frames_after_interrupt):
                            await ws.send(stale)
                    await asyncio.sleep(self.interrupted_delay_s)
                    if self.ack_interrupt:
                        report = {"type": "SpeechInterrupted", "metadata": metadata(victim) if victim else {}}
                        offset = message.get("playback_offset")
                        if offset is not None and victim is not None:
                            chars = max(0, int((offset["value"] - session_ms) * 48 / self.bytes_per_char))
                            report.update(text_spoken=victim.text[:chars], text_remaining=victim.text[chars:])
                            session_ms = offset["value"]
                        if victim is not None:
                            inputs += len(victim.text)
                            billable += len(victim.text)
                        async with send_lock:
                            await ws.send(json.dumps(report))
                elif kind == "Close":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "SessionMetadata",
                                "total_audio_duration_ms": session_ms,
                                "total_input_character_count": inputs,
                                "total_billable_character_count": billable,
                            }
                        )
                    )
                    await ws.close(1000)
                    return
        except websockets.ConnectionClosed:
            pass
        finally:
            w.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await w

    async def drop(self) -> None:
        await self.conns[-1].close(code=1011, reason="test drop")


def sample_values(pcm: bytes) -> set[int]:
    """Distinct int16 values in a PCM byte string (0 = silence)."""
    vals = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) - len(pcm) % 2])
    return set(vals)


# ============================================================================================ OpenAI
@dataclass
class Script:
    """One scripted Responses API answer.

    ``steps``: ("text", str) | ("delay", seconds) | ("tool", name, arguments_json) | ("fail", message)
    ``status``: HTTP status to return instead of a stream (e.g. 404, 500).
    """

    steps: list[tuple[Any, ...]] = field(default_factory=list)
    status: int = 200
    usage: dict[str, Any] | None = None
    incomplete: str | None = None
    observation: str = ""


def say(*parts: str, delay: float = 0.0) -> Script:
    steps: list[tuple[Any, ...]] = []
    for p in parts:
        steps.append(("text", p))
        if delay:
            steps.append(("delay", delay))
    return Script(steps)


def structured_script(script: Script, *, observation: bool = False) -> Script:
    """Mock the real strict speech/actions stream without changing provider-level tool-call tests."""
    if any(step[0] == "raw" for step in script.steps):
        steps = [("text", step[1]) if step[0] == "raw" else step for step in script.steps]
    else:
        actions = [{"tool": step[1], "arguments_json": step[2]} for step in script.steps if step[0] == "tool"]
        steps = [("text", '{"speech":"')]
        for step in script.steps:
            if step[0] == "text":
                steps.append(("text", json.dumps(step[1], ensure_ascii=True)[1:-1]))
            elif step[0] != "tool":
                steps.append(step)
        suffix = ', "actions":' + json.dumps(actions)
        if observation:
            suffix += ', "observation":' + json.dumps(script.observation)
        steps.append(("text", '"' + suffix + "}"))
    return Script(steps, script.status, script.usage, script.incomplete)


class _EventStream(httpx.AsyncByteStream):
    def __init__(self, iterator):
        self.iterator = iterator

    async def __aiter__(self):
        async for chunk in self.iterator:
            yield chunk

    async def aclose(self):
        await self.iterator.aclose()


class FakeOpenAI:
    def __init__(self, scripts: list[Script] | None = None) -> None:
        self.scripts: deque[Script] = deque(scripts or [])
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.completed = 0
        self.aborted = 0
        self.default = say("Okay.")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def add(self, *scripts: Script) -> None:
        self.scripts.extend(scripts)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {OA_KEY}":
            return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "bad key"}})
        if request.url.path.endswith("/models/gpt-6-luna"):
            return httpx.Response(200, json={"id": "gpt-6-luna", "object": "model", "owned_by": "openai"})
        if "/models/" in request.url.path:
            return httpx.Response(
                404, json={"error": {"code": "model_not_found", "message": "no such model"}}
            )
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        script = self.scripts.popleft() if self.scripts else self.default
        if script.status != 200:
            code = "model_not_found" if script.status == 404 else "server_error"
            return httpx.Response(
                script.status, json={"error": {"code": code, "message": f"HTTP {script.status}"}}
            )
        if (body.get("text", {}).get("format") or {}).get("name") == "companion_reply":
            script = structured_script(
                script, observation="observation" in body["text"]["format"]["schema"]["properties"]
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_EventStream(self._sse(body, script))
        )

    async def _sse(self, body: dict[str, Any], script: Script) -> AsyncIterator[bytes]:
        seq = 0
        text = ""
        output: list[dict[str, Any]] = []
        done = False

        def ev(payload: dict[str, Any]) -> bytes:
            nonlocal seq
            seq += 1
            payload["sequence_number"] = seq
            return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n".encode()

        try:
            yield ev({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}})
            for step in script.steps:
                kind = step[0]
                if kind == "text":
                    text += step[1]
                    yield ev(
                        {
                            "type": "response.output_text.delta",
                            "item_id": "msg_1",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": step[1],
                            "logprobs": [],
                        }
                    )
                elif kind == "delay":
                    await asyncio.sleep(step[1])
                elif kind == "tool":
                    uid = uuid.uuid4().hex[:12]  # unique per call, like the real API
                    item = {
                        "type": "function_call",
                        "id": f"fc_{uid}",
                        "call_id": f"call_{uid}",
                        "name": step[1],
                        "arguments": step[2],
                        "status": "completed",
                    }
                    yield ev(
                        {
                            "type": "response.output_item.added",
                            "output_index": len(output),
                            "item": {**item, "arguments": ""},
                        }
                    )
                    yield ev(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item["id"],
                            "output_index": len(output),
                            "delta": step[2],
                        }
                    )
                    yield ev({"type": "response.output_item.done", "output_index": len(output), "item": item})
                    output.append(item)
                elif kind == "fail":
                    yield ev(
                        {
                            "type": "response.failed",
                            "response": {
                                "id": "resp_1",
                                "status": "failed",
                                "error": {"code": "server_error", "message": step[1]},
                                "usage": None,
                            },
                        }
                    )
                    done = True
                    return
            if text:
                output.insert(
                    0,
                    {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    },
                )
            usage = script.usage or {
                "input_tokens": max(1, len(json.dumps(body["input"])) // 4),
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": max(1, len(text) // 4),
                "output_tokens_details": {"reasoning_tokens": 0},
            }
            kind = "response.incomplete" if script.incomplete else "response.completed"
            done = True
            self.completed += 1
            yield ev(
                {
                    "type": kind,
                    "response": {
                        "id": "resp_1",
                        "status": kind.split(".")[1],
                        "output": output,
                        "usage": usage,
                        "incomplete_details": {"reason": script.incomplete} if script.incomplete else None,
                    },
                }
            )
            yield b"data: [DONE]\n\n"
        finally:
            if not done:
                self.aborted += 1


# ============================================================================================ helpers
async def until(pred: Any, timeout: float = 3.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError(f"timeout waiting for {what}")
        await asyncio.sleep(0.005)


__all__ = [
    "DG_KEY",
    "OA_KEY",
    "FakeFluxTTS",
    "FakeFlux",
    "FakeOpenAI",
    "Headers",
    "Script",
    "say",
    "sample_values",
    "until",
]

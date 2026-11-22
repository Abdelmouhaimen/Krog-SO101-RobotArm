"""LIVE test of the custom pipeline against the real providers. PAID — never runs by default.

    pytest -m live -s -k live_pipeline     # needs DEEPGRAM_API_KEY and OPENAI_API_KEY (.env)

Run only with the owner's explicit authorization. The "user" is synthetic speech generated with
Deepgram's REST TTS, streamed as microphone audio in real time; the speaker is drained in real time.
Expected cost: well under $0.01 (about 30 s of Flux audio, two short LLM turns, a few hundred characters
of TTS). A report is written to logs/live-pipeline-report.json.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from so101_voice.app import build_companion
from so101_voice.config import load_config, load_secrets
from so101_voice.conversation import Notice, Transcript, TurnLatency, UIEvent

from .fakes import FakeAudioIO

pytestmark = pytest.mark.live
REPORT = Path(__file__).resolve().parents[1] / "logs" / "live-pipeline-report.json"


async def synth(key: str, text: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.deepgram.com/v1/speak",
            params={
                "model": "aura-2-orion-en",
                "encoding": "linear16",
                "sample_rate": 16000,
                "container": "none",
            },
            headers={"Authorization": f"Token {key}"},
            json={"text": text},
        )
        r.raise_for_status()
        return r.content


async def test_live_pipeline_two_turns_and_barge_in():
    secrets = load_secrets()
    if not (secrets.deepgram_api_key and secrets.openai_api_key):
        pytest.skip("DEEPGRAM_API_KEY and OPENAI_API_KEY are required")
    cfg = load_config()
    cfg.llm.greeting = None
    cfg.cost.max_session_estimated_usd = 0.05
    events: list[tuple[float, UIEvent]] = []
    t0 = time.monotonic()
    audio = FakeAudioIO()
    comp = build_companion(
        cfg,
        secrets,
        on_event=lambda e: events.append((time.monotonic() - t0, e)),
        use_robot=False,
        audio=audio,
    )
    q1 = await synth(secrets.deepgram_api_key, "Hi! My name is Sam. What time is it?")
    q2 = await synth(secrets.deepgram_api_key, "Tell me a long story about a robot who learns to dance.")
    q3 = await synth(secrets.deepgram_api_key, "Wait, stop. What's my name?")
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def mic() -> None:
        frame, pending, nxt = 640, b"", time.monotonic()
        while True:
            if not pending and not queue.empty():
                pending = queue.get_nowait() + bytes(16000)
            chunk, pending = (pending[:frame], pending[frame:]) if pending else (bytes(frame), b"")
            if audio.on_audio:
                audio.speak(chunk.ljust(frame, b"\0"))
            nxt += 0.02
            await asyncio.sleep(max(0, nxt - time.monotonic()))

    async def speaker() -> None:
        nxt = time.monotonic()
        while True:
            if audio.buffer is not None:
                audio.pump(480)
            nxt += 0.02
            await asyncio.sleep(max(0, nxt - time.monotonic()))

    tasks = [asyncio.create_task(mic()), asyncio.create_task(speaker())]
    conv = comp.conversation
    try:
        await comp.startup()
        assert await conv.start()
        await asyncio.sleep(1.0)
        for q, wait in ((q1, 12), (q2, 4)):
            n = conv.generations_started
            queue.put_nowait(q)
            end = time.monotonic() + wait + 10
            while time.monotonic() < end and not (conv.generations_started > n and conv._gen is None):
                if q is q2 and conv.generations_started > n and conv.playback.state.value == "playing":
                    break
                await asyncio.sleep(0.05)
        await asyncio.sleep(2.0)
        queue.put_nowait(q3)
        await asyncio.sleep(12)
    finally:
        await comp.shutdown()
        for t in tasks:
            t.cancel()
        report = {
            "timeline": [
                f"{t:6.2f} {e}" for t, e in events if not isinstance(e, Notice) or e.level != "info"
            ],
            "latency": [e.__dict__ for _, e in events if isinstance(e, TurnLatency)],
            "cost": comp.ledger.report(),
            "interruptions": conv.interruptions,
        }
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2, default=str))
        print(json.dumps(report, indent=2, default=str))
    users = [e.text for _, e in events if isinstance(e, Transcript) and e.role == "user" and e.final]
    assert len(users) >= 3 and conv.interruptions >= 1
    assert conv.last_error is None

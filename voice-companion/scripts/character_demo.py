"""Expressive conversation demo: the whole app, the REAL SO-101 service with a FAKE arm, no paid calls.

    SO101_REPO=../robot-controller .venv/bin/python scripts/character_demo.py

The real objects are used (``build_companion``):
* conversation coordinator, LLM client with tool calling, robot tools, behavior director, Talk Mode;
* the SO-101 HTTP API (started headless with ``--fake-hardware --fake-arm``).

Fakes:
* speech recognition, LLM and TTS are local fakes (scripted user turns and scripted model replies,
  including tool calls);
* the speaker is drained in real time.

The owner's 15 behaviors are temporarily copied into the service's fake scratch library.

It prints one timeline merging what was said, the conversation phases, the LLM tool calls, and every
command the robot received (from the robot's own ledger).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from so101_voice.app import build_companion  # noqa: E402
from so101_voice.config import Secrets  # noqa: E402
from so101_voice.conversation import Phase, PhaseChanged, ToolActivity, Transcript  # noqa: E402
from so101_voice.robot.client import RobotAPIClient  # noqa: E402
from tests.fake_providers import DG_KEY, OA_KEY, FakeFlux, FakeFluxTTS, FakeOpenAI, Script, until  # noqa: E402
from tests.fakes import FakeAudioIO  # noqa: E402
from tests.rig import make_config  # noqa: E402
from tests.test_integration_so101_fake import run_so101_service  # noqa: E402


def gesture(behavior: str, next_idle: str | None = None) -> tuple[str, str, str]:
    args = {"behavior": behavior} | ({"next_idle": next_idle} if next_idle else {})
    return ("tool", "play_robot_behavior", json.dumps(args))


# (what the user says, the scripted model reply: one structured speech/action response)
CONVERSATION: list[tuple[str, list[Script]]] = [
    (
        "Hey Krog, you awake?",
        [
            Script([gesture("wavy", "idle_curious"), ("text", "Hey! Always awake for you. Ready to play?")]),
        ],
    ),
    (
        "We just lost the round thirteen to eleven.",
        [
            Script(
                [
                    gesture("comic_rage", "idle_sad"),
                    ("text", "Noooo! Thirteen eleven? That hurts. We'll get them next half."),
                ]
            ),
        ],
    ),
    (
        "Wait, I got an ace in the last round though!",
        [
            Script(
                [gesture("high_five_offer", "idle_hyped"), ("text", "An ace? No way! High five, champion!")]
            ),
        ],
    ),
    (
        "Thanks. What do you think about my aim?",
        [
            Script(
                [
                    (
                        "text",
                        "Honestly? Your aim is great. Your crosshair placement, though, we can talk about.",
                    )
                ]
            )
        ],
    ),
    (
        "Haha okay. I'm getting tired, good night.",
        [
            Script(
                [gesture("sleepy", "idle_watching"), ("text", "Good night, teammate. Same time tomorrow?")]
            ),
        ],
    ),
]


async def run(base: str, token: str) -> list[str]:
    t0 = time.monotonic()
    lines: list[tuple[float, str]] = []

    def at(text: str) -> None:
        lines.append((time.monotonic() - t0, text))

    def on_event(ev: Any) -> None:
        match ev:
            case PhaseChanged(phase=p):
                at(f"  [{p.value}]")
            case Transcript(role="user", text=t, final=True):
                at(f'USER: "{t}"')
            case Transcript(role="assistant", text=t):
                at(f'KROG: "{t}"')
            case ToolActivity(name=n, status="started"):
                at(f"  (LLM tool call: {n})")

    async with FakeFlux() as flux, FakeFluxTTS(frame_delay_s=0.004) as aura:
        oai = FakeOpenAI([s for _, scripts in CONVERSATION for s in scripts])
        cfg = make_config(
            flux,
            aura,
            robot={"base_url": base},
            character={"calm_idles": ["idle_watching"]},
            robot_tools={"command_poll_s": 0.1, "status_poll_s": 0.5},
        )
        audio = FakeAudioIO()
        robot = RobotAPIClient(base, token, timeout_s=3.0)
        comp = build_companion(
            cfg,
            Secrets(deepgram_api_key=DG_KEY, openai_api_key=OA_KEY, so101_api_token=token),
            on_event=on_event,
            robot=robot,
            audio=audio,
            llm_transport=oai.transport(),
        )
        conv, director = comp.conversation, comp.director
        assert director is not None

        async def speaker() -> None:  # the device callback: 20 ms per 20 ms, once the speaker is open
            while True:
                if audio.buffer is not None:
                    audio.pump(480)
                await asyncio.sleep(0.02)

        pump = asyncio.get_running_loop().create_task(speaker())
        try:
            await comp.startup()
            assert await conv.start()
            await flux.wait_connections(1)
            await until(lambda: director.active is not None and director.active.label == "idle_watching", 25)
            for text, _ in CONVERSATION:
                n = conv.generations_started
                await flux.utter(text)
                await until(
                    lambda n=n: (
                        conv.generations_started > n and conv._gen is None and conv.phase is Phase.LISTENING
                    ),
                    30,
                )
                a = director.active
                if a is not None and a.kind == "reaction":  # let the reaction finish and its idle start
                    await until(lambda: director.active is not None and director.active.kind == "idle", 30)
                await asyncio.sleep(1.5)
            await conv.stop("demo finished")
            await asyncio.sleep(1.0)
        finally:
            pump.cancel()
            await comp.shutdown()
        # the robot's own ledger, merged into the timeline
        check = RobotAPIClient(base, token, timeout_s=3.0)
        cmds = await check.commands(100)
        await check.aclose()
    wall0 = time.time() - (time.monotonic() - t0)
    from datetime import datetime

    for c in cmds:
        if c.get("source") != "api" or c["kind"] not in ("play_behavior", "go_rest", "go_pose"):
            continue
        created = datetime.fromisoformat(c["created_at"]).timestamp() - wall0
        name = c["params"].get("behavior_name") or c["kind"]
        lines.append(
            (
                created,
                f"  ROBOT {name:16} -> {c['state']}"
                + (f" (by {c['superseded_by']})" if c.get("superseded_by") else ""),
            )
        )
    return [f"{t:6.2f}s  {m}" for t, m in sorted(lines)]


def main() -> int:
    with (
        tempfile.TemporaryDirectory() as td,
        run_so101_service(Path(td), seed_behaviors=True) as (base, token, _),
    ):
        timeline = asyncio.run(run(base, token))
    print("\n".join(timeline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M3B against the REAL SO-101 service with a FAKE arm (never real hardware).

    SO101_REPO=../robot-controller pytest -m robot_fake -s -k character

The owner's 15 behaviors are temporarily copied into the service's fake-hardware scratch library (see
``seeded_fake_library``). Everything else is the real HTTP API: routes, saved-setting defaults, command
ledger, supersession and idempotency.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from so101_voice.character.director import BehaviorDirector
from so101_voice.character.library import BehaviorLibrary
from so101_voice.character.persona import EXPECTED_BEHAVIORS
from so101_voice.config import CharacterConfig, RobotToolsConfig
from so101_voice.robot.client import RobotAPIClient
from so101_voice.robot.motion import RobotMotion

from .fake_providers import until
from .test_integration_so101_fake import run_so101_service

pytestmark = pytest.mark.robot_fake


async def test_character_workflow_on_the_real_api_with_fake_arm(tmp_path):
    with run_so101_service(tmp_path, seed_behaviors=True) as (base, token, _proc):
        client = RobotAPIClient(base, token, timeout_s=3.0)
        library = BehaviorLibrary(client, expected=EXPECTED_BEHAVIORS)
        director = BehaviorDirector(
            client,
            RobotMotion(client),
            library,
            CharacterConfig(min_switch_interval_s=0.0, calm_idles=["idle_watching"]),
            RobotToolsConfig(command_poll_s=0.1, status_poll_s=0.5),
        )
        t0 = time.monotonic()
        timeline: list[str] = []

        def mark(what: str) -> None:
            timeline.append(f"{time.monotonic() - t0:6.2f}s  {what}")

        try:
            await director.start()
            st = await client.status()
            assert "FakeRobot" in st["robot"]["driver"]  # never the physical arm
            assert set(EXPECTED_BEHAVIORS) <= set(library.behaviors)  # A01: discovered from the API
            assert library.resolve("idle_curious").loops and not library.resolve("wavy").loops  # A04

            async def ledger_state(cid: str) -> str:
                return str((await client.command(cid))["state"])

            # Session start: wake_up (one-shot) -> idle_watching (loop)
            director.on_phase("connecting")
            await until(lambda: director.active is not None and director.active.label == "wake_up", 5)
            wake = director.active.command_id
            mark("session start: wake_up sent")
            await until(lambda: director.active.label == "idle_watching", 20, "idle after wake_up")
            assert await ledger_state(wake) == "completed"  # A06/A21: the robot's own completion
            mark("wake_up completed (ledger) -> idle_watching")
            watching = director.active.command_id
            rec = await client.command(watching)
            assert rec["params"]["effective_settings"]["repeat_count"] is None  # A05: loops until stopped

            # Reaction -> planned idle
            director.phase = "speaking"
            await director.play("wavy", key="call-demo-1", next_idle="idle_curious")
            mark("LLM tool: wavy (then idle_curious)")
            assert await ledger_state(watching) == "superseded"  # A09
            wavy = director.active.command_id
            await until(lambda: director.active.label == "idle_curious", 20, "idle_curious after wavy")
            assert await ledger_state(wavy) == "completed"
            mark("wavy completed -> idle_curious")

            # Talk Mode overlay alongside the arm behavior (A13)
            await client.talk_start(lease_s=2.0)
            talk = await client.talk_status()
            assert talk["state"] == "ON" and director.active.label == "idle_curious"
            assert await ledger_state(director.active.command_id) == "running"
            await client.talk_stop()
            mark("Talk Mode ON during idle_curious (gripper overlay), then OFF")

            # A superseded reaction cannot start its stale idle (A10)
            await director.play("thinking", key="call-demo-2", next_idle="idle_sad")
            thinking = director.active.command_id
            await asyncio.sleep(0.5)
            await director.play("acknowledge", key="call-demo-3", next_idle="idle_hyped")
            mark("thinking superseded by acknowledge")
            assert await ledger_state(thinking) == "superseded"
            await until(lambda: director.active.label == "idle_hyped", 20, "idle_hyped after acknowledge")
            mark("acknowledge completed -> idle_hyped (idle_sad dropped)")

            # Go Rest: pending continuation cancelled, automation paused (A20)
            await director.play("shy_reaction", key="call-demo-4", next_idle="idle_curious")
            await director.go_rest(key="call-demo-5")
            mark("shy_reaction, then Go Rest")
            await asyncio.sleep(6.0)
            assert director.active.label == "rest" and director.suspended == "resting"

            # The robot's own ledger: no rest was inserted between reactions and idles (A08)
            cmds = [
                c
                for c in (await client.commands(50))[::-1]
                if c["source"] == "api" and c["kind"] != "talk_start"
            ]
            names = [
                c["params"].get("behavior_name", c["kind"])
                for c in cmds
                if c["kind"] in ("play_behavior", "go_rest")
            ]
            mark("robot ledger: " + " -> ".join(names))
            assert names == [
                "wake_up",
                "idle_watching",
                "wavy",
                "idle_curious",
                "thinking",
                "acknowledge",
                "idle_hyped",
                "shy_reaction",
                "go_rest",
            ]
            replay = await RobotMotion(client).play_behavior(
                library.resolve("wavy").id, idempotency_key="call-demo-1"
            )
            assert replay.get("idempotent_replay")  # A16 on the real API
        finally:
            print("\n".join(timeline))
            await director.aclose()
            await client.aclose()


async def test_expressive_conversation_demo_end_to_end(tmp_path):
    """The whole app (fake voice providers), the real API, the fake arm (scripts/character_demo.py)."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    import character_demo

    with run_so101_service(tmp_path, seed_behaviors=True) as (base, token, _proc):
        timeline = await character_demo.run(base, token)
    robot = [line.split("ROBOT", 1)[1].split()[0] for line in timeline if "ROBOT" in line]
    assert robot == [
        "wake_up",
        "idle_watching",
        "wavy",
        "idle_curious",
        "comic_rage",
        "idle_sad",
        "high_five_offer",
        "idle_hyped",
        # A reply without a gesture preserves the explicitly chosen idle_hyped mood.
        "sleepy",
        "idle_watching",
        "go_rest",  # session end: the looping idle is stopped
    ]

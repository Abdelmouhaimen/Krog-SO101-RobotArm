"""M3B: behavior library, robot tools and the behavior director (fake SO-101 API, no paid calls)."""

from __future__ import annotations

import asyncio
import json

import pytest

from so101_voice.character.director import ActionRefused, BehaviorDirector, Priority
from so101_voice.character.library import BehaviorLibrary, UnknownBehavior
from so101_voice.character.persona import EXPECTED_BEHAVIORS, build_instructions
from so101_voice.config import CharacterConfig, RobotToolsConfig
from so101_voice.robot.client import RobotAPIClient
from so101_voice.robot.motion import RobotMotion
from so101_voice.tools.registry import Effect, FunctionCall, ToolRegistry
from so101_voice.tools.robot_tools import register_robot_tools

from .fake_providers import until
from .fakes import LIBRARY, FakeSO101API


class Harness:
    def __init__(self, api: FakeSO101API | None = None, **char) -> None:
        self.api = api or FakeSO101API()
        self.client: RobotAPIClient = self.api.client()
        self.library = BehaviorLibrary(self.client, expected=EXPECTED_BEHAVIORS)
        self.notices: list[tuple[str, str]] = []
        cfg = CharacterConfig(min_switch_interval_s=0.0, **char)
        tools = RobotToolsConfig(command_poll_s=0.02, status_poll_s=0.1, library_refresh_s=5)
        self.director = BehaviorDirector(
            self.client,
            RobotMotion(self.client),
            self.library,
            cfg,
            tools,
            on_notice=lambda lv, t: self.notices.append((lv, t)),
        )
        self.registry = ToolRegistry(allow_physical_tools=True)
        register_robot_tools(self.registry, self.director, self.library)

    async def start(self) -> Harness:
        await self.director.start()
        return self

    async def call(self, name: str, args: dict, call_id: str = "c1") -> dict:
        res = await self.registry.execute(FunctionCall(call_id, name, json.dumps(args)))
        return json.loads(res.content)

    async def close(self) -> None:
        await self.director.aclose()
        await self.client.aclose()


# ------------------------------------------------------------------ M3B-A01..A03: dynamic library
async def test_library_loads_from_the_api_and_resolves_names_to_ids():
    h = await Harness().start()
    lib = h.library
    assert set(lib.behaviors) == set(LIBRARY)  # A01: all 15, from GET /behaviors
    assert lib.resolve("wavy").id == LIBRARY["wavy"][0]  # A02: name -> stable id
    assert lib.resolve("WAVY").name == "wavy" and lib.resolve(LIBRARY["wavy"][0]).name == "wavy"
    assert [b.name for b in lib.idles()] == sorted(n for n, v in LIBRARY.items() if v[1] is None)
    assert lib.resolve("idle_curious").loops and not lib.resolve("wavy").loops  # from saved repetitions
    with pytest.raises(UnknownBehavior):
        lib.resolve("moonwalk")  # no invented animations
    await h.close()


async def test_missing_behaviors_are_reported_and_not_offered():
    api = FakeSO101API()
    api.behaviors.pop("comic_rage")
    h = await Harness(api).start()
    schema = next(t for t in h.registry.openai_tools() if t["name"] == "play_robot_behavior")["parameters"]
    assert "comic_rage" not in schema["properties"]["behavior"]["enum"]
    assert "wavy" in schema["properties"]["behavior"]["enum"]
    assert h.director.describe_library()["missing"] == ["comic_rage"]
    res = await h.call("play_robot_behavior", {"behavior": "comic_rage", "next_idle": "idle_watching"})
    assert not res["ok"] and "no behavior named 'comic_rage'" in res["error"]["message"]
    assert api.motion_posts == []  # nothing was sent
    assert "comic_rage" not in build_instructions("Krog", h.library)
    await h.close()


async def test_library_follows_changes_in_the_studio():
    api = FakeSO101API()
    h = await Harness(api).start()
    api.behaviors["idle_dancing"] = ("new-id-123", None, "rest", 6.0)  # recorded later in SO-101 Studio
    api.behaviors.pop("sleepy")
    res = await h.call("list_robot_behaviors", {"refresh": True})
    idles = [b["name"] for b in res["result"]["idles_loop_until_replaced"]]
    reactions = [b["name"] for b in res["result"]["reactions_play_once"]]
    assert "idle_dancing" in idles and "sleepy" not in reactions
    assert h.library.resolve("idle_dancing").loops  # kind inferred from its saved repetitions
    await h.close()


# ---------------------------------------------------------- A04..A09: saved settings, loops, reactions
async def test_saved_repetitions_are_inherited_and_idles_loop_until_replaced():
    api = FakeSO101API()
    h = await Harness(api).start()
    await h.call("play_robot_behavior", {"behavior": "idle_curious"}, "c1")
    assert api.motion_posts[-1][1] == {}  # A04: no repeat/speed sent -> the robot uses the SAVED settings
    await asyncio.sleep(0.4)
    assert api.active()["label"] == "idle_curious" and api.active()["state"] == "running"  # A05
    res = await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_watching"}, "c2")
    assert res["ok"] and api.ledger["cmd_0001"]["state"] == "superseded"  # A09: the loop was replaced
    await until(lambda: api.ledger["cmd_0002"]["state"] == "completed" or api.active()["label"] != "wavy", 2)
    assert api.ledger["cmd_0002"]["state"] == "completed"  # A06: the one-shot finished by itself
    await until(lambda: api.played()[-1] == "idle_watching", 2, "planned next idle")  # A07
    assert "rest" not in api.played()  # A08: never a rest between reaction and idle
    assert h.director.continuations_sent == 1
    await h.close()


async def test_explicit_overrides_apply_to_that_run_only():
    api = FakeSO101API()
    h = await Harness(api).start()
    res = await h.call(
        "play_robot_behavior", {"behavior": "wavy", "repeat": 2, "speed": 1.5, "next_idle": "idle_curious"}
    )
    assert res["ok"] and api.motion_posts[-1][1] == {"speed": 1.5, "repeat": 2}
    bad = await h.call("play_robot_behavior", {"behavior": "wavy", "speed": 5.0}, "c2")
    assert bad["error"]["code"] == "invalid_arguments" and len(api.motion_posts) == 1
    await h.close()


# ---------------------------------------------------------- A10, A20, A21: completion and supersession
async def test_superseded_reaction_never_starts_its_stale_idle():
    api = FakeSO101API(auto_complete=False)
    h = await Harness(api).start()
    await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"}, "c1")
    await h.call("play_robot_behavior", {"behavior": "comic_rage", "next_idle": "idle_sad"}, "c2")
    wavy, rage = "cmd_0001", "cmd_0002"
    assert api.ledger[wavy]["state"] == "superseded"
    api.ledger[wavy].update(state="completed")  # even a (bogus) late completion of the old command ...
    await asyncio.sleep(0.2)
    assert "idle_curious" not in api.played()  # ... cannot start its idle
    api.complete(rage)
    await until(lambda: api.played()[-1] == "idle_sad", 2)
    assert api.played() == ["wavy", "comic_rage", "idle_sad"]
    await h.close()


async def test_go_rest_cancels_the_pending_continuation_and_pauses_automatic_idles():
    api = FakeSO101API(auto_complete=False)
    h = await Harness(api).start()
    h.director.phase = "speaking"  # a session is running
    await h.call("play_robot_behavior", {"behavior": "thinking", "next_idle": "idle_curious"}, "c1")
    res = await h.call("robot_go_rest", {}, "c2")
    assert res["ok"]
    api.complete("cmd_0001")
    await asyncio.sleep(0.3)
    assert api.played() == ["thinking", "rest"]  # A20: no idle after rest
    h.director.on_phase("listening")
    await asyncio.sleep(1.5)
    assert api.played() == ["thinking", "rest"] and h.director.suspended == "resting"
    await h.call("play_robot_behavior", {"behavior": "idle_watching"}, "c3")  # deliberate: resumes
    assert h.director.suspended is None
    await h.close()


async def test_completion_comes_from_command_status_not_a_timer():
    api = FakeSO101API(auto_complete=False)  # nominal 2.1 s wave, but the robot decides when it is done
    h = await Harness(api).start()
    await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"})
    await asyncio.sleep(1.0)  # far beyond the scaled nominal duration
    assert api.played() == ["wavy"]
    api.complete("cmd_0001")
    await until(lambda: api.played() == ["wavy", "idle_curious"], 2)
    await h.close()


# ------------------------------------------------------------------ A11, A22: priorities and moods
async def test_automatic_phase_idles_never_interrupt_a_reaction():
    api = FakeSO101API(auto_complete=False)
    h = await Harness(api).start()
    h.director.phase = "thinking"  # a session is running
    await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"})
    h.director.on_phase("speaking")  # speech starts while wavy plays
    await asyncio.sleep(1.0)
    assert api.played() == ["wavy"]  # not replaced by idle_talking
    api.complete("cmd_0001")
    await until(lambda: api.played() == ["wavy", "idle_curious"], 2)
    await h.close()


async def test_conversation_phases_drive_listening_talking_and_calm_idles():
    api = FakeSO101API()
    h = await Harness(api, calm_idles=["idle_watching"]).start()
    h.director.phase = "listening"  # session already running (no wake_up)
    h.director.on_phase("user_speaking")
    await until(lambda: api.played()[-1:] == ["idle_listening"], 2)
    h.director.on_phase("thinking")
    h.director.on_phase("speaking")
    await until(lambda: api.played()[-1:] == ["idle_talking"], 2)
    h.director.on_phase("listening")
    await until(lambda: api.played()[-1:] == ["idle_watching"], 3)
    n = len(api.played())
    h.director.on_phase("speaking")  # a short buffer gap: speaking -> listening -> speaking quickly
    h.director.on_phase("listening")
    h.director.on_phase("speaking")
    await asyncio.sleep(0.2)
    assert len(api.played()) == n  # debounced: no flapping
    await h.close()


async def test_the_agent_chooses_emotional_idles_that_persist_after_speaking():
    api = FakeSO101API()
    h = await Harness(api).start()
    h.director.phase = "speaking"
    await h.call("play_robot_behavior", {"behavior": "idle_hyped"})
    assert h.director.active.priority is Priority.AGENT and h.director.mood == "idle_hyped"
    h.director.on_phase("listening")  # after speaking: the calm idle is the agent's mood
    await asyncio.sleep(1.5)
    assert api.played()[-1] == "idle_hyped" and api.played().count("idle_hyped") == 1  # not restarted
    await h.call("play_robot_behavior", {"behavior": "idle_sad"}, "c2")
    assert api.played()[-1] == "idle_sad"
    h.director.on_phase("user_speaking")
    await h.director._auto_select()
    assert api.played()[-1] == "idle_sad"  # intentional mood survives speech phases
    h.director.on_phase("speaking")
    await h.director._auto_select()
    assert api.played()[-1] == "idle_sad"
    h.director._mood_until = 0
    await h.director._auto_select()
    assert api.played()[-1] == "idle_talking"
    await h.close()


async def test_session_start_wakes_up_then_idles_and_session_end_rests():
    api = FakeSO101API()
    h = await Harness(api).start()
    h.director.on_phase("connecting")
    await until(lambda: api.played()[:2] == ["wake_up", "idle_watching"], 3)
    h.director.on_phase("idle")
    await until(lambda: api.played()[-1] == "rest", 2)
    await h.close()


# ------------------------------------------------------------------ A14..A16, A19: tools and safety
async def test_tool_schemas_are_structured_and_offer_only_safe_actions():
    h = await Harness().start()
    tools = {t["name"]: t for t in h.registry.openai_tools()}
    assert set(tools) == {
        "list_robot_behaviors",
        "play_robot_behavior",
        "list_robot_poses",
        "go_to_robot_pose",
        "robot_go_rest",
        "get_robot_status",
    }
    play = tools["play_robot_behavior"]["parameters"]
    assert play["additionalProperties"] is False and play["required"] == ["behavior"]
    assert set(play["properties"]["next_idle"]["enum"]) == {n for n, v in LIBRARY.items() if v[1] is None}
    # What the LLM can DO: tool names and argument names (behavior names and descriptions are data)
    surface = " ".join([*tools, *(k for t in tools.values() for k in t["parameters"].get("properties", {}))])
    for banned in ("joint", "torque", "calibrat", "serial", "recording", "follow", "talk", "emergency"):
        assert banned not in surface, banned
    assert all(h.registry._tools[n].effect in (Effect.READ, Effect.PHYSICAL) for n in tools)
    await h.close()


@pytest.mark.parametrize(
    "args",
    [
        {"behavior": "wavy", "next_idle": "comic_rage"},  # next_idle must be a looping idle
        {"behavior": "idle_watching", "next_idle": "idle_sad"},  # next_idle only for reactions
        {"behavior": "wavy; rm -rf /"},
        {"behavior": "wavy", "joint": 90},
        {"behavior": "wavy", "repeat": 500},
    ],
)
async def test_invalid_arguments_never_move_the_robot(args):
    api = FakeSO101API()
    h = await Harness(api).start()
    res = await h.call("play_robot_behavior", args)
    assert not res["ok"] and api.motion_posts == []
    await h.close()


async def test_duplicate_and_retried_calls_move_the_robot_once():
    api = FakeSO101API()
    h = await Harness(api).start()
    a, b = await asyncio.gather(
        h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"}, "dup"),
        h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"}, "dup"),
    )
    assert a["ok"] and b["ok"] and len([p for p in api.motion_posts if "behaviors" in p[0]]) == 1
    # A transport retry with the same Idempotency-Key is replayed by the robot, not executed again
    rec = await h.director.motion.play_behavior(LIBRARY["acknowledge"][0], idempotency_key="k-retry")
    again = await h.director.motion.play_behavior(LIBRARY["acknowledge"][0], idempotency_key="k-retry")
    assert again["id"] == rec["id"] and again.get("idempotent_replay")
    assert api.played().count("acknowledge") == 1
    await h.close()


async def test_retry_after_timeout_reuses_the_idempotency_key():
    api = FakeSO101API()
    h = await Harness(api).start()
    real = api._motion
    calls = {"n": 0}

    def flaky(path, body, key):
        calls["n"] += 1
        resp = real(path, body, key)  # the robot DID accept it ...
        if calls["n"] == 1:
            import httpx

            raise httpx.ReadTimeout("response lost")  # ... but the response was lost
        return resp

    api._motion = flaky
    res = await h.director.motion.play_behavior(LIBRARY["wavy"][0], idempotency_key="k-lost")
    assert res.get("idempotent_replay") and api.played().count("wavy") == 1
    await h.close()


async def test_emergency_hold_and_faults_block_actions_and_continuations():
    api = FakeSO101API(auto_complete=False)
    h = await Harness(api).start()
    await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"}, "c1")
    api.latch_emergency_hold()  # the operator hit Emergency Hold during the reaction
    await asyncio.sleep(0.3)
    res = await h.call("play_robot_behavior", {"behavior": "idle_watching"}, "c2")
    assert not res["ok"] and "Emergency Hold" in res["error"]["message"]
    assert api.played() == ["wavy"]  # no continuation, no new action
    api.emergency_hold, api.fault = False, "overheat"
    res = await h.call("play_robot_behavior", {"behavior": "idle_watching"}, "c3")
    assert not res["ok"] and "fault" in res["error"]["message"]
    await h.close()


async def test_operator_takeover_in_the_studio_pauses_automation():
    api = FakeSO101API()
    h = await Harness(api).start()
    await h.call("play_robot_behavior", {"behavior": "idle_curious"})
    api.studio_takeover("idle_sad")
    await until(lambda: h.director.suspended == "operator", 2)
    h.director.on_phase("user_speaking")
    await asyncio.sleep(1.0)
    assert api.played()[-1] == "idle_sad"  # the companion does not fight the operator
    await h.close()


async def test_real_arm_motion_is_refused_unless_allowed():
    api = FakeSO101API(driver="LeRobot SOFollower")
    h = await Harness(api).start()
    h.director.tools_cfg = RobotToolsConfig(allow_physical=False, command_poll_s=0.02, status_poll_s=0.1)
    res = await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"})
    assert not res["ok"] and "switched off" in res["error"]["message"]
    assert api.motion_posts == []
    h.director.tools_cfg = RobotToolsConfig(command_poll_s=0.02, status_poll_s=0.1)  # the default: allowed
    res = await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"}, "c2")
    assert res["ok"] and api.played() == ["wavy"]
    await h.close()


async def test_robot_down_gives_a_meaningful_error():
    api = FakeSO101API()
    h = await Harness(api).start()
    api.down = True
    res = await h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"})
    assert not res["ok"] and "unavailable" in res["error"]["message"]
    status = await h.call("get_robot_status", {}, "c2")
    assert not status["ok"]
    await h.close()


async def test_concurrent_requests_are_serialized_and_the_newest_wins():
    api = FakeSO101API(auto_complete=False)
    h = await Harness(api).start()
    await asyncio.gather(
        h.call("play_robot_behavior", {"behavior": "wavy", "next_idle": "idle_curious"}, "a"),
        h.call("play_robot_behavior", {"behavior": "shy_reaction", "next_idle": "idle_watching"}, "b"),
        h.call("play_robot_behavior", {"behavior": "acknowledge", "next_idle": "idle_sad"}, "c"),
    )
    last = h.director.active
    for cid, rec in api.ledger.items():
        if cid != last.command_id:
            assert rec["state"] == "superseded"
        api.complete(cid)
    await asyncio.sleep(0.4)
    assert (
        api.played()[-1]
        == {"wavy": "idle_curious", "shy_reaction": "idle_watching", "acknowledge": "idle_sad"}[last.label]
    )
    assert len(api.played()) == 4  # three reactions + exactly one continuation
    await h.close()


async def test_poses_are_validated_and_hold():
    api = FakeSO101API(poses={"Watching": "pose-1"})
    h = await Harness(api).start()
    assert (await h.call("list_robot_poses", {}))["result"]["poses"] == ["Watching"]
    bad = await h.call("go_to_robot_pose", {"pose": "Dab"}, "c2")
    assert not bad["ok"] and api.motion_posts == []
    ok = await h.call("go_to_robot_pose", {"pose": "Watching", "transition": "gentle"}, "c3")
    assert (
        ok["ok"]
        and api.motion_posts[-1][1] == {"transition": "gentle"}
        and h.director.suspended == "holding pose"
    )
    await h.close()


def test_unknown_behaviors_raise_and_no_action_is_refused_silently():
    assert issubclass(ActionRefused, RuntimeError)

"""Single model response, streamed speech, independent validated actions; entirely local fakes."""

import asyncio
import dataclasses
import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from so101_voice.audio.playback import PlaybackState
from so101_voice.conversation import Notice, TurnLatency
from so101_voice.llm.reply import SpeechStream
from so101_voice.tools.registry import Effect, Tool

from .fake_providers import FakeFlux, FakeFluxTTS, FakeOpenAI, Script, say, until
from .fakes import FakeSO101API
from .rig import Rig

JPEG = (Path(__file__).parent / "fixtures/screen.jpg").read_bytes()
QUICK = {"session_start_reaction": None, "automatic_idles": False, "rest_on_stop": False}
MEASUREMENTS = []
REPORT = Path(__file__).resolve().parents[1] / "planning/SINGLE_RESPONSE_MEASUREMENTS.json"


def world(body):
    item = next(item for item in body["input"] if item.get("role") == "developer")
    return json.loads(item["content"].split(": ", 1)[1])


def test_streamed_speech_never_leaks_json_actions_or_split_escape_sequences():
    value = {
        "speech": 'Hi "friend". Here is a newline.\nAnd Unicode: 🦊 café.',
        "actions": [{"tool": "set_follow_me", "arguments_json": '{"enabled":true}'}],
    }
    raw = json.dumps(value)
    for size in (1, 2, 7, 100):
        parser = SpeechStream()
        spoken = "".join(parser.feed(raw[i : i + size]) for i in range(0, len(raw), size))
        assert spoken == value["speech"]
        assert parser.finish().actions[0].tool == "set_follow_me"
        assert "arguments_json" not in spoken


@pytest.mark.parametrize("case", ["conversation", "follow", "behavior", "multiple"])
async def test_one_request_for_speech_and_actions_with_measured_mock_timings(case):
    seq = 0

    def receiver(request):
        nonlocal seq
        seq += 1
        return httpx.Response(
            200,
            json={
                "screen": {"available": False},
                "face": {
                    "found": True,
                    "angle_deg": 12,
                    "camera_angle_deg": 2,
                    "confidence": 0.1,
                    "age_s": 0.01,
                    "seq": seq,
                    "session": "mock-sender",
                },
            },
        )

    actions = []
    if case in ("follow", "multiple"):
        actions.append(("tool", "set_follow_me", '{"enabled":false}'))
    if case in ("behavior", "multiple"):
        actions.append(("tool", "play_robot_behavior", '{"behavior":"wavy","next_idle":"idle_curious"}'))
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        api = FakeSO101API(time_scale=0.5)
        oai = FakeOpenAI(
            [Script([("delay", 0.02), ("text", "Hey, I'm here with you. "), ("delay", 0.03), *actions])]
        )
        rig = Rig(
            flux,
            aura,
            oai,
            api=api,
            character=QUICK,
            perception={"enabled": True, "receiver_url": "http://receiver.test"},
            follow={"calibration_valid": True, "acquire_s": 0},
            perception_transport=httpx.MockTransport(receiver),
        )
        try:
            assert await rig.start()
            await flux.wait_connections(1)
            await flux.utter("Hello teammate")
            await until(lambda: rig.conv._gen is None and rig.conv.generations_started == 1, 5)
            await until(lambda: len(rig.conv.action_dispatch_s) == len(actions), 2)
            assert len(oai.requests) == 1
            assert "tools" not in oai.requests[0]
            assert not any(
                i.get("type") in ("function_call", "function_call_output")
                for i in oai.requests[0]["input"] + rig.conv.history.items
            )
            assert "Hey, I'm here with you." in rig.assistant_text()
            if case in ("follow", "multiple"):
                await until(lambda: not rig.companion.follow.requested, 2)
                assert not api.follow_on
            if case in ("behavior", "multiple"):
                await until(lambda: "wavy" in api.played(), 2)
            latency = next(event for event in rig.events if isinstance(event, TurnLatency))
            measurement = {
                "case": case,
                "luna_requests": len(oai.requests),
                "first_speech_text_ms": round(latency.llm_first_text_s * 1000, 2),
                "first_audio_ms": round(latency.playback_start_s * 1000, 2),
                "decision_to_dispatch_ms": [round(x * 1000, 2) for x in rig.conv.action_dispatch_s],
                "source": "local fake providers and fake robot; synthetic model delays",
            }
            MEASUREMENTS.append(measurement)
            print("SINGLE_RESPONSE_TIMING " + json.dumps(measurement))
            if case == "multiple":
                await asyncio.to_thread(REPORT.write_text, json.dumps(MEASUREMENTS, indent=2) + "\n")
        finally:
            await rig.close()


async def test_fresh_state_reports_running_reaction_and_refreshes_next_turn():
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        api = FakeSO101API(time_scale=20)
        oai = FakeOpenAI(
            [
                Script(
                    [
                        ("text", "I'll try a wave."),
                        ("tool", "play_robot_behavior", '{"behavior":"wavy","next_idle":"idle_curious"}'),
                    ]
                ),
                say("Still chatting."),
                say("Here I am."),
            ]
        )
        rig = Rig(flux, aura, oai, api=api, character=QUICK, robot_tools={"command_poll_s": 0.02})
        try:
            assert await rig.start()
            await flux.wait_connections(1)
            await flux.utter("Wave")
            await until(lambda: "wavy" in api.played(), 2)
            await until(lambda: rig.conv._gen is None, 5)
            wave = rig.companion.director.active.command_id
            await flux.utter("What are you doing now?")
            await until(lambda: len(oai.requests) == 2, 3)
            snapshot = world(oai.requests[1])
            assert snapshot["current_behavior"] == "wavy"
            assert snapshot["behavior_kind"] == "reaction" and snapshot["behavior_status"] == "running"
            assert snapshot["follow_me"] is False  # robot service's actual overlay state
            await until(lambda: rig.conv._gen is None, 5)
            api.complete(wave)
            await until(lambda: rig.companion.director.active.label == "idle_curious", 3)
            await flux.utter("And now?")
            await until(lambda: len(oai.requests) == 3, 3)
            assert world(oai.requests[2])["current_behavior"] == "idle_curious"
            assert world(oai.requests[2])["behavior_kind"] == "looping_idle"
            assert sum(item.get("role") == "developer" for item in oai.requests[2]["input"]) == 1
            assert all(item.get("role") != "developer" for item in rig.conv.history.items)
        finally:
            await rig.close()


async def test_same_gesture_after_app_restart_gets_a_new_robot_command():
    api = FakeSO101API(auto_complete=False)
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        commands = []
        for run in range(2):
            oai = FakeOpenAI(
                [
                    Script(
                        [
                            ("text", "High five!"),
                            (
                                "tool",
                                "play_robot_behavior",
                                '{"behavior":"high_five_offer","next_idle":"idle_hyped"}',
                            ),
                        ]
                    )
                ]
            )
            rig = Rig(flux, aura, oai, api=api, character=QUICK)
            try:
                assert await rig.start()
                await flux.wait_connections(run + 1)
                await flux.utter("High five")
                await until(
                    lambda rig=rig: (
                        rig.companion.director.active is not None
                        and rig.companion.director.active.label == "high_five_offer"
                    ),
                    3,
                )
                command = rig.companion.director.active.command_id
                commands.append(command)
                assert commands.count(command) == 1
                api.complete(command)
            finally:
                await rig.close()
        assert api.played().count("high_five_offer") == 2


async def test_slow_independent_actions_do_not_block_speech_and_duplicates_execute_once():
    class Args(BaseModel):
        value: int

    entered = set()
    release = asyncio.Event()
    calls = []

    async def first(args, ctx):
        entered.add("first")
        await release.wait()
        calls.append(("first", args.value))

    async def second(args, ctx):
        entered.add("second")
        await release.wait()
        calls.append(("second", args.value))

    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        oai = FakeOpenAI(
            [
                Script(
                    [
                        ("text", "I'll try those two actions now."),
                        ("tool", "first", '{"value":1}'),
                        ("tool", "second", '{"value":2}'),
                        ("tool", "first", '{ "value": 1 }'),
                    ]
                )
            ]
        )
        rig = Rig(flux, aura, oai)
        rig.conv.tools.register(Tool("first", "first", Args, first, effect=Effect.VIRTUAL))
        rig.conv.tools.register(Tool("second", "second", Args, second, effect=Effect.VIRTUAL))
        try:
            assert await rig.start()
            await flux.wait_connections(1)
            await flux.utter("Try both actions")
            await until(lambda: entered == {"first", "second"}, 2)
            await until(lambda: rig.playback.state is PlaybackState.PLAYING, 2)
            assert not calls and len(oai.requests) == 1
            release.set()
            await until(lambda: len(calls) == 2, 2)
            assert sorted(calls) == [("first", 1), ("second", 2)]
        finally:
            release.set()
            await rig.close()


async def test_pending_ordered_actions_are_cancelled_on_interruption():
    entered = asyncio.Event()
    never = asyncio.Event()
    dispatched = []

    async def slow(args, ctx):
        dispatched.append(args.behavior)
        entered.set()
        await never.wait()

    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        oai = FakeOpenAI(
            [
                Script(
                    [
                        ("text", "I'll try a wave and then a sad mood."),
                        ("tool", "play_robot_behavior", '{"behavior":"wavy","next_idle":"idle_curious"}'),
                        ("tool", "play_robot_behavior", '{"behavior":"idle_sad"}'),
                    ]
                ),
                say("Okay, stopping."),
            ]
        )
        rig = Rig(flux, aura, oai, api=FakeSO101API(), character=QUICK)
        old = rig.conv.tools
        old._tools["play_robot_behavior"] = dataclasses.replace(
            old._tools["play_robot_behavior"], handler=slow
        )
        try:
            assert await rig.start()
            await flux.wait_connections(1)
            await flux.utter("Try a wave")
            await until(entered.is_set, 2)
            await until(lambda: rig.conv._gen is None, 5)
            await flux.turn("StartOfTurn", "Wait")
            await until(lambda: rig.conv.phase.value == "user_speaking", 2)
            never.set()
            await asyncio.sleep(0.05)
            assert dispatched == ["wavy"]
            await flux.turn("EndOfTurn", "Wait, stop please")
            await until(lambda: len(oai.requests) == 2 and rig.conv._gen is None, 5)
            assert dispatched == ["wavy"]  # second arm action never dispatched
        finally:
            await rig.close()


async def test_failed_action_is_local_and_incomplete_json_never_dispatches_actions():
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        oai = FakeOpenAI(
            [
                Script(
                    [
                        ("text", "I'll try it."),
                        ("tool", "play_robot_behavior", '{"behavior":"does_not_exist"}'),
                    ]
                ),
                Script(
                    [("raw", '{"speech":"I can still speak.","actions":[')], incomplete="max_output_tokens"
                ),
            ]
        )
        api = FakeSO101API()
        rig = Rig(flux, aura, oai, api=api, character=QUICK)
        try:
            assert await rig.start()
            await flux.wait_connections(1)
            await flux.utter("Try an action")
            await until(lambda: rig.conv.last_action_error is not None, 2)
            await until(lambda: rig.conv._gen is None, 5)
            assert len(oai.requests) == 1
            assert any(
                isinstance(e, Notice) and "Action play_robot_behavior failed" in e.text for e in rig.events
            )
            assert "succeeded" not in rig.assistant_text()
            count = len(api.motion_posts)
            await flux.utter("Speak again")
            await until(lambda: len(oai.requests) == 2 and rig.conv.last_error is not None, 3)
            assert len(api.motion_posts) == count
        finally:
            await rig.close()


async def test_screen_unavailable_is_explicit_and_ordinary_turns_never_fetch_images():
    from so101_voice.perception.lan import PerceptionClient
    from so101_voice.turn_context import WorldContext

    paths = []

    def receive(request):
        paths.append(request.url.path)
        if request.url.path == "/v1/frame":
            return httpx.Response(404)
        return httpx.Response(200, json={"screen": {"available": False}, "face": {"found": False}})

    client = PerceptionClient("http://receiver.test", "test", transport=httpx.MockTransport(receive))
    try:
        provider = WorldContext(None, client, None)
        ordinary = await provider("Hello, how are you?")
        assert paths == ["/v1/status"] and not ordinary.images
        requested = await provider("What is on the game screen?")
        assert paths[-1] == "/v1/frame"
        assert not requested.images and not requested.state["screen_available"]
        assert not requested.state["image_included"]
    finally:
        await client.aclose()

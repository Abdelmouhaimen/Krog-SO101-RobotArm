"""M3B through the whole voice pipeline: LLM tool calls -> robot behaviors, with Talk Mode alongside."""

from __future__ import annotations

import asyncio
import json

import pytest

from so101_voice.audio.playback import PlaybackState
from so101_voice.conversation import Phase, ToolActivity

from .fake_providers import FakeFlux, FakeFluxTTS, FakeOpenAI, Script, say, until
from .fakes import FakeSO101API
from .rig import Rig

FAST = {"command_poll_s": 0.02, "status_poll_s": 0.1}
QUICK = {"min_switch_interval_s": 0.0, "session_start_reaction": None, "calm_idles": ["idle_watching"]}


@pytest.fixture
async def flux():
    async with FakeFlux() as f:
        yield f


async def start(rig: Rig) -> None:
    assert await rig.start()
    await rig.flux.wait_connections(1)
    await until(lambda: rig.conv.phase is Phase.LISTENING)


async def test_llm_gesture_tool_call_plays_reaction_then_planned_idle_with_talk_mode(flux):
    api = FakeSO101API(time_scale=0.3)
    async with FakeFluxTTS(frame_delay_s=0.003) as aura:
        oai = FakeOpenAI(
            [
                Script(
                    [
                        (
                            "tool",
                            "play_robot_behavior",
                            json.dumps({"behavior": "wavy", "next_idle": "idle_curious"}),
                        ),
                        ("text", "Hey, good to see you! How was your match?"),
                    ]
                ),
            ]
        )
        rig = Rig(flux, aura, oai, api=api, robot_tools=FAST, character=QUICK)
        await start(rig)
        await flux.utter("Hi Krog!")
        await until(lambda: rig.playback.state is PlaybackState.PLAYING, 3, "speech")
        assert "wavy" in api.played()  # the gesture started before/with the speech
        await until(lambda: api.talk_active(), 2, "Talk Mode on")  # gripper follows audio, independently
        assert api.active()["label"] == "wavy"  # A13: Talk Mode overlay and the arm behavior coexist
        await until(lambda: "idle_curious" in api.played(), 5, "planned idle")
        seq = api.played()
        assert seq.index("idle_curious") == seq.index("wavy") + 1  # reaction -> planned idle, nothing between
        assert "rest" not in seq
        await until(lambda: rig.conv._gen is None and rig.conv.phase is Phase.LISTENING, 5)
        await until(lambda: "stop" in api.talk_calls, 2)  # A12: Talk Mode stops after the last sample
        assert len(oai.requests) == 1
        assert not any(i.get("type") == "function_call_output" for i in oai.requests[0]["input"])
        assert [e.status for e in rig.events if isinstance(e, ToolActivity)] == ["started", "ok"]
        assert "tools" not in oai.requests[0]
        assert oai.requests[0]["text"]["format"]["type"] == "json_schema"
        assert "wavy" in oai.requests[0]["instructions"]  # the persona lists the live library
        await rig.close()


async def test_barge_in_before_dispatch_sends_nothing_to_the_robot(flux):
    api = FakeSO101API()
    async with FakeFluxTTS() as aura:
        oai = FakeOpenAI(
            [
                Script(
                    [
                        (
                            "tool",
                            "play_robot_behavior",
                            json.dumps({"behavior": "comic_rage", "next_idle": "idle_sad"}),
                        ),
                        ("delay", 0.6),  # the response is still streaming: the call is not committed yet
                    ]
                ),
                say("Okay, what is it?"),
            ]
        )
        rig = Rig(flux, aura, oai, api=api, robot_tools=FAST, character={**QUICK, "automatic_idles": False})
        await start(rig)
        await flux.utter("I lost the round")
        await until(lambda: len(oai.requests) == 1, 2)
        await asyncio.sleep(0.2)
        await flux.turn("StartOfTurn", "wait")  # the user interrupts
        await flux.turn("EndOfTurn", "wait, never mind")
        await until(lambda: rig.conv._gen is None and len(oai.requests) == 2, 5)
        await asyncio.sleep(0.3)
        assert "comic_rage" not in api.played()  # the uncommitted gesture never reached the robot
        await rig.close()


async def test_robot_api_down_keeps_the_conversation_going(flux):
    api = FakeSO101API()
    async with FakeFluxTTS() as aura:
        oai = FakeOpenAI(
            [
                Script(
                    [
                        (
                            "tool",
                            "play_robot_behavior",
                            json.dumps({"behavior": "wavy", "next_idle": "idle_curious"}),
                        ),
                        ("text", "Hi there!"),
                    ]
                ),
            ]
        )
        rig = Rig(flux, aura, oai, api=api, robot_tools=FAST, character=QUICK)
        await start(rig)
        api.down = True
        await flux.utter("Hello")
        await until(lambda: len(oai.requests) == 1 and rig.conv._gen is None, 5)
        assert rig.conv.last_action_error is not None  # failure is local, with no model feedback loop
        assert "Hi there!" in rig.assistant_text() and rig.conv.last_error is None
        await rig.close()


async def test_robot_tools_can_be_disabled_entirely(flux):
    async with FakeFluxTTS() as aura:
        oai = FakeOpenAI([say("Just talking.")])
        rig = Rig(flux, aura, oai, api=FakeSO101API(), robot_tools={"enabled": False})
        await start(rig)
        await flux.utter("Hi")
        await until(lambda: len(oai.requests) == 1, 2)
        assert oai.requests[0]["text"]["format"]["schema"]["properties"]["actions"]["maxItems"] == 0
        assert rig.companion.director is None
        assert "physical expression is unavailable" in oai.requests[0]["instructions"]
        await rig.close()

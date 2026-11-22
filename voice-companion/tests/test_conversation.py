"""End-to-end pipeline tests: real coordinator + clients against local fakes (no paid calls)."""

from __future__ import annotations

import asyncio
import json

import pytest

from so101_voice.audio.playback import PlaybackState
from so101_voice.conversation import Notice, Phase, ToolActivity, TurnLatency
from so101_voice.cost import BudgetState
from so101_voice.llm.history import INTERRUPTED_NOTE
from so101_voice.motion.speech_motion import RobotLink

from .fake_providers import FakeFlux, FakeFluxTTS, FakeOpenAI, Script, sample_values, say, until
from .fakes import FakeSO101API
from .rig import Rig


@pytest.fixture
async def flux():
    async with FakeFlux() as f:
        yield f


@pytest.fixture
async def aura():
    async with FakeFluxTTS() as a:
        yield a


async def ready(rig: Rig) -> None:
    assert await rig.start()
    await rig.flux.wait_connections(1)
    await until(lambda: rig.conv.phase is Phase.LISTENING, what="LISTENING")


async def finished_speaking(rig: Rig, timeout: float = 5.0) -> None:
    """Wait for a NEW generation (since the last call) to have been played to the end."""
    await until(lambda: rig.conv.generations_started > rig.checked_gens, timeout, "a new generation")
    await until(
        lambda: (
            rig.conv._gen is None and rig.conv.phase is Phase.LISTENING and rig.playback.buffered_s() == 0
        ),
        timeout,
        "reply played to the end",
    )
    rig.checked_gens = rig.conv.generations_started


async def ask(rig: Rig, text: str, timeout: float = 5.0) -> None:
    """One complete user turn, then wait until its answer has been played to the end."""
    rig.checked_gens = rig.conv.generations_started
    await rig.flux.utter(text)
    await finished_speaking(rig, timeout)


# ------------------------------------------------------------------ A02, A03, A04, A05, A06
async def test_end_of_turn_triggers_exactly_one_request_and_reply_is_spoken(flux, aura):
    oai = FakeOpenAI([say("Hi there! ", "Nice to meet you.")])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await flux.turn("StartOfTurn", "Hello")
    await flux.turn("Update", "Hello there")
    await asyncio.sleep(0.05)
    assert oai.requests == []  # provisional events never reach the LLM
    await flux.turn("EndOfTurn", "Hello there", trigger="model")
    await finished_speaking(rig)
    assert len(oai.requests) == 1
    req = oai.requests[0]
    assert req["model"] == "gpt-6-luna" and req["stream"] is True and req["store"] is False
    assert req["reasoning"] == {"effort": "none"} and "temperature" not in req
    assert req["input"][-2] == {"role": "user", "content": "Hello there"}
    assert "tools" not in req and req["text"]["format"]["name"] == "companion_reply"
    assert rig.transcripts("user") == ["Hello there"]
    assert "Hi there!" in rig.assistant_text() and "Nice to meet you." in rig.assistant_text()
    assert aura.speaks and aura.flushes >= 1
    played = sample_values(bytes(rig.audio.played))
    assert {FakeFluxTTS.job_value(i) for i in range(len(aura.jobs))} <= played  # all synthesized audio played
    ph = rig.phases()
    assert ph.index(Phase.USER_SPEAKING) < ph.index(Phase.THINKING) < ph.index(Phase.SPEAKING)
    assert rig.conv.history.items[-1] == {"role": "assistant", "content": "Hi there! Nice to meet you."}
    await rig.close()


async def test_duplicate_end_of_turn_is_answered_once(flux, aura):
    oai = FakeOpenAI([say("One."), say("Two.")])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await flux.turn("EndOfTurn", "hello", turn_index=0)
    await flux.turn("EndOfTurn", "hello", turn_index=0)  # same turn delivered twice
    await finished_speaking(rig)
    await asyncio.sleep(0.1)
    assert len(oai.requests) == 1
    await rig.close()


async def test_context_survives_multiple_turns(flux, aura):
    oai = FakeOpenAI([say("Nice to meet you, Sam."), say("Your name is Sam.")])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await ask(rig, "My name is Sam.")
    await ask(rig, "What's my name?")
    second = oai.requests[1]["input"]
    assert second[:-1] == [
        {"role": "user", "content": "My name is Sam."},
        {"role": "assistant", "content": "Nice to meet you, Sam."},
        {"role": "user", "content": "What's my name?"},
    ]
    assert oai.requests[1]["instructions"] == oai.requests[0]["instructions"]  # stable prefix (caching)
    await rig.close()


async def test_speech_starts_before_the_llm_finishes(flux, aura):
    oai = FakeOpenAI(
        [Script([("text", "Sure thing. "), ("delay", 0.8), ("text", "Here is the rest of it.")])]
    )
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await flux.utter("Tell me something")
    await until(lambda: rig.playback.state is PlaybackState.PLAYING, 2.0, "playback")
    assert oai.completed == 0  # still streaming: audio started from the first sentence
    assert aura.speaks[0] == "Sure thing."
    await finished_speaking(rig)
    lat = [e for e in rig.events if isinstance(e, TurnLatency)]
    assert len(lat) == 1 and lat[0].playback_start_s is not None and lat[0].playback_start_s < 0.8
    assert lat[0].llm_first_text_s <= lat[0].tts_first_audio_s <= lat[0].playback_start_s
    await rig.close()


async def test_chunking_streams_phrases_and_flushes_only_at_reply_end(flux, aura):
    long = ["First sentence here. "] + [f"This is sentence number {i} of a longer answer. " for i in range(8)]
    oai = FakeOpenAI([say(*long)])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await ask(rig, "Tell me a long story")
    assert aura.speaks[0] == "First sentence here."  # first segment alone, flushed at once
    assert aura.flushes == 1  # Flux owns internal streaming boundaries
    assert len(aura.speaks) >= 5  # but text still streamed sentence by sentence
    await rig.close()


# ------------------------------------------------------------------ A07, A08 (Talk Mode)
async def test_talk_mode_follows_real_playback(flux, aura):
    api = FakeSO101API()
    oai = FakeOpenAI([Script([("delay", 0.3), ("text", "Hello! I am speaking now, for a little while.")])])
    rig = Rig(flux, aura, oai, api=api)
    await ready(rig)
    await flux.utter("Hi")
    await until(lambda: rig.conv.phase is Phase.THINKING)
    await asyncio.sleep(0.15)
    assert api.talk_calls == []  # thinking: no Talk Mode
    await until(lambda: rig.playback.state is PlaybackState.PLAYING, 2.0)
    await until(lambda: api.talk_active(), 1.0, "talk on")
    await until(lambda: rig.playback.state is PlaybackState.IDLE, 5.0, "drained")
    t_drained = asyncio.get_running_loop().time()
    await until(lambda: "stop" in api.talk_calls, 1.0, "talk stop")
    assert asyncio.get_running_loop().time() - t_drained < 0.3
    assert api.talk_calls[0] == "start" and api.talk_calls.count("stop") == 1
    assert rig.motion.stats.starts == 1
    await rig.close()


# ------------------------------------------------------------------ A09, A10, A11 (barge-in)
async def test_barge_in_stops_everything_and_no_stale_audio_ever_plays(flux):
    api = FakeSO101API()
    async with FakeFluxTTS(
        stale_frames_after_interrupt=5, interrupted_delay_s=0.05, frame_delay_s=0.005
    ) as aura:
        story = ["Once upon a time. "] + [
            f"Part {i} of a very long story keeps going on and on. " for i in range(10)
        ]
        oai = FakeOpenAI([say(*story, delay=0.05), say("Sure, I stopped.")])
        rig = Rig(flux, aura, oai, api=api)
        await ready(rig)
        await flux.utter("Tell me a story")
        await until(lambda: api.talk_active(), 3.0, "speaking")
        await asyncio.sleep(0.3)
        old_jobs = {FakeFluxTTS.job_value(i) for i in range(len(aura.jobs))}
        await flux.turn("StartOfTurn", "Wait")  # barge-in
        await until(
            lambda: rig.playback.buffered_s() == 0 and not api.talk_active(), 0.5, "flushed + talk off"
        )
        n_played = len(rig.audio.played)
        assert aura.interrupts == 1
        await until(lambda: oai.aborted == 1, 1.0, "LLM stream cancelled")
        assert rig.conv.interruptions == 1
        assert any(isinstance(e, Notice) and e.text.startswith("interrupted") for e in rig.events)
        await asyncio.sleep(0.2)  # stale frames + late text arrive now; none may play
        await flux.turn("EndOfTurn", "Wait, stop.")
        await finished_speaking(rig)
        after = sample_values(bytes(rig.audio.played[n_played:]))
        assert not (after & old_jobs), "audio from the interrupted answer played after the barge-in"
        assert rig.conv.tts is not None and rig.conv.tts.discarded_audio_bytes > 0
        # History keeps only what was heard + a developer note (no inline marker the model could copy).
        hist = rig.conv.history.items
        i_note = next(i for i, it in enumerate(hist) if it.get("role") == "developer")
        cut = hist[i_note - 1]
        assert cut["role"] == "assistant" and cut["content"].startswith("Once upon a time.")
        assert "Part 9" not in cut["content"] and "…" not in cut["content"]
        assert hist[i_note]["content"] == INTERRUPTED_NOTE
        assert hist[-2:] == [
            {"role": "user", "content": "Wait, stop."},
            {"role": "assistant", "content": "Sure, I stopped."},
        ]
        assert rig.ledger.tts.discarded_characters > 0  # unplayed synthesis is still metered
        await rig.close()


async def test_rapid_interruptions_do_not_cause_duplicate_responses(flux, aura):
    oai = FakeOpenAI([say("A " * 30 + "end.", delay=0.0)] * 5 + [say("Final answer.")])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    for i in range(4):
        await flux.turn("StartOfTurn", "x")
        await flux.turn("EndOfTurn", f"question {i}")
        await asyncio.sleep(0.03)
    await flux.turn("StartOfTurn", "x")
    await flux.turn("EndOfTurn", "last question")
    await finished_speaking(rig)
    await asyncio.sleep(0.2)
    users = [i["content"] for i in rig.conv.history.items if i.get("role") == "user"]
    assert users[-1] == "last question"
    items = rig.conv.history.items
    complete = [
        it
        for k, it in enumerate(items)
        if it.get("role") == "assistant"
        and not (k + 1 < len(items) and items[k + 1].get("role") == "developer")
    ]
    assert len(complete) == 1  # exactly one full answer: the one for the last turn
    assert items[-1] == complete[0] and items[-2] == {"role": "user", "content": "last question"}
    assert rig.conv._gen is None and rig.conv.phase is Phase.LISTENING
    await rig.close()


async def test_push_to_talk_press_interrupts_immediately(flux, aura):
    oai = FakeOpenAI([say("This is a fairly long answer that will be interrupted by the user. " * 3)])
    rig = Rig(flux, aura, oai, input={"mode": "push_to_talk", "idle_close_s": 0.5})
    assert await rig.start()  # gated mode: the STT stream opens only when the user talks
    rig.conv.set_push_to_talk(True)
    await rig.mic(0.5, value=3000)
    rig.conv.set_push_to_talk(False)
    await rig.mic(1.0)  # post-roll, then the gate closes
    await until(lambda: any(m.get("type") == "ForceEndTurn" for m in flux.client_msgs), 2.0, "ForceEndTurn")
    await flux.turn("EndOfTurn", "talk to me")
    await until(lambda: rig.playback.state is PlaybackState.PLAYING, 3.0)
    rig.conv.set_push_to_talk(True)  # pressing again interrupts at once, before Flux even reacts
    assert rig.playback.buffered_s() == 0 and rig.conv.interruptions == 1
    rig.conv.set_push_to_talk(False)
    await rig.close()


# ------------------------------------------------------------------ A13 (provider failures)
async def test_llm_server_error_is_reported_and_the_conversation_continues(flux, aura):
    oai = FakeOpenAI([Script(status=500), say("Back again.")])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await flux.utter("Hello?")
    await until(lambda: any(isinstance(e, Notice) and "HTTP 500" in e.text for e in rig.events), 2.0)
    assert rig.conv.phase is Phase.LISTENING and rig.conv.running
    await ask(rig, "Hello again?")
    assert "Back again." in rig.assistant_text()
    await rig.close()


async def test_unavailable_model_fails_clearly_without_substitution(flux, aura):
    oai = FakeOpenAI([Script(status=404)])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await flux.utter("Hello?")
    await until(lambda: rig.conv.phase is Phase.ERROR, 2.0)
    assert (
        "gpt-6-luna" in (rig.conv.last_error or "") and "No other model is substituted" in rig.conv.last_error
    )
    assert all(r["model"] == "gpt-6-luna" for r in oai.requests)
    await rig.close()


async def test_stream_failure_event_is_handled(flux, aura):
    oai = FakeOpenAI([Script([("text", "Hmm. "), ("fail", "overloaded")]), say("Okay now.")])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await flux.utter("Hi")
    await until(lambda: any(isinstance(e, Notice) and "overloaded" in e.text for e in rig.events), 2.0)
    assert rig.ledger.llm.estimated_requests == 1  # no usage reported: estimated, still metered
    await ask(rig, "Hi again")
    await rig.close()


async def test_tts_disconnect_mid_speech_stops_audio_and_talk_mode(flux):
    api = FakeSO101API()
    async with FakeFluxTTS(frame_delay_s=0.01) as aura:
        oai = FakeOpenAI(
            [say("A long answer that keeps going for a while so we can drop it. " * 4), say("I'm back.")]
        )
        rig = Rig(flux, aura, oai, api=api)
        await ready(rig)
        await flux.utter("Talk")
        await until(lambda: api.talk_active(), 3.0)
        await aura.drop()
        await until(
            lambda: not api.talk_active() and rig.playback.buffered_s() == 0, 1.0, "talk off after drop"
        )
        assert any(isinstance(e, Notice) and "text-to-speech connection lost" in e.text for e in rig.events)
        await ask(rig, "Are you there?")  # a new TTS connection is opened for the next answer
        assert "I'm back." in rig.assistant_text() and len(aura.conns) >= 2
        await rig.close()


async def test_stt_drop_reconnects(flux, aura):
    rig = Rig(flux, aura, FakeOpenAI([say("Still here.")]))
    await ready(rig)
    await flux.drop()
    await flux.wait_connections(2)
    await asyncio.sleep(0.05)
    await ask(rig, "Are you there?")
    assert "Still here." in rig.assistant_text()
    await rig.close()


async def test_bad_deepgram_key_fails_fast():
    async with FakeFlux(http_status=401) as flux, FakeFluxTTS() as aura:
        rig = Rig(flux, aura, FakeOpenAI())
        assert await rig.start()
        await until(lambda: rig.conv.phase is Phase.ERROR, 2.0)
        assert "401" in (rig.conv.last_error or "")
        await rig.close()


# ------------------------------------------------------------------ A14 (robot down)
async def test_robot_api_down_does_not_break_the_conversation(flux, aura):
    api = FakeSO101API(down=True)
    rig = Rig(flux, aura, FakeOpenAI([say("I can still talk.")]), api=api)
    await ready(rig)
    await ask(rig, "Hello")
    assert "I can still talk." in rig.assistant_text()
    assert rig.motion.status.link is RobotLink.UNAVAILABLE
    assert "robot animation disconnected" in rig.motion.status.detail
    await rig.close()


# ------------------------------------------------------------------ A19 (budget cap)
async def test_budget_cap_stops_new_paid_requests(flux, aura):
    oai = FakeOpenAI([say("This answer uses up the whole tiny budget, sorry about that.")])
    rig = Rig(flux, aura, oai, cost={"max_session_estimated_usd": 0.0005})
    await ready(rig)
    await flux.utter("Hello")
    await until(lambda: rig.conv.budget_stopped, 5.0, "budget stop")
    assert rig.ledger.state is BudgetState.EXCEEDED
    n_req, n_speak = len(oai.requests), len(aura.speaks)
    await until(lambda: not rig.conv.stt.connected, 2.0, "STT stream closed")  # type: ignore[union-attr]
    await rig.mic(0.5, value=2000)
    await asyncio.sleep(0.1)
    assert len(oai.requests) == n_req and len(aura.speaks) == n_speak  # nothing new was paid for
    assert any(isinstance(e, Notice) and "cap" in e.text for e in rig.events)
    await rig.close()


# ------------------------------------------------------------------ A21 (tools)
async def test_time_state_needs_one_request_and_no_tool_feedback(flux, aura):
    oai = FakeOpenAI([say("It is time for tea.")])
    rig = Rig(flux, aura, oai)
    await ready(rig)
    await ask(rig, "What time is it?")
    assert len(oai.requests) == 1 and "local_time" in oai.requests[0]["input"][-1]["content"]
    assert not any(isinstance(e, ToolActivity) for e in rig.events)
    assert "It is time for tea." in rig.assistant_text()
    await rig.close()


async def test_invalid_action_arguments_are_reported_locally_without_continuation(flux, aura):
    oai = FakeOpenAI(
        [Script([("text", "I'll try a gesture."), ("tool", "play_robot_behavior", '{"behavior":123}')])]
    )
    rig = Rig(flux, aura, oai, api=FakeSO101API())
    await ready(rig)
    await ask(rig, "Wave")
    await until(lambda: rig.conv.last_action_error is not None, 2)
    assert json.loads(rig.conv.last_action_error)["error"]["code"] == "invalid_arguments"
    assert len(oai.requests) == 1
    assert not any(item.get("type") == "function_call_output" for item in rig.conv.history.items)
    await rig.close()


# ------------------------------------------------------------------ greeting, mute, gated input
async def test_greeting_is_spoken_and_recorded(flux, aura):
    rig = Rig(flux, aura, FakeOpenAI(), llm={"greeting": "Hey! I'm awake."})
    await ready(rig)
    await finished_speaking(rig)
    assert aura.speaks == ["Hey!", " I'm awake."]  # first sentence goes out alone, for a fast start
    assert rig.conv.history.items == [{"role": "assistant", "content": "Hey! I'm awake."}]
    await rig.close()


async def test_mute_closes_the_stt_stream_and_sends_nothing(flux, aura):
    rig = Rig(flux, aura, FakeOpenAI())
    await ready(rig)
    await rig.mic(0.4, value=500)
    await until(lambda: flux.audio_bytes[0] >= 2560 * 4, 1.0)
    assert set(flux.chunks[0]) == {2560}  # 80 ms chunks at 16 kHz linear16
    rig.conv.set_muted(True)
    await until(lambda: flux.closed_by_client == 1, 1.0, "CloseStream")
    sent = sum(flux.audio_bytes)
    await rig.mic(0.5, value=500)
    await asyncio.sleep(0.1)
    assert sum(flux.audio_bytes) == sent
    rig.conv.set_muted(False)
    await flux.wait_connections(2)
    await rig.close()


async def test_push_to_talk_sends_only_the_utterance_with_preroll(flux, aura):
    rig = Rig(
        flux,
        aura,
        FakeOpenAI(),
        input={"mode": "push_to_talk", "preroll_ms": 200, "postroll_ms": 200, "idle_close_s": 0.5},
    )
    assert await rig.start()
    await rig.mic(1.0)  # gate closed: nothing is sent, nothing connects
    await asyncio.sleep(0.1)
    assert flux.conns == []
    rig.conv.set_push_to_talk(True)
    await rig.mic(0.5, value=1000)
    rig.conv.set_push_to_talk(False)
    await rig.mic(0.6)
    await flux.wait_connections(1)
    await until(lambda: any(m.get("type") == "ForceEndTurn" for m in flux.client_msgs), 2.0)
    await until(lambda: flux.closed_by_client == 1, 2.0, "idle close")
    sent_s = flux.audio_bytes[0] / 32000
    assert 0.85 <= sent_s <= 0.95  # 0.2 pre-roll + 0.5 speech + 0.2 post-roll, not the 2.1 s captured
    assert rig.ledger.stt.audio_s == pytest.approx(sent_s)
    await rig.close()


async def test_fast_exchanges_use_one_final_flush_per_reply_without_aura_rate_limits(flux, aura):
    oai = FakeOpenAI([say("Sure. ", "And a second sentence follows here.")] * 8)
    rig = Rig(flux, aura, oai)
    await ready(rig)
    t0 = asyncio.get_running_loop().time()
    for i in range(8):
        await ask(rig, f"question {i}")
    assert asyncio.get_running_loop().time() - t0 < 8.0  # never waited for the 60 s window
    assert aura.flushes == 8  # exactly one final Flush for each reply
    assert not any(isinstance(e, Notice) and "rate limit" in e.text for e in rig.events)
    await rig.close()


# ------------------------------------------------------------------ echo guard (found in a live run)
async def test_echo_of_the_assistant_voice_neither_interrupts_nor_is_answered(flux):
    async with FakeFluxTTS(frame_delay_s=0.005) as aura:
        oai = FakeOpenAI([say("Hey! Good to see you. How is your day going so far, my friend? " * 2)])
        rig = Rig(flux, aura, oai)
        await ready(rig)
        await flux.utter("Hello")
        await until(lambda: rig.playback.state is PlaybackState.PLAYING, 3.0)
        # Open speakers: Flux transcribes the assistant's own voice as a user turn.
        await flux.turn("StartOfTurn", "Hey")
        await flux.turn("Update", "Hey good to see you")
        await flux.turn("EndOfTurn", "Hey. Good to see you.")
        await asyncio.sleep(0.2)
        assert rig.conv.interruptions == 0 and len(oai.requests) == 1
        assert rig.playback.state is PlaybackState.PLAYING
        assert any(isinstance(e, Notice) and "echo" in e.text for e in rig.events)
        # A real interruption with different words still barges in at once.
        await flux.turn("StartOfTurn", "Stop")
        await until(lambda: rig.conv.interruptions == 1, 1.0)
        await rig.close()


async def test_echo_guard_can_be_disabled(flux, aura):
    oai = FakeOpenAI([say("Hey! Good to see you. How is your day going so far, my friend? " * 2)])
    rig = Rig(flux, aura, oai, input={"echo_guard": False})
    await ready(rig)
    await flux.utter("Hello")
    await until(lambda: rig.playback.state is PlaybackState.PLAYING, 3.0)
    await flux.turn("StartOfTurn", "Hey")
    await until(lambda: rig.conv.interruptions == 1, 1.0)
    await rig.close()


async def test_empty_updates_between_turns_never_interrupt(flux):
    """Regression (seen live): Flux sends Update events ~every 0.25 s, also between turns, with an
    empty transcript. They must not cancel the reply."""
    async with FakeFluxTTS(frame_delay_s=0.005) as aura:
        oai = FakeOpenAI(
            [Script([("delay", 0.3), ("text", "Here is my answer, and it keeps going for a while. " * 2)])]
        )
        rig = Rig(flux, aura, oai, llm={"greeting": "Hello there, I am awake."})
        await ready(rig)
        await until(lambda: rig.playback.state is PlaybackState.PLAYING, 3.0, "greeting audible")
        await flux.turn("Update", "")  # between turns, while the greeting plays
        await asyncio.sleep(0.05)
        assert rig.conv.interruptions == 0
        await finished_speaking(rig)
        await flux.utter("hi")
        for _ in range(6):  # empty Updates while the LLM thinks and while the reply plays
            await flux.turn("Update", "")
            await asyncio.sleep(0.1)
        await finished_speaking(rig)
        assert rig.conv.interruptions == 0 and oai.aborted == 0
        assert "Here is my answer" in rig.assistant_text()
        await rig.close()


# ------------------------------------------------------------------ session time limit
async def test_session_stops_itself_at_the_time_limit_and_can_be_extended(flux, aura):
    rig = Rig(
        flux, aura, FakeOpenAI(), session={"max_minutes": 0.01, "extend_minutes": 0.01, "warn_minutes": 0.004}
    )
    await ready(rig)
    left = rig.conv.remaining_s()
    assert left is not None and 0.4 < left <= 0.6  # 0.6 s limit in this test (2 h by default)
    assert rig.conv.extend() > left + 0.3  # +0.6 s
    await until(lambda: any(isinstance(e, Notice) and "session ends in" in e.text for e in rig.events), 2.0)
    await until(lambda: rig.conv.phase is Phase.IDLE, 3.0, "automatic stop")
    assert rig.conv.ended_by_time_limit and not rig.conv.running
    assert rig.conv.remaining_s() is None and rig.conv.extend() is None
    assert any(isinstance(e, Notice) and "time limit reached" in e.text for e in rig.events)
    await until(lambda: flux.closed_by_client >= 1 or not flux.conns[-1].state.name == "OPEN", 2.0)
    await rig.close()


def test_default_session_limit_is_two_hours():
    from so101_voice.config import load_config

    s = load_config().session
    assert (s.max_minutes, s.extend_minutes) == (120, 120)

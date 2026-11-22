"""Offline Krog assembly and Flux v2 regression checks; mocks do not evaluate a real model's acting."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from so101_voice.character.persona import GESTURE_MEANINGS, build_instructions
from so101_voice.config import AppConfig, load_config
from so101_voice.cost import CostLedger, Prices
from so101_voice.llm.reply import instructions

from .fake_providers import FakeFlux, FakeFluxTTS, FakeOpenAI, Script, until
from .fakes import FakeSO101API
from .rig import Rig
from .test_character import Harness
from .test_providers import TtsProbe, make_tts

QUICK = {"session_start_reaction": None, "automatic_idles": False, "rest_on_stop": False}
CASES = json.loads((Path(__file__).parent / "fixtures/sol_character.json").read_text())


def test_flux_defaults_greeting_and_price_keep_existing_models_and_cap():
    cfg = load_config()
    assert cfg.tts.model == "flux-cliff-en" and cfg.tts.url.endswith("/v2/speak")
    assert cfg.tts.expressivity == 0 and cfg.tts.sample_rate == 24000
    assert cfg.audio.prebuffer_ms == 40 and cfg.audio.output_period_ms == 40
    assert cfg.stt.model == "flux-general-en" and cfg.llm.model == "gpt-6-luna"
    assert cfg.llm.greeting == "Hmph. There you are. I was getting bored."
    assert cfg.cost.prices.tts_usd_per_1k_chars == 0.045
    assert cfg.cost.max_session_estimated_usd == 0.25
    for override in (
        {"url": "wss://api.deepgram.com/v1/speak"},
        {"model": "aura-2-aries-en"},
        {"expressivity": 3},
        {"expressivity": 0.5},
        {"flush_limit": 20},
        {"max_connection_s": 3600},
    ):
        with pytest.raises(ValidationError):
            AppConfig.model_validate({"tts": override})


def test_config_precedence_preserves_intentional_prompt_and_identifies_source(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "default.toml").write_text('[llm]\nprompt="default override"\n')
    local = config / "local.toml"
    local.write_text('[llm]\nprompt="intentional local character"\n')
    extra = tmp_path / "custom.toml"
    extra.write_text('[llm]\nprompt="intentional extra character"\n')
    cfg = load_config(root=tmp_path)
    assert cfg.llm.prompt == "intentional local character"
    assert cfg.llm.prompt_source == str(local.resolve())
    cfg = load_config(extra, root=tmp_path)
    assert cfg.llm.prompt == "intentional extra character"
    assert cfg.llm.prompt_source == str(extra.resolve())


async def test_character_body_dynamic_library_tool_meanings_and_fallbacks(caplog):
    h = await Harness(rest_on_stop=False).start()
    try:
        text = build_instructions("Krog", h.library)
        assert "tiny cyber dragon" in text and "Small mood changes can be frequent" in text
        assert "fixed mandatory pairs" in text and "MOVING animations" in text
        assert "sparingly" not in text and "I'll try" not in instructions([])
        assert set(GESTURE_MEANINGS) == set(h.library.behaviors)
        catalog = {t["name"]: t["description"] for t in h.registry.action_catalog()}
        assert "intention and emotion" in catalog["play_robot_behavior"]
        assert "static pose" in catalog["go_to_robot_pose"]
        assert "not an ordinary emotional idle" in catalog["robot_go_rest"]
        h.library.behaviors.pop("high_five_offer")
        assert "high_five_offer" not in build_instructions("Krog", h.library)
        for library, gestures in ((None, True), (h.library, False)):
            fallback = build_instructions("Krog", library, gestures=gestures)
            assert "tiny cyber dragon" in fallback and "physical expression is unavailable" in fallback
        h.library.behaviors.clear()
        assert "physical expression is unavailable" in build_instructions("Krog", h.library)
    finally:
        await h.close()


async def test_fixed_sol_greeting_uses_flux_playback_without_an_llm_request():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI()
        rig = Rig(stt, tts, model, llm={"greeting": AppConfig().llm.greeting})
        try:
            assert await rig.start()
            await until(lambda: rig.conv._gen is None, 3)
            assert "".join(tts.speaks) == AppConfig().llm.greeting
            assert tts.flushes == 1 and not model.requests
            assert rig.playback.played_bytes > 0
        finally:
            await rig.close()


async def test_two_hour_companion_session_renews_tts_between_turns_without_replay():
    now = [0.0]
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI(
            [Script([("text", text)]) for text in ("First watch.", "Still here.", "Last watch.")]
        )
        rig = Rig(stt, tts, model, session={"max_minutes": 120})
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            assert rig.conv.tts is not None
            rig.conv.tts._clock = lambda: now[0]
            # Socket was opened before replacing its clock.
            await rig.conv.tts.connect()
            rig.conv.tts._connected_at = 0
            for i, moment in enumerate((0, 3600, 7199)):
                now[0] = moment
                await stt.utter(f"watch {i}")
                await until(lambda i=i: len(model.requests) == i + 1 and rig.conv._gen is None, 3)
                assert rig.conv._running
            assert rig.cfg.session.max_minutes == 120
            assert rig.conv.tts.connections == 3
            assert tts.speaks == ["First watch.", "Still here.", "Last watch."]
            assert tts.flushes == 3 and len(model.requests) == 3
        finally:
            await rig.close()


@pytest.mark.parametrize("origin", [0, 48000])
async def test_interrupt_uses_local_session_offset_and_drops_generated_unplayed_text(origin):
    played = [origin]
    heard = []
    async with FakeFluxTTS(frame_delay_s=0.005, stale_frames_after_interrupt=3) as server:
        probe = TtsProbe()
        tts = make_tts(
            server.url,
            probe,
            played_bytes=lambda: played[0],
            playback_latency_s=0.01,
            on_interrupted=lambda gen, text: heard.append((gen, text)),
        )
        try:
            await tts.connect()
            tts.begin(1)
            await tts.speak(1, "One short sentence. Followed by unheard words that continue for a long time.")
            await until(lambda: len(probe.audio) >= 3)
            played[0] = origin + 480 * 5 + 2  # fractional millisecond: round DOWN, never add heard audio
            tts.cancel(1)
            await tts.wait_interrupted()
            message = next(m for m in server.messages if m["type"] == "Interrupt")
            assert message["playback_offset"] == {"type": "time_ms", "value": 40}
            assert type(message["playback_offset"]["value"]) is int
            assert heard == [(1, "One ")]
            assert tts.discarded_audio_bytes > 0
            tts.begin(2)
            await tts.speak(2, "Next answer is also long enough for interruption.")
            await until(lambda: any(gen == 2 for gen, _, _ in probe.audio))
            played[0] += 480 * 2
            tts.cancel(2)
            await tts.wait_interrupted()
            offsets = [m["playback_offset"]["value"] for m in server.messages if m["type"] == "Interrupt"]
            assert offsets == [40, 60]  # never reset to zero at a new turn
        finally:
            await tts.aclose()


async def test_ignored_interrupt_warning_never_allows_stale_audio():
    async with FakeFluxTTS(ack_interrupt=False, frame_delay_s=0.01) as server:
        probe = TtsProbe()
        tts = make_tts(server.url, probe, interrupt_timeout_s=0.05)
        try:
            tts.begin(1)
            await tts.speak(1, "Unfinished speech.")
            await until(lambda: bool(probe.audio))
            tts.cancel(1)
            tts._message(json.dumps({"type": "Warning", "code": "NO_AUDIO_GENERATED"}))
            count = len(probe.audio)
            tts._audio(b"\x01\x02")
            assert len(probe.audio) == count
            tts.begin(2)
            await tts.speak(2, "Fresh turn.")
            assert tts.connections == 2
        finally:
            await tts.aclose()


def test_provider_cost_reports_replace_estimates_without_double_counting_or_free_cancellation():
    ledger = CostLedger(Prices())
    ledger.tts_sent(100)
    ledger.tts_sent(50)
    ledger.tts_discarded(50)
    ledger.tts_report("a", 100, 90)
    assert ledger.tts_usd() == pytest.approx(140 / 1000 * 0.045)
    ledger.tts_report("a", 100, 90)  # duplicate metadata/session summary
    assert ledger.tts_usd() == pytest.approx(140 / 1000 * 0.045)
    ledger.tts_report("b", 50, 50)
    assert ledger.tts.characters == 150 and ledger.tts.provider_billable_characters == 140
    assert ledger.tts_usd() == pytest.approx(140 / 1000 * 0.045)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_sol_scenario_mock_pipeline_has_one_response_and_only_intended_actions(case):
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        actions = [("tool", a["tool"], json.dumps(a["arguments"])) for a in case["actions"]]
        model = FakeOpenAI([Script([("text", case["speech"]), *actions])])
        seq = 0

        def receive(request):
            nonlocal seq
            seq += 1
            return httpx.Response(
                200,
                json={
                    "screen": {"available": False},
                    "face": {
                        "found": True,
                        "angle_deg": 15,
                        "camera_angle_deg": 0,
                        "confidence": 0.1,
                        "age_s": 0.01,
                        "seq": seq,
                        "session": "sol-eval",
                    },
                },
            )

        api = FakeSO101API(down=case["name"] == "robot_unavailable")
        rig = Rig(
            stt,
            tts,
            model,
            api=api,
            character=QUICK,
            perception={"enabled": True, "receiver_url": "http://receiver.test"},
            follow={"calibration_valid": True, "acquire_s": 0},
            perception_transport=httpx.MockTransport(receive),
        )
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await stt.utter(case["user"])
            await until(lambda: len(model.requests) == 1 and rig.conv._gen is None, 3)
            assert len(model.requests) == 1 and "tools" not in model.requests[0]
            assert "tiny cyber dragon" in model.requests[0]["instructions"]
            assert case["speech"] in rig.assistant_text()
            state = next(item for item in model.requests[0]["input"] if item.get("role") == "developer")
            world = json.loads(state["content"].split(": ", 1)[1])
            if case["name"] == "robot_unavailable":
                assert not world["robot_available"]
            if case["name"] in ("missing_screenshot", "watch_game"):
                assert not world["image_included"]
            if case["name"] == "watch_game":
                await until(lambda: not rig.companion.follow.requested, 2)
            assert not any(
                i.get("type") in ("function_call", "function_call_output") for i in rig.conv.history.items
            )
        finally:
            await rig.close()


async def test_fatal_tts_error_blocks_audio_and_next_reply_reconnects_without_replay():
    async with FakeFluxTTS(frame_delay_s=0.01) as server:
        probe = TtsProbe()
        tts = make_tts(server.url, probe)
        try:
            tts.begin(1)
            await tts.speak(1, "Old speech that must not replay after a provider error.")
            await until(lambda: bool(probe.audio))
            tts._message(json.dumps({"type": "Error", "code": "NET-0003", "description": "Session expired"}))
            count = len(probe.audio)
            tts._audio(b"\x01\x02")
            assert len(probe.audio) == count and len(probe.lost) == 1
            tts.begin(2)
            await tts.speak(2, "New speech.")
            await tts.flush(2, final=True)
            await until(lambda: any(turn.gen == 2 for turn in probe.done))
            assert tts.connections == 2
            assert server.speaks == ["Old speech that must not replay after a provider error.", "New speech."]
        finally:
            await tts.aclose()


async def test_intentional_persona_override_is_preserved_and_diagnosed_without_logging_its_contents(caplog):
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI([Script([("text", "Custom character reply.")])])
        rig = Rig(stt, tts, model, llm={"prompt": "An intentional private persona override."})
        try:
            expected = "Custom llm.prompt replaces built-in Krog persona"
            assert any(expected in r.message for r in caplog.records)
            assert not any("An intentional private persona override." in r.message for r in caplog.records)
            assert await rig.start()
            await stt.wait_connections(1)
            await stt.utter("Hello")
            await until(lambda: len(model.requests) == 1 and rig.conv._gen is None, 3)
            assert model.requests[0]["instructions"].startswith("An intentional private persona override.")
        finally:
            await rig.close()

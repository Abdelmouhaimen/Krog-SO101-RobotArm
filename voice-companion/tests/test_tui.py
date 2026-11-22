"""Offscreen smoke test of the terminal UI (A28): fake devices, fake providers, real app objects."""

from __future__ import annotations

from textual.widgets import RichLog, Static

from so101_voice.app import build_companion
from so101_voice.config import Secrets
from so101_voice.conversation import Phase
from so101_voice.ui.tui import CompanionTUI

from .fake_providers import DG_KEY, OA_KEY, FakeFlux, FakeFluxTTS, FakeOpenAI, say, until
from .fakes import FakeAudioIO
from .rig import make_config


async def test_tui_offscreen_conversation_with_cost_bar():
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        oai = FakeOpenAI([say("Hello from the fake model.")])
        audio = FakeAudioIO()
        cfg = make_config(flux, aura)

        def build(on_event, on_talk):
            return build_companion(
                cfg,
                Secrets(deepgram_api_key=DG_KEY, openai_api_key=OA_KEY, so101_api_token=None),
                on_event=on_event,
                on_talk_status=on_talk,
                use_robot=False,
                audio=audio,
                llm_transport=oai.transport(),
            )

        app = CompanionTUI(build)
        async with app.run_test() as pilot:
            await pilot.press("s")
            conv = app.companion.conversation
            await until(lambda: conv.phase is Phase.LISTENING, 3, "LISTENING")
            await flux.utter("hi there")
            for _ in range(200):  # drain the fake speaker
                audio.pump(480)
                await pilot.pause(0.01)
                if conv._gen is None and conv.generations_started == 1:
                    break
            log_text = "\n".join(str(line) for line in app.query_one(RichLog).lines)
            assert "hi there" in log_text and "Hello from the fake model." in log_text
            cost = str(app.query_one("#cost", Static).render())
            assert "est. $" in cost and "TTS $" in cost and "cap $0.25" in cost
            assert not app.query_one("#talk").display  # continuous mode: no push-to-talk button
            status = str(app.query_one("#status", Static).render())
            assert "gpt-6-luna" in status and "robot: off" in status
            timer = str(app.query_one("#timer", Static).render())
            assert timer[:6] in ("⏱ 1:59", "⏱ 2:00")  # 2 h countdown
            assert not app.query_one("#extend").disabled
            before = conv.remaining_s()
            await pilot.press("e")
            assert conv.remaining_s() > before + 7000  # +2 h
            assert str(app.query_one("#timer", Static).render())[:6] in ("⏱ 3:59", "⏱ 4:00")
            await pilot.press("m")
            assert conv.muted
            await pilot.press("x")
            await until(lambda: conv.phase is Phase.IDLE, 3, "IDLE")
            await pilot.press("q")


async def test_compact_readiness_updates_without_duplicate_checklists_or_log_spam():
    import httpx

    from so101_voice.conversation import Notice, ToolActivity
    from so101_voice.perception.lan import PerceptionClient

    from .fakes import TOKEN, FakeSO101API

    api = FakeSO101API()
    streams = {"available": True}
    oai = FakeOpenAI()

    def receiver(request):
        return httpx.Response(
            200,
            json={
                "screen": {"available": streams["available"], "age_s": 0.1},
                "face": {
                    "found": streams["available"],
                    "age_s": 0.1,
                    "angle_deg": 12,
                    "camera_angle_deg": 2,
                    "confidence": 0.9,
                },
            },
        )

    client = PerceptionClient("http://receiver.test", "test-token", transport=httpx.MockTransport(receiver))
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        cfg = make_config(flux, aura)

        def build(on_event, on_talk):
            comp = build_companion(
                cfg,
                Secrets(deepgram_api_key=DG_KEY, openai_api_key=OA_KEY, so101_api_token=TOKEN),
                on_event=on_event,
                on_talk_status=on_talk,
                audio=FakeAudioIO(),
                robot=api.client(),
                llm_transport=oai.transport(),
            )
            comp.readiness.perception = client
            return comp

        app = CompanionTUI(build)
        try:
            async with app.run_test() as pilot:
                ready = app.companion.readiness
                await ready.aclose()  # Drive service changes deterministically below.

                def row():
                    return str(app.query_one("#readiness", Static).render()).replace("\n", " | ")

                def log_text():
                    return "\n".join(str(line) for line in app.query_one(RichLog).lines)

                assert row() == (
                    "Arm: Connected | Screen feed: Available | Face angle: Available | Follow: OFF"
                )
                assert not any(
                    word in row().lower()
                    for word in ("calibration", "credential", "authentication", "provider", "audio")
                )
                assert "Robot API reachable" not in log_text()
                assert "Voice credentials" not in log_text()
                baseline = len(app.query_one(RichLog).lines)

                streams["available"] = False
                await ready.refresh()
                app._render_readiness()
                assert row() == (
                    "Arm: Connected | Screen feed: Unavailable | Face angle: Unavailable | Follow: OFF"
                )
                api.fault = "test fault"
                await ready.refresh()
                app._render_readiness()
                assert "Arm: Fault" in row()
                api.down = True
                await ready.refresh()
                app._render_readiness()
                assert "Arm: Disconnected" in row()
                api.down, api.fault, streams["available"] = False, None, True
                await ready.refresh()
                app._render_readiness()
                assert row() == (
                    "Arm: Connected | Screen feed: Available | Face angle: Available | Follow: OFF"
                )
                for _ in range(5):
                    app._render_readiness()
                    app.on_session_event(Notice("info", ready.text()))
                assert len(app.query_one(RichLog).lines) == baseline

                from types import SimpleNamespace

                original_follow = app.companion.follow
                try:
                    for status in ("SEARCHING", "TRACKING", "FACE LOST", "OFF"):
                        app.companion.follow = SimpleNamespace(status=status)
                        app._render_readiness()
                        assert f"Follow: {status}" in row()
                        assert len(app.query_one(RichLog).lines) == baseline
                    app.companion.follow = SimpleNamespace(status="FACE LOST")
                    for width in (80, 60, 100):
                        await pilot.resize_terminal(width, 30)
                        await pilot.pause()
                        app._render_readiness()
                        await pilot.pause()
                        widget = app.query_one("#readiness", Static)
                        lines = str(widget.render()).splitlines()
                        assert "Follow: FACE LOST" in row()
                        assert all(len(line) <= widget.content_size.width for line in lines)
                        assert widget.content_size.height >= len(lines)
                        assert len(app.query_one(RichLog).lines) == baseline
                    assert "\n" not in row()  # wide windows keep the compact single row
                finally:
                    app.companion.follow = original_follow
                    app._render_readiness()

                app.on_session_event(Notice("error", "Actual test error"))
                app.on_session_event(ToolActivity("get_latest_screen", "test-call", "ok"))
                assert "Actual test error" in log_text() and "tool get_latest_screen: ok" in log_text()
                assert not api.motion_posts and not api.talk_calls and not oai.requests
                await pilot.press("q")
        finally:
            await client.aclose()

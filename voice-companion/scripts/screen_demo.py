"""Offline Screen UI/vision demonstration: fake providers, receiver, audio and robot only."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from textual.widgets import Button, Static  # noqa: E402

from so101_voice.app import build_companion  # noqa: E402
from so101_voice.config import Secrets  # noqa: E402
from so101_voice.ui.tui import CompanionTUI  # noqa: E402
from tests.fake_providers import DG_KEY, OA_KEY, FakeFlux, FakeFluxTTS, FakeOpenAI, Script, say, until  # noqa: E402
from tests.fakes import TOKEN, FakeAudioIO, FakeSO101API  # noqa: E402
from tests.rig import make_config  # noqa: E402
from tests.test_screen_observation import QUICK, Receiver, images  # noqa: E402


async def run(output: Path) -> dict[str, object]:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI(
            [Script([], observation="A menu is visible."), say("Hello."), say("Here with you.")]
        )
        api, audio, receiver = FakeSO101API(), FakeAudioIO(), Receiver()
        cfg = make_config(
            stt,
            tts,
            character=QUICK,
            screen={"interval_s": 0.08},
            perception={"enabled": True, "receiver_url": "http://receiver.test"},
        )

        def build(events, talk):
            return build_companion(
                cfg,
                Secrets(
                    deepgram_api_key=DG_KEY,
                    openai_api_key=OA_KEY,
                    so101_api_token=TOKEN,
                    bridge_token="test-bridge",
                ),
                on_event=events,
                on_talk_status=talk,
                audio=audio,
                robot=api.client(),
                llm_transport=model.transport(),
                perception_transport=httpx.MockTransport(receiver),
                follow_transport=api.transport(),
            )

        async def pump():
            while True:
                if audio.buffer is not None:
                    audio.pump(480)
                await asyncio.sleep(0.01)

        pumping = asyncio.create_task(pump())
        app = CompanionTUI(build)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                conv = app.companion.conversation
                initial = str(app.query_one("#screen-toggle", Button).label)
                assert initial == "Screen: OFF" and not model.requests
                await pilot.click("#screen-toggle")
                assert str(app.query_one("#screen-toggle", Button).label) == "Screen: ON"
                assert not model.requests  # toggling before Start incurs no provider use
                await pilot.press("s")
                await stt.wait_connections(1)
                await until(lambda: conv.screen_observations == 1 and conv._gen is None, 3)
                assert not tts.speaks and not api.talk_active()
                cfg.screen.interval_s = 3
                conv._defer_observation()
                await pilot.pause()
                app.save_screenshot("screen-on.svg", str(output))
                receiver.available = False
                await app.companion.readiness.refresh()
                app._render_readiness()
                assert str(app.query_one("#screen-toggle", Button).label) == "Screen: UNAVAILABLE"
                app.save_screenshot("screen-unavailable.svg", str(output))
                receiver.available = True
                await app.companion.readiness.refresh()
                await pilot.press("v")
                assert str(app.query_one("#screen-toggle", Button).label) == "Screen: OFF"
                await pilot.pause(0.2)
                assert len(model.requests) == 1
                app.save_screenshot("screen-off.svg", str(output))
                await stt.utter("Hello")
                await until(lambda: len(model.requests) == 2 and conv._gen is None, 3)
                receiver.seq += 1
                follow_before = app.companion.follow.requested
                await pilot.press("v")
                await stt.utter("And now?")
                await until(lambda: len(model.requests) == 3 and conv._gen is None, 3)
                assert app.companion.follow.requested == follow_before
                report = {
                    "source": "offline fake providers / fake receiver / fake arm / fake audio",
                    "startup": initial,
                    "readiness": str(app.query_one("#readiness", Static).render()),
                    "initial_toggle_no_requests": True,
                    "interval_test_s": 0.08,
                    "production_interval_s": 3,
                    "image_counts_background_then_OFF_then_ON": [
                        len(images(body)) for body in model.requests
                    ],
                    "silent_observation_no_tts_or_talk": True,
                    "follow_unchanged_by_toggle": True,
                    "background_requests": conv.screen_observations,
                    "total_model_requests": len(model.requests),
                    "no_image_history": "data:image" not in json.dumps(conv.history.items),
                    "paid_requests": 0,
                    "physical_robot_movements": 0,
                }
                assert report["image_counts_background_then_OFF_then_ON"] == [1, 0, 1]
                await pilot.press("q")
        finally:
            pumping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pumping
        await asyncio.to_thread((output / "report.json").write_text, json.dumps(report, indent=2) + "\n")
        return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(Path("docs/artifacts/screen-observation"))), indent=2))

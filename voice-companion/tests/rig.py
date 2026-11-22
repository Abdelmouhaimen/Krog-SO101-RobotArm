"""End-to-end rig: the real application objects wired to local fakes (providers, audio, robot)."""

from __future__ import annotations

import asyncio
from typing import Any

from so101_voice.app import Companion, build_companion
from so101_voice.config import AppConfig, Secrets
from so101_voice.conversation import Phase, PhaseChanged, Transcript, UIEvent

from .fake_providers import DG_KEY, OA_KEY, FakeFlux, FakeFluxTTS, FakeOpenAI
from .fakes import TOKEN, FakeAudioIO, FakeSO101API

RATE_IN, RATE_OUT = 16000, 24000


def make_config(flux: FakeFlux, aura: FakeFluxTTS, **over: Any) -> AppConfig:
    data: dict[str, Any] = {
        "log_file": None,
        "stt": {
            "url": flux.url,
            "reconnect_initial_s": 0.05,
            "reconnect_max_s": 0.1,
            "reconnect_max_attempts": 3,
        },
        "llm": {"base_url": "http://openai.test/v1", "greeting": None},
        "tts": {"url": aura.url, "interrupt_timeout_s": 0.5},
        "audio": {"prebuffer_ms": 20},
        "robot": {"lease_s": 0.5, "renew_interval_s": 0.15, "stop_grace_s": 0.05, "retry_interval_s": 0.2},
        "cost": {"ledger_file": None},
    }
    for section, values in over.items():
        if isinstance(values, dict):
            data.setdefault(section, {}).update(values)
        else:
            data[section] = values
    return AppConfig.model_validate(data)


class Rig:
    def __init__(
        self,
        flux: FakeFlux,
        aura: FakeFluxTTS,
        oai: FakeOpenAI,
        *,
        api: FakeSO101API | None = None,
        audio_io: FakeAudioIO | None = None,
        perception_transport: Any = None,
        **cfg_over: Any,
    ) -> None:
        self.flux, self.aura, self.oai = flux, aura, oai
        self.api = api
        self.audio = audio_io or FakeAudioIO()
        self.events: list[UIEvent] = []
        self.cfg = make_config(flux, aura, **cfg_over)
        self.companion: Companion = build_companion(
            self.cfg,
            Secrets(
                deepgram_api_key=DG_KEY,
                openai_api_key=OA_KEY,
                so101_api_token=TOKEN if api else None,
                bridge_token="test-bridge" if perception_transport else None,
            ),
            on_event=self.events.append,
            use_robot=api is not None,
            robot=api.client() if api else None,
            audio=self.audio,
            llm_transport=oai.transport(),
            perception_transport=perception_transport,
            follow_transport=api.transport() if api else None,
        )
        self.conv = self.companion.conversation
        self.ledger = self.companion.ledger
        self.motion = self.companion.motion
        self.playback = self.conv.playback
        self._pump: asyncio.Task[None] | None = None
        self.checked_gens = 0

    async def start(self, *, pump: bool = True, speed: float = 1.0) -> bool:
        await self.companion.startup()
        ok = await self.conv.start()
        if pump:
            self.start_pump(speed)
        return ok

    def start_pump(self, speed: float = 1.0) -> None:
        """The speaker: pull 20 ms every 20/speed ms, like the device callback."""

        async def pump() -> None:
            while True:
                self.audio.pump(RATE_OUT // 50)
                await asyncio.sleep(0.02 / speed)

        self._pump = asyncio.get_running_loop().create_task(pump())

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
        await self.companion.shutdown()

    async def mic(self, seconds: float, value: int = 0) -> None:
        """Push ``seconds`` of microphone audio as 20 ms device callbacks (processed in order)."""
        frame = int(value).to_bytes(2, "little", signed=True) * (RATE_IN // 50)
        for _ in range(round(seconds * 50)):
            self.audio.speak(frame)
            await asyncio.sleep(0)

    def phases(self) -> list[Phase]:
        return [e.phase for e in self.events if isinstance(e, PhaseChanged)]

    def transcripts(self, role: str | None = None, final: bool | None = True) -> list[str]:
        return [
            e.text
            for e in self.events
            if isinstance(e, Transcript)
            and (role is None or e.role == role)
            and (final is None or e.final == final)
        ]

    def assistant_text(self) -> str:
        return " ".join(self.transcripts("assistant", final=False))

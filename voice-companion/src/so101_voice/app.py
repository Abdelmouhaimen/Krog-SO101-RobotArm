"""Composition root: builds the pipeline, robot link, tools and cost ledger; owns shutdown order."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from so101_voice.audio.aec import (
    SOURCE_NODE,
    AECNodes,
    AECSettings,
    AECUnavailable,
    PipeWireAEC,
    list_audio_nodes,
    resolve_node,
)
from so101_voice.audio.devices import AudioIO, MiniaudioIO, list_devices
from so101_voice.audio.playback import PlaybackBuffer
from so101_voice.character.director import BehaviorDirector
from so101_voice.character.library import BehaviorLibrary
from so101_voice.character.persona import EXPECTED_BEHAVIORS, build_instructions
from so101_voice.config import REPO_ROOT, AppConfig, ConfigError, Secrets
from so101_voice.conversation import ConversationCoordinator, Notice, PhaseChanged, UIEvent
from so101_voice.cost import CostLedger, CostSnapshot
from so101_voice.llm.responses import ResponsesClient
from so101_voice.motion.speech_motion import SpeechMotionCoordinator, TalkStatus
from so101_voice.perception.follow import FollowController
from so101_voice.perception.interfaces import NullPerceptionProvider, PerceptionProvider
from so101_voice.perception.lan import PerceptionClient
from so101_voice.readiness import Check, Readiness
from so101_voice.robot.client import RobotAPIClient
from so101_voice.robot.follow import FollowAPI
from so101_voice.robot.motion import RobotMotion
from so101_voice.stt.flux import FluxSTT
from so101_voice.tools.builtin import register_builtin_tools
from so101_voice.tools.perception_tools import INSTRUCTIONS, register_perception_tools
from so101_voice.tools.registry import ToolRegistry
from so101_voice.tools.robot_tools import register_robot_tools
from so101_voice.tts.flux import FluxTTS
from so101_voice.turn_context import WorldContext

log = logging.getLogger(__name__)

__all__ = ["Companion", "ConfigError", "build_companion", "run_plain"]


@dataclass
class Companion:
    cfg: AppConfig
    conversation: ConversationCoordinator
    motion: SpeechMotionCoordinator
    robot: RobotAPIClient | None
    tools: ToolRegistry
    ledger: CostLedger
    llm: ResponsesClient
    perception: PerceptionProvider | PerceptionClient
    follow: FollowController | None = None
    readiness: Readiness | None = None
    aec: PipeWireAEC | None = None
    mic_io: MiniaudioIO | None = None  # the real device I/O (None when a fake is injected)
    director: BehaviorDirector | None = None  # arm behaviors (M3B); None: no robot or tools disabled

    async def startup(self) -> None:
        await self.prepare_audio()
        self.motion.start()
        if self.follow is not None:
            self.follow.start()
        if self.director is not None:
            await self.director.start()
        if self.readiness is not None:
            self.readiness.audio = await self.check_audio()
            await self.readiness.start()
            log.info("Startup checklist:\n%s", self.readiness.text())
            self.conversation._notice("info", self.readiness.text())

    async def check_audio(self) -> Check:
        if self.mic_io is None:
            return Check("OK", "Audio input/output supplied by injected device interface")
        if self.conversation.blocked:
            return Check("Error", "Audio preparation blocked: " + self.conversation.blocked)
        try:
            devices = await asyncio.to_thread(list_devices)
            for kind, wanted in (("input", self.mic_io.input_device), ("output", self.mic_io.output_device)):
                choices = [d for d in devices if d.kind == kind]
                if not choices:
                    return Check("Unavailable", f"No audio {kind} device available")
                if wanted not in (None, "", "default"):
                    if isinstance(wanted, int) or str(wanted).isdigit():
                        present = any(d.index == int(wanted) for d in choices)
                    else:
                        present = any(str(wanted).lower() in d.name.lower() for d in choices)
                    if not present:
                        return Check("Unavailable", f"Configured audio {kind} device unavailable")
            return Check("OK", "Audio input/output devices available; capture opens on Start")
        except Exception:
            return Check("Error", "Audio device enumeration failed")

    async def before_session(self) -> None:
        await self.prepare_audio()
        if self.readiness is not None:
            self.readiness.audio = await self.check_audio()
            await self.readiness.refresh()

    async def prepare_audio(self) -> None:
        """Start echo cancellation (mode ``aec``) and point the devices at it; apply ``on_failure``."""
        conv = self.conversation
        a = self.cfg.audio
        if a.mode != "aec":
            conv.audio_mode = a.mode
            if a.mode == "raw":
                conv._notice(
                    "warning", "audio mode RAW: no echo cancellation — the assistant can hear itself"
                )
            else:
                conv._notice(
                    "warning", "audio mode HALF-DUPLEX: you cannot interrupt by voice while it speaks"
                )
            return
        if self.aec is None:
            return
        if self.aec.running:
            present = await asyncio.to_thread(lambda: any(n.name == SOURCE_NODE for n in list_audio_nodes()))
            if present:
                return
            # The audio service was restarted under us: our echo-cancel nodes are gone. Recreate them.
            log.warning("echo-cancel nodes disappeared (audio service restarted?): restarting AEC")
            conv._notice("warning", "audio service restarted: recreating echo cancellation")
            await asyncio.to_thread(self.aec.stop)
        try:
            nodes = await asyncio.to_thread(self._start_aec)
        except AECUnavailable as e:
            policy = a.aec.on_failure
            log.error("echo cancellation unavailable: %s", e)
            if policy == "error":
                conv.blocked = (
                    f"echo cancellation unavailable: {e}. Fix PipeWire AEC (scripts/aec_check.py), or set "
                    "[audio] mode / [audio.aec] on_failure to 'raw' or 'half_duplex' explicitly"
                )
                conv._notice("error", conv.blocked)
                return
            conv.audio_mode = policy
            conv.half_duplex = policy == "half_duplex"
            conv._notice(
                "warning", f"echo cancellation unavailable ({e}); running {policy.upper()} as configured"
            )
            return
        if self.mic_io is not None:
            self.mic_io.input_device = nodes.source_description
            if nodes.sink_description is not None:
                self.mic_io.output_device = nodes.sink_description
        conv.audio_mode = "aec"
        conv._notice("info", f"echo cancellation on (PipeWire WebRTC, {nodes.topology} reference)")

    def _start_aec(self) -> AECNodes:
        assert self.aec is not None
        a = self.cfg.audio.aec
        if a.mic or a.speaker:
            nodes = list_audio_nodes()
            self.aec.settings = dataclasses.replace(
                self.aec.settings,
                mic_node=resolve_node(a.mic, "Audio/Source", nodes),
                speaker_node=resolve_node(a.speaker, "Audio/Sink", nodes),
            )
        return self.aec.start(a.start_timeout_s)

    async def shutdown(self) -> None:
        """Order matters: silence + Talk OFF first, then close the robot link and HTTP clients."""
        if self.follow is not None:
            self.follow.on_phase("idle")
        if self.readiness is not None:
            await self.readiness.aclose()
        with contextlib.suppress(Exception):
            await self.conversation.stop("app exit")
        if self.follow is not None:
            with contextlib.suppress(Exception):
                await self.follow.aclose()
        if self.director is not None:  # idles loop forever: rest the arm it animated (best effort)
            with contextlib.suppress(Exception):
                await self.director.aclose()
        if self.aec is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.aec.stop)
        with contextlib.suppress(Exception):
            await self.motion.aclose(timeout_s=1.0)
        if self.robot is not None:
            with contextlib.suppress(Exception):
                await self.robot.aclose()
        with contextlib.suppress(Exception):
            await self.llm.aclose()
        with contextlib.suppress(Exception):
            await self.perception.aclose()


def build_companion(
    cfg: AppConfig,
    secrets: Secrets,
    *,
    on_event: Callable[[UIEvent], None],
    on_talk_status: Callable[[TalkStatus], None] | None = None,
    use_robot: bool = True,
    audio: AudioIO | None = None,
    robot: RobotAPIClient | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    perception_transport: httpx.AsyncBaseTransport | None = None,
    follow_transport: httpx.AsyncBaseTransport | None = None,
    stt_connector: Any = None,
    tts_connector: Any = None,
    root: Path = REPO_ROOT,
    aec: PipeWireAEC | None = None,
) -> Companion:
    """Fakes can be injected for tests (``audio``, ``robot``, ``llm_transport``, ``*_connector``)."""
    if not secrets.deepgram_api_key:
        raise ConfigError("DEEPGRAM_API_KEY is not set (put it in .env or the environment)")
    if not secrets.openai_api_key:
        raise ConfigError("OPENAI_API_KEY is not set (put it in .env or the environment)")
    dg_key, oa_key = secrets.deepgram_api_key, secrets.openai_api_key

    if robot is None and use_robot and cfg.robot.enabled:
        if secrets.so101_api_token:
            robot = RobotAPIClient(cfg.robot.base_url, secrets.so101_api_token, timeout_s=cfg.robot.timeout_s)
        else:
            on_event(Notice("warning", "SO101_API_TOKEN not set: robot animation disabled"))
    if not use_robot:
        robot = None

    motion = SpeechMotionCoordinator(
        robot,
        intensity=cfg.robot.talk_intensity,
        lease_s=cfg.robot.lease_s,
        renew_interval_s=cfg.robot.renew_interval_s,
        stop_grace_s=cfg.robot.stop_grace_s,
        retry_interval_s=cfg.robot.retry_interval_s,
        on_status=on_talk_status,
    )
    mic_io: MiniaudioIO | None = None
    if audio is None:
        aec_mode = cfg.audio.mode == "aec"
        mic_io = MiniaudioIO(
            input_rate=cfg.stt.sample_rate,
            output_rate=cfg.tts.sample_rate,
            # aec: switched to the echo-cancelled nodes once AEC runs (Companion.prepare_audio)
            input_device=None if aec_mode else cfg.audio.input_device,
            output_device=cfg.audio.aec.speaker if aec_mode else cfg.audio.output_device,
            input_period_ms=cfg.audio.input_period_ms,
            output_period_ms=cfg.audio.output_period_ms,
        )
        audio = mic_io
        if aec is None and aec_mode:
            x = cfg.audio.aec
            aec = PipeWireAEC(
                AECSettings(
                    topology=x.topology,
                    noise_suppression=x.noise_suppression,
                    gain_control=x.gain_control,
                    high_pass_filter=x.high_pass_filter,
                )
            )
    latency = (
        cfg.audio.output_latency_ms / 1000
        if cfg.audio.output_latency_ms is not None
        else audio.output_latency_s
    )
    playback = PlaybackBuffer(
        cfg.tts.sample_rate, prebuffer_s=cfg.audio.prebuffer_ms / 1000, device_latency_s=latency
    )
    # Robot behaviors (M3B): only with a robot link and [robot_tools] enabled; otherwise voice only.
    director: BehaviorDirector | None = None
    library: BehaviorLibrary | None = None
    notices: dict[str, Callable[[str, str], None]] = {}
    if robot is not None and cfg.robot_tools.enabled:
        library = BehaviorLibrary(robot, expected=EXPECTED_BEHAVIORS)
        director = BehaviorDirector(
            robot,
            RobotMotion(robot),
            library,
            cfg.character,
            cfg.robot_tools,
            on_notice=lambda level, text: notices["fn"](level, text) if "fn" in notices else None,
        )
    tools = ToolRegistry(allow_physical_tools=director is not None or cfg.perception.enabled)
    register_builtin_tools(tools)
    gestures = director is not None and library is not None and cfg.character.reactive_gestures
    if gestures:
        assert director is not None and library is not None
        register_robot_tools(tools, director, library)

    tracker = None
    lan = None
    if cfg.perception.enabled:
        lan = PerceptionClient(
            cfg.perception.receiver_url,
            secrets.bridge_token,
            timeout_s=cfg.perception.timeout_s,
            transport=perception_transport,
        )
        follow_api = None
        if robot is not None and secrets.so101_api_token:
            follow_api = FollowAPI(
                RobotAPIClient(
                    robot.base_url,
                    secrets.so101_api_token,
                    timeout_s=cfg.perception.timeout_s,
                    transport=follow_transport,
                )
            )
        tracker = FollowController(
            lan, follow_api, cfg.follow, director, notice=lambda level, text: notices["fn"](level, text)
        )
        if director is not None:
            director.before_exclusive_motion = tracker.release_for_motion
        register_perception_tools(tools, lan, tracker)
    readiness = Readiness(
        cfg,
        robot,
        lan,
        credentials=(bool(secrets.deepgram_api_key), bool(secrets.openai_api_key)),
        audio=Check("Waiting", "Audio input/output not checked yet"),
    )

    log.info("Effective character prompt source: %s", cfg.llm.prompt_source)
    if cfg.llm.prompt:
        log.warning("Custom llm.prompt replaces built-in Krog persona: %s", cfg.llm.prompt_source)

    def instructions() -> str:
        prompt = cfg.llm.prompt or build_instructions(cfg.character.name, library, gestures=gestures)
        if lan is not None:
            prompt += INSTRUCTIONS
        else:
            prompt += (
                f"\nNo live perception is available. Stay {cfg.character.name}; "
                "do not claim to see the game or human."
            )
        return prompt

    def events(ev: UIEvent) -> None:
        if tracker is not None and isinstance(ev, PhaseChanged):
            tracker.on_phase(ev.phase.value)
        if director is not None and isinstance(ev, PhaseChanged):
            director.on_phase(ev.phase.value)
        on_event(ev)

    holder: dict[str, ConversationCoordinator] = {}

    def on_cost(snap: CostSnapshot) -> None:
        conv = holder.get("conv")
        if conv is not None:
            conv.on_cost(snap)

    c = cfg.cost
    ledger = CostLedger(
        c.prices,
        enabled=c.enabled,
        max_session_usd=c.max_session_estimated_usd,
        warn_at_fraction=c.warn_at_fraction,
        stt_keyterms=bool(cfg.stt.keyterms),
        ledger_file=None if c.ledger_file is None else root / c.ledger_file,
        on_change=on_cost,
    )
    llm = ResponsesClient(
        api_key=oa_key,
        base_url=cfg.llm.base_url,
        timeout_s=cfg.llm.request_timeout_s,
        transport=llm_transport,
    )

    def new_stt(**callbacks: Any) -> FluxSTT:
        s = cfg.stt
        return FluxSTT(
            url=s.url,
            api_key=dg_key,
            model=s.model,
            sample_rate=s.sample_rate,
            eot_threshold=s.eot_threshold,
            eot_timeout_ms=s.eot_timeout_ms,
            keyterms=s.keyterms,
            chunk_ms=s.send_chunk_ms,
            ledger=ledger,
            connect_timeout_s=s.connect_timeout_s,
            reconnect_initial_s=s.reconnect_initial_s,
            reconnect_max_s=s.reconnect_max_s,
            reconnect_max_attempts=s.reconnect_max_attempts,
            connector=stt_connector,
            **callbacks,
        )

    def new_tts(**callbacks: Any) -> FluxTTS:
        t = cfg.tts
        return FluxTTS(
            url=t.url,
            api_key=dg_key,
            model=t.model,
            sample_rate=t.sample_rate,
            ledger=ledger,
            connect_timeout_s=t.connect_timeout_s,
            interrupt_timeout_s=t.interrupt_timeout_s,
            max_connection_s=t.max_connection_s,
            expressivity=t.expressivity,
            played_bytes=lambda: playback.played_bytes,
            playback_latency_s=latency,
            connector=tts_connector,
            **callbacks,
        )

    conv = ConversationCoordinator(
        cfg,
        stt_factory=new_stt,
        tts_factory=new_tts,
        llm=llm,
        audio=audio,
        playback=playback,
        motion=motion,
        tools=tools,
        ledger=ledger,
        on_event=events,
        instructions=instructions,
        prepare_turn=WorldContext(robot, lan, library, cfg),
    )
    holder["conv"] = conv
    notices["fn"] = conv._notice

    def provider_status() -> Check:
        if not conv.running:
            return Check(
                "Waiting",
                "Providers idle — press Start to connect voice; OpenAI is verified on the first reply",
            )
        if conv.last_error:
            return Check("Error", "Voice session error — see the conversation log")
        stt_connected = conv.stt is not None and conv.stt.connected
        tts_connected = conv.tts is not None and conv.tts.connected
        llm_verified = ledger.llm.requests > ledger.llm.estimated_requests
        return Check(
            "OK" if tts_connected and llm_verified else "Waiting",
            f"STT {'connected' if stt_connected else 'idle/connecting'}; "
            f"TTS {'connected' if tts_connected else 'connecting'}; "
            f"OpenAI {'response verified' if llm_verified else 'waiting for first reply'}",
        )

    readiness.provider_check = provider_status
    companion = Companion(
        cfg,
        conv,
        motion,
        robot,
        tools,
        ledger,
        llm,
        lan if lan is not None else NullPerceptionProvider(),
        follow=tracker,
        readiness=readiness,
        aec=aec,
        mic_io=mic_io,
        director=director,
    )
    conv.before_start = companion.before_session  # re-checked on every Start (audio service restarts)
    return companion


async def run_plain(companion: Companion, *, duration_s: float | None = None) -> int:
    """Console mode: start at once, print events, Ctrl+C to quit."""
    await companion.startup()
    if not await companion.conversation.start():
        await companion.shutdown()
        return 2
    try:
        if duration_s is None:
            while companion.conversation.running and companion.conversation.last_error is None:
                await asyncio.sleep(0.2)
        else:
            await asyncio.sleep(duration_s)
    except asyncio.CancelledError:
        pass
    finally:
        await companion.shutdown()
    return 0 if companion.conversation.last_error is None else 1

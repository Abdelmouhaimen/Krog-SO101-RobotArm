"""ConversationCoordinator: the one authority over a voice conversation.

    microphone → MicGate → FluxSTT ─EndOfTurn→ Responses (gpt-6-luna) ─text→ TextChunker → FluxTTS
          → PlaybackBuffer → speaker ──(real playback state)──→ SpeechMotionCoordinator (Talk Mode)

Threads: miniaudio calls back on its own threads; those callbacks only touch the thread-safe
``PlaybackBuffer`` or hop onto the asyncio loop (``call_soon_threadsafe``). Everything else runs on
the loop, so the state below needs no locks.

**Turns and generations.** Every assistant response is a *generation* with a unique id. Flux
``EndOfTurn`` (deduplicated per connection and ``turn_index``) starts exactly one generation. Every
asynchronous result (LLM delta, TTS audio, ``Flushed``, playback marker) carries its generation id and
is dropped unless it belongs to the current one, so nothing from an old turn can act later.

**Barge-in** (Flux ``StartOfTurn``, or a push-to-talk press, while a generation is active):
1. invalidate the generation;
2. flush local playback;
3. Talk OFF;
4. cancel TTS (local drop, then ``Interrupt``);
5. cancel the LLM stream;
6. cancel turn-bound tools;
7. record only the text whose audio had started playing;
8. meter the discarded TTS characters (still billed).

**Speech output.** Natural phrases are streamed as Speak messages. Flux starts audio as text arrives;
one final Flush ends the turn. SpeechMetadata ends synthesis, while local playback determines
SPEAKING and Talk Mode. SpeechInterrupted reconciles heard text only from a local playback offset.

**Budget.** The ledger meters every provider. At the session cap, no new paid request starts and
active work is cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import enum
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from so101_voice.audio.devices import AudioDeviceError, AudioIO
from so101_voice.audio.gate import InputMode, MicGate
from so101_voice.audio.playback import PlaybackBuffer, PlaybackEvent, PlaybackMarker, PlaybackState
from so101_voice.choreography.scheduler import SegmentTag
from so101_voice.config import AppConfig
from so101_voice.cost import BudgetState, CostLedger, CostSnapshot
from so101_voice.llm.history import ConversationHistory
from so101_voice.llm.reply import (
    OBSERVATION_INSTRUCTIONS,
    Action,
    ObservationReply,
    Reply,
    SpeechStream,
    response_format,
)
from so101_voice.llm.reply import instructions as reply_instructions
from so101_voice.llm.responses import (
    Finished,
    LLMError,
    LLMEvent,
    TextDelta,
    ToolCallItem,
    build_request,
    estimate_tokens,
)
from so101_voice.motion.speech_motion import SpeechMotionCoordinator
from so101_voice.stt.flux import FluxSTT, TurnEvent
from so101_voice.tools.registry import FunctionCall, ToolRegistry
from so101_voice.tts.chunker import TextChunker
from so101_voice.tts.flux import FluxTTS, SpeechTurn, TTSError
from so101_voice.turn_context import TurnContext, WorldContext, requests_screen

log = logging.getLogger(__name__)
trace = logging.getLogger("so101_voice.trace")

_WORD = re.compile(r"[a-z0-9']+")


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("’", "'"))


class Phase(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    THINKING = "thinking"
    SPEAKING = "speaking"  # assistant audio is actually playing
    INTERRUPTED = "interrupted"
    ERROR = "error"


# ---------------------------------------------------------------------------------------- UI events
@dataclass(frozen=True)
class PhaseChanged:
    phase: Phase


@dataclass(frozen=True)
class Transcript:
    role: str  # "user" | "assistant"
    text: str
    final: bool = True


@dataclass(frozen=True)
class Notice:
    level: str  # "info" | "warning" | "error"
    text: str


@dataclass(frozen=True)
class TurnLatency:
    """Seconds, measured locally. ``stt_eot_s``: EndOfTurn receipt - the turn's last audio sent;
    the others are from EndOfTurn receipt to: first LLM text, first TTS audio, playback start."""

    gen: int
    stt_eot_s: float | None
    llm_first_text_s: float | None
    tts_first_audio_s: float | None
    playback_start_s: float | None


@dataclass(frozen=True)
class ToolActivity:
    name: str
    call_id: str
    status: str  # "started" | "ok" | "error" | "duplicate"


@dataclass(frozen=True)
class CostUpdate:
    snapshot: CostSnapshot


UIEvent = PhaseChanged | Transcript | Notice | TurnLatency | ToolActivity | CostUpdate


class LLMClient(Protocol):
    def stream(self, body: dict[str, Any]) -> AsyncIterator[LLMEvent]: ...


SttFactory = Callable[..., FluxSTT]  # (on_turn=, on_state=) -> FluxSTT
TtsFactory = Callable[..., FluxTTS]  # (on_audio=, on_group_done=, on_notice=, on_lost=) -> FluxTTS


@dataclass
class _Gen:
    id: int
    t_start: float
    stt_eot_s: float | None = None
    is_greeting: bool = False
    observation: TurnContext | None = None
    chunker: TextChunker | None = None
    groups: dict[int, str] = field(default_factory=dict)  # speech turn -> submitted text
    started_groups: set[int] = field(default_factory=set)  # playback reached these groups
    group: int = 0  # current (unflushed) group index
    text: str = ""
    user_text: str = ""
    llm_done: bool = False
    audio_done: bool = False  # SpeechMetadata received
    marked_group: int = -1
    first_text_t: float | None = None
    first_audio_t: float | None = None
    playback_t: float | None = None
    latency_sent: bool = False
    committed: bool = False
    task: asyncio.Task[None] | None = None


class ConversationCoordinator:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        stt_factory: SttFactory,
        tts_factory: TtsFactory,
        llm: LLMClient,
        audio: AudioIO,
        playback: PlaybackBuffer,
        motion: SpeechMotionCoordinator,
        tools: ToolRegistry,
        ledger: CostLedger,
        on_event: Callable[[UIEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        instructions: Callable[[], str] | None = None,
        prepare_turn: Callable[[str], Awaitable[TurnContext]] | None = None,
    ) -> None:
        self.cfg = cfg
        self._instructions = instructions or self._default_instructions
        self._prepare_turn = prepare_turn or WorldContext(None, None, None, cfg)
        self._llm_lock = asyncio.Lock()
        self._observation_task: asyncio.Task[None] | None = None
        self._observation_due = 0.0
        self._last_screen_digest: str | None = None
        self._screen_image_available: bool | None = None
        self._visual_memory: dict[str, str] | None = None
        self.screen_observations = 0
        self._interrupted_items: dict[int, dict[str, Any]] = {}
        self.last_action_error: str | None = None
        self.action_dispatch_s: list[float] = []
        # Generation numbers restart with the app; robot idempotency history does not.
        self._action_namespace = uuid.uuid4().hex
        self._stt_factory = stt_factory
        self._tts_factory = tts_factory
        self.llm = llm
        self.audio = audio
        self.playback = playback
        self.motion = motion
        self.tools = tools
        self.ledger = ledger
        self._on_event = on_event or (lambda e: None)
        self._clock = clock
        self.history = ConversationHistory(cfg.llm.history_max_items)
        self.mode = InputMode(cfg.input.mode)
        self.gate = self._new_gate()

        self.phase = Phase.IDLE
        self.muted = False
        self.stt: FluxSTT | None = None
        self.tts: FluxTTS | None = None
        self.last_error: str | None = None
        self.budget_stopped = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gen: _Gen | None = None
        self._gen_seq = 0
        self._seen_turns: set[tuple[int, int]] = set()
        self._echo_turns: set[tuple[int, int]] = set()
        self._recent_speech = ""  # assistant text heard recently (echo guard)
        self._recent_speech_until = 0.0
        # Acoustic mode (set by the app once the audio path is prepared): "aec" | "raw" | "half_duplex"
        self.audio_mode: str = cfg.audio.mode
        self.half_duplex = cfg.audio.mode == "half_duplex"
        self.blocked: str | None = None  # a reason the conversation must not start (e.g. AEC failed)
        self.before_start: Callable[[], Awaitable[None]] | None = None  # e.g. (re)prepare the audio path
        self._audible_until = 0.0  # half duplex: assistant audio playing (inf) or its tail
        self._half_duplex_turns: set[tuple[int, int]] = set()
        self.half_duplex_ignored = 0
        # Session time limit (default 2 h, extendable): stops a forgotten session by itself.
        self._deadline: float | None = None
        self._limit_task: asyncio.Task[None] | None = None
        self._limit_wake = asyncio.Event()
        self.ended_by_time_limit = False
        self._listeners_added = False
        self._running = False
        self._idle_close: asyncio.Task[None] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self._last_cost_emit = 0.0
        self._warned_budget = False
        # counters (tests and reports)
        self.generations_started = 0
        self.llm_requests = 0
        self.interruptions = 0
        self.discarded_audio_bytes = 0

    def _default_instructions(self) -> str:
        from so101_voice.character.persona import build_instructions

        return self.cfg.llm.prompt or build_instructions(self.cfg.character.name, None)

    # ================================================================== public API
    @property
    def running(self) -> bool:
        return self._running

    @property
    def screen_enabled(self) -> bool:
        return self.cfg.screen.enabled

    def screen_status(self, feed_available: bool) -> str:
        if not self.screen_enabled:
            return "OFF"
        return "ON" if feed_available and self._screen_image_available is not False else "UNAVAILABLE"

    def set_screen_enabled(self, enabled: bool) -> None:
        if self.cfg.screen.enabled == enabled:
            return
        self.cfg.screen.enabled = enabled
        self._screen_image_available = None
        self._last_screen_digest = None
        self._defer_observation()
        if enabled and self._running:
            self._start_observer()
        elif not enabled:
            if self._observation_task is not None:
                self._observation_task.cancel()
                self._observation_task = None
            if self._gen is not None and self._gen.observation is not None:
                self._interrupt("Screen OFF", notice=False)
                if self.phase not in (Phase.USER_SPEAKING, Phase.IDLE, Phase.ERROR):
                    self._set_phase(Phase.LISTENING)
        self._notice("info", f"Screen observation {'ON' if enabled else 'OFF'}")

    def _defer_observation(self) -> None:
        self._observation_due = self._clock() + self.cfg.screen.interval_s

    def _start_observer(self) -> None:
        self._defer_observation()
        if self._observation_task is None or self._observation_task.done():
            self._observation_task = asyncio.create_task(self._observe_screen(), name="screen-observer")

    def _observation_idle(self) -> bool:
        return (
            self._running
            and self.screen_enabled
            and not self.budget_stopped
            and self.phase is Phase.LISTENING
            and self._gen is None
            and not self._llm_lock.locked()
            and not any(t.get_name().startswith("action-") and not t.done() for t in self._bg)
        )

    async def _observe_screen(self) -> None:
        while self._running and self.screen_enabled:
            await asyncio.sleep(min(0.25, max(0.01, self._observation_due - self._clock())))
            if self._clock() < self._observation_due:
                continue
            self._defer_observation()  # no queue of missed ticks
            if not self._observation_idle():
                continue
            if not self.ledger.allows_new_requests():
                self._budget_stop()
                continue
            turn_id = self.tools.turn_id
            try:
                context = await self._prepare_turn("")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Screen observation preparation failed")
                self._screen_image_available = False
                continue
            self._screen_image_available = bool(context.images)
            if not self._observation_idle() or self.tools.turn_id != turn_id:
                continue
            if not context.images:
                continue  # unavailable feed never starts a paid request
            digest = context.image_digest or hashlib.sha256(json.dumps(context.images).encode()).hexdigest()
            context.image_digest = digest
            if digest == self._last_screen_digest:
                continue
            self._start_generation(None, observation=context)

    async def start(self) -> bool:
        """Open devices, connect STT (and pre-connect TTS), speak the greeting. False on failure."""
        if self._running:
            return True
        self._loop = asyncio.get_running_loop()
        self.last_error = None
        if self.before_start is not None:
            await self.before_start()
        if self.blocked is not None:
            self._fail(self.blocked)
            return False
        if not self.ledger.allows_new_requests():
            self._fail("estimated session budget already reached; not starting paid services")
            return False
        if not self._listeners_added:
            self.playback.add_listener(self._playback_from_audio_thread)
            self.playback.add_marker_listener(self._marker_from_audio_thread)
            self._listeners_added = True
        try:
            self.audio.start_output(self.playback)
            self.audio.start_input(self._mic_from_audio_thread)
        except AudioDeviceError as e:
            self.audio.close()
            self._fail(f"audio device error: {e}")
            return False
        self._running = True
        self.ended_by_time_limit = False
        self._deadline = self._clock() + self.cfg.session.max_minutes * 60
        self._limit_task = asyncio.get_running_loop().create_task(self._time_limit(), name="session-limit")
        self.budget_stopped = False
        self._set_phase(Phase.CONNECTING)
        self.gate = self._new_gate()
        self.stt = self._stt_factory(on_turn=self._on_turn, on_state=self._on_stt_state)
        self.tts = self._tts_factory(
            on_audio=self._on_tts_audio,
            on_group_done=self._on_group_done,
            on_notice=self._notice,
            on_lost=self._on_tts_lost,
            on_interrupted=self._on_tts_interrupted,
        )
        self.stt.start()
        if self.mode is InputMode.CONTINUOUS:
            self.stt.want(True)
        else:
            self._set_phase(Phase.LISTENING)  # gated: STT connects when the user talks
        self._spawn(self._preconnect_tts(), "tts-preconnect")
        if self.cfg.llm.greeting:
            self._start_generation(None, greeting=self.cfg.llm.greeting)
        if self.screen_enabled:
            self._start_observer()
        return True

    async def stop(self, reason: str = "stopped by user") -> None:
        if not self._running and self.stt is None:
            return
        self._running = False
        observer, self._observation_task = self._observation_task, None
        if observer is not None and observer is not asyncio.current_task():
            observer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await observer
        self._visual_memory = None
        self._last_screen_digest = None
        self._deadline = None
        limit, self._limit_task = self._limit_task, None
        if limit is not None and limit is not asyncio.current_task():
            limit.cancel()
        self._interrupt(reason, notice=False)
        self.playback.flush("stop")
        self.motion.force_off(reason)
        self.tools.cancel_all(reason)
        for t in list(self._bg):
            t.cancel()
        if self._idle_close is not None:
            self._idle_close.cancel()
        stt, self.stt = self.stt, None
        tts, self.tts = self.tts, None
        if stt is not None:
            await stt.aclose()
        if tts is not None:
            await tts.aclose()
        self.audio.close()
        with contextlib.suppress(OSError):
            self.ledger.persist()
        self._emit(CostUpdate(self.ledger.snapshot()))
        if self.phase is not Phase.ERROR:
            self._set_phase(Phase.IDLE)

    # ------------------------------------------------------------------ session time limit
    def remaining_s(self) -> float | None:
        """Seconds left before the session stops by itself (None when no session runs)."""
        return None if self._deadline is None else max(0.0, self._deadline - self._clock())

    def extend(self, minutes: float | None = None) -> float | None:
        """Add time (default ``[session] extend_minutes``); returns the new remaining seconds."""
        if self._deadline is None:
            return None
        add = (self.cfg.session.extend_minutes if minutes is None else minutes) * 60
        self._deadline = max(self._deadline, self._clock()) + add
        self._limit_wake.set()
        left = self.remaining_s() or 0.0
        self._notice("info", f"session extended: {_hms(left)} left")
        return left

    async def _time_limit(self) -> None:
        warn_s = self.cfg.session.warn_minutes * 60
        warned_for: float | None = None
        while self._running and self._deadline is not None:
            left = self._deadline - self._clock()
            if left <= 0:
                break
            if warn_s and left <= warn_s and warned_for != self._deadline:
                warned_for = self._deadline
                self._notice("warning", f"session ends in {_hms(left)} — press E / +2h to extend")
            wait = left - warn_s if left > warn_s and warn_s else left
            self._limit_wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._limit_wake.wait(), max(0.05, wait))
        if self._running:
            self.ended_by_time_limit = True
            self._notice("warning", "session time limit reached: stopped (press S to start a new one)")
            log.info("session stopped by the time limit")
            await self.stop("session time limit reached")

    def set_muted(self, muted: bool) -> None:
        """Muted: no audio is sent (and billed); the STT stream is closed until unmuted."""
        self.muted = muted
        if self.stt is not None:
            if muted:
                self.stt.drop_backlog()
                self.stt.want(False)
            elif self.mode is InputMode.CONTINUOUS and not self.budget_stopped:
                self.stt.want(True)
        self._notice("info", "microphone muted" if muted else "microphone live")

    def set_push_to_talk(self, active: bool) -> None:
        """Push-to-talk (toggle in the UI): pressing interrupts the assistant at once."""
        if self.mode is not InputMode.PUSH_TO_TALK or self.stt is None or self.budget_stopped:
            return
        self.gate.set_push_to_talk(active)
        if active:
            self._cancel_idle_close()
            self.stt.want(True)
            self._interrupt("push-to-talk pressed")
            self._set_phase(Phase.USER_SPEAKING)

    # ================================================================== audio-thread entry points
    def _mic_from_audio_thread(self, pcm: bytes) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._on_mic, pcm)

    def _playback_from_audio_thread(self, ev: PlaybackEvent) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._on_playback, ev)

    def _marker_from_audio_thread(self, mk: PlaybackMarker) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._on_marker, mk)

    # ================================================================== microphone
    def _on_mic(self, pcm: bytes) -> None:
        stt = self.stt
        if stt is None or self.muted or self.budget_stopped:
            return
        was_open = self.gate.is_open
        out = self.gate.process(pcm)
        if self.gate.is_open and not was_open:
            self._cancel_idle_close()
            stt.want(True)
        if out:
            stt.send_audio(out)
        if was_open and not self.gate.is_open:  # end of a gated utterance (post-roll sent)
            stt.flush_partial()
            self._spawn(stt.force_end_turn(), "force-end-turn")
            self._schedule_idle_close()

    def _schedule_idle_close(self) -> None:
        self._cancel_idle_close()

        async def close_when_idle() -> None:
            await asyncio.sleep(self.cfg.input.idle_close_s)
            if self.stt is not None and not self.gate.is_open:
                self.stt.want(False)

        self._idle_close = asyncio.get_running_loop().create_task(close_when_idle(), name="stt-idle-close")

    def _cancel_idle_close(self) -> None:
        if self._idle_close is not None:
            self._idle_close.cancel()
            self._idle_close = None

    # ================================================================== STT events
    def _on_stt_state(self, state: str, detail: str) -> None:
        if state == "connected":
            if self.phase is Phase.CONNECTING:
                self._set_phase(Phase.LISTENING)
            self._notice("info", "speech recognition connected")
        elif state == "failed":
            self._fail(f"speech recognition failed: {detail}")
        elif state == "warning":
            self._notice("warning", f"speech recognition: {detail}")
        elif (
            self._running and self.mode is InputMode.CONTINUOUS and not self.muted and not self.budget_stopped
        ):
            self._notice("warning", f"speech recognition disconnected ({detail}); reconnecting")

    def _on_turn(self, ev: TurnEvent) -> None:
        if not self._running:
            return
        key = (ev.connection, ev.turn_index)
        if self.half_duplex and (key in self._half_duplex_turns or self._assistant_audible()):
            # Fallback mode: the microphone may contain the assistant's own voice; turns that start
            # while it is audible are ignored entirely (no barge-in, no answer).
            if ev.event in ("StartOfTurn", "EndOfTurn") and key not in self._half_duplex_turns:
                self.half_duplex_ignored += 1
            self._half_duplex_turns.add(key)
            if ev.event == "EndOfTurn":
                self._seen_turns.add(key)
            return
        if ev.event == "StartOfTurn":
            # Barge-in is driven by StartOfTurn only (its transcript is never empty).
            log.info("StartOfTurn #%d: %r", ev.turn_index, ev.transcript)
            if self._is_echo(ev.transcript):
                self._echo_turns.add(key)  # the microphone heard the assistant: not a barge-in
                log.info("echo guard: StartOfTurn %r repeats the assistant; not interrupting", ev.transcript)
                return
            self._user_started(ev.transcript)
        elif ev.event == "Update":
            # Flux sends Updates about every 0.25 s, also BETWEEN turns with an empty transcript: they
            # only refresh the live transcript. (Treating them as speech cancelled every reply, live.)
            if not ev.transcript.strip():
                return
            if key in self._echo_turns:
                if self._is_echo(ev.transcript):
                    return
                self._echo_turns.discard(key)  # new words: it is the user after all
                self._user_started(ev.transcript)
                return
            self._emit(Transcript("user", ev.transcript, final=False))
        elif ev.event == "EndOfTurn":
            log.info(
                "EndOfTurn #%d (%s, eot %.2fs): %r",
                ev.turn_index,
                ev.trigger,
                ev.latency_s or 0,
                ev.transcript,
            )
            if key in self._seen_turns:
                return  # never answer the same turn twice
            self._seen_turns.add(key)
            text = ev.transcript.strip()
            trace.info(
                "EndOfTurn received %r (Flux end-of-turn latency %.0f ms)", text, (ev.latency_s or 0) * 1000
            )
            if text and key in self._echo_turns and self._is_echo(text):
                log.info("ignoring echo of the assistant's own voice: %r", text)
                self._notice("info", "ignored the assistant's own voice (echo); headphones avoid this")
                return
            if not text:
                if self.phase is Phase.USER_SPEAKING:
                    self._set_phase(Phase.LISTENING)
                return
            self._emit(Transcript("user", text))
            if self._gen is not None:
                self._interrupt("new user turn")
            self._start_generation(text, stt_eot_s=ev.latency_s)
        # EagerEndOfTurn / TurnResumed: eager mode is not enabled; nothing speculative is generated

    def _user_started(self, transcript: str) -> None:
        self._defer_observation()
        self._interrupt(f"user started speaking: {transcript!r}")
        if self.phase is not Phase.USER_SPEAKING:
            self._set_phase(Phase.USER_SPEAKING)
        if transcript:
            self._emit(Transcript("user", transcript, final=False))

    def _assistant_audible(self) -> bool:
        return self._clock() < self._audible_until

    def _is_echo(self, transcript: str) -> bool:
        """Does this user transcript mostly repeat what the assistant is saying (or just said)?

        With open speakers Flux transcribes the assistant's own voice. Such a turn must neither
        interrupt the assistant nor be answered. A transcript with new words still barges in at once.
        """
        if not self.cfg.input.echo_guard:
            return False
        words = _words(transcript)
        if not words:
            return False
        recent = self._gen.text if self._gen is not None else ""
        if self._clock() < self._recent_speech_until:
            recent += " " + self._recent_speech
        spoken = set(_words(recent))
        if not spoken:
            return False
        return sum(w in spoken for w in words) / len(words) >= self.cfg.input.echo_overlap

    # ================================================================== generations
    def _start_generation(
        self,
        user_text: str | None,
        *,
        greeting: str | None = None,
        stt_eot_s: float | None = None,
        observation: TurnContext | None = None,
    ) -> None:
        if not self.ledger.allows_new_requests():
            self._budget_stop()
            return
        self.tools.new_turn()
        self._gen_seq += 1
        g = _Gen(
            id=self._gen_seq,
            t_start=self._clock(),
            stt_eot_s=stt_eot_s,
            is_greeting=greeting is not None,
            observation=observation,
            user_text=user_text or "",
        )
        t = self.cfg.tts
        g.chunker = TextChunker(
            min_chars=t.chunk_min_chars,
            soft_max_chars=t.chunk_soft_max_chars,
            hard_max_chars=t.chunk_hard_max_chars,
        )
        self._gen = g
        self.generations_started += 1
        self._defer_observation()
        log.info("generation %d started (%s)", g.id, "greeting" if greeting is not None else "reply")
        if user_text is not None:
            self.history.add_user(user_text)
        if self.tts is not None:
            self.tts.begin(g.id)
        coro = self._speak_text_only(g, greeting) if greeting is not None else self._respond(g)
        g.task = asyncio.get_running_loop().create_task(coro, name=f"gen-{g.id}")
        if greeting is None and observation is None:
            self._set_phase(Phase.THINKING)

    def _trace(self, g: _Gen, what: str) -> None:
        """Per-turn timeline in ms since EndOfTurn receipt (logger ``so101_voice.trace``)."""
        trace.info("gen=%d +%5.0f ms  %s", g.id, (self._clock() - g.t_start) * 1000, what)

    def _current(self, g: _Gen) -> bool:
        return self._gen is g and self._running

    async def _speak_text_only(self, g: _Gen, text: str) -> None:
        try:
            g.text = text
            assert g.chunker is not None
            for seg in g.chunker.feed(text) + g.chunker.finish():
                await self._speak(g, seg)
            g.llm_done = True
            if not g.text.strip():
                g.audio_done = True
                self._complete(g)
                return
            await self._flush(g, final=True)
        except TTSError as e:
            self._abort_generation(g, f"text-to-speech unavailable: {e}")

    async def _respond(self, g: _Gen) -> None:
        cfg = self.cfg.llm
        try:
            if self.tts is not None:
                await self.tts.wait_interrupted()
            context = g.observation if g.observation is not None else await self._prepare_turn(g.user_text)
            if g.observation is None:
                if not self.screen_enabled and not requests_screen(g.user_text):
                    context.images = []  # Screen was switched OFF while preparation was in flight
                    context.state["image_included"] = False
                if self.screen_enabled or requests_screen(g.user_text):
                    self._screen_image_available = bool(context.images)
            if self._visual_memory is not None:
                context.state["previous_screen_observation"] = self._visual_memory
            if not self._current(g):
                return
            if not self.ledger.allows_new_requests():
                self._budget_stop()
                return
            catalog = self.tools.action_catalog()
            body = build_request(
                model=cfg.model,
                instructions=self._instructions()
                + reply_instructions(catalog)
                + (OBSERVATION_INSTRUCTIONS if g.observation is not None else ""),
                input_items=self.history.items + context.items(),
                tools=[],
                reasoning_effort=cfg.reasoning_effort,
                verbosity=cfg.verbosity,
                temperature=cfg.temperature,
                max_output_tokens=cfg.max_output_tokens,
                prompt_cache_key="so101-voice",
                response_format=response_format(catalog, observation=g.observation is not None),
            )
            async with self._llm_lock:
                if not self._current(g) or (g.observation is not None and not self.screen_enabled):
                    return
                if not self.ledger.allows_new_requests():
                    self._budget_stop()
                    return
                context.expire_image(self.cfg.screen.max_age_s)
                if g.observation is not None and not context.images:
                    self._screen_image_available = False
                    g.audio_done = g.llm_done = True
                    self._complete(g)
                    return
                if self.screen_enabled or requests_screen(g.user_text):
                    self._screen_image_available = bool(context.images)
                body["input"] = self.history.items + context.items()
                if context.images:
                    self._last_screen_digest = context.image_digest
                    self._defer_observation()
                if g.observation is not None:
                    self.screen_observations += 1
                _finished, reply = await self._stream_llm(g, body)
            if not self._current(g):
                return
            if isinstance(reply, ObservationReply) and reply.observation.strip():
                note = reply.observation.strip()[:400]
                if self._visual_memory is None or self._visual_memory["summary"] != note:
                    self._visual_memory = {
                        "summary": note,
                        "observed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
            decision_t = self._clock()
            self._trace(g, f"action decision validated ({len(reply.actions)} actions)")
            self._dispatch_actions(g, reply.actions, decision_t)
            assert g.chunker is not None
            for seg in g.chunker.finish():
                await self._speak(g, seg)
            g.llm_done = True
            if not g.text.strip():
                g.audio_done = True
                self._complete(g)
                return
            await self._flush(g, final=True)
        except asyncio.CancelledError:
            raise
        except LLMError as e:
            self._abort_generation(g, str(e), fatal=e.fatal)
        except TTSError as e:
            self._abort_generation(g, f"text-to-speech unavailable: {e}")
        except Exception as e:
            log.exception("generation %d crashed", g.id)
            self._abort_generation(g, f"internal error: {type(e).__name__}: {e}")

    async def _stream_llm(self, g: _Gen, body: dict[str, Any]) -> tuple[Finished | None, Reply]:
        self.llm_requests += 1
        finished: Finished | None = None
        parser = SpeechStream(observation=g.observation is not None)
        streamed = ""
        t0 = self._clock()
        self._trace(g, f"LLM single request sent ({len(body['input'])} input items)")
        first = True
        try:
            async for ev in self.llm.stream(body):
                if not self._current(g):
                    break
                if first:
                    first = False
                    self._trace(g, f"LLM first output event ({type(ev).__name__})")
                if isinstance(ev, TextDelta):
                    streamed += ev.text
                    text = parser.feed(ev.text)
                    if not text:
                        continue
                    if g.first_text_t is None:
                        g.first_text_t = self._clock()
                        self._trace(g, "LLM first speech text")
                    g.text += text
                    assert g.chunker is not None
                    for seg in g.chunker.feed(text):
                        await self._speak(g, seg)
                elif isinstance(ev, ToolCallItem):
                    raise LLMError("Unexpected function call in a structured speech/action response")
                else:
                    finished = ev
                    self._trace(g, f"LLM response {ev.status}")
        finally:
            self._meter_llm(body, finished, streamed)
            if g.observation is not None:
                log.info(
                    "Screen observation gen=%d usage=%s status=%s",
                    g.id,
                    finished.usage if finished else "estimated",
                    finished.status if finished else "cancelled",
                )
            log.info(
                "llm gen=%d %.2fs status=%s",
                g.id,
                self._clock() - t0,
                finished and finished.status,
            )
        if not self._current(g):
            return finished, Reply(speech=g.text, actions=[])
        if finished is None or finished.status != "completed":
            raise LLMError("Structured reply did not complete; no actions dispatched")
        try:
            return finished, parser.finish()
        except ValueError as exc:
            raise LLMError("Invalid structured reply; no actions dispatched") from exc

    def _meter_llm(self, body: dict[str, Any], finished: Finished | None, streamed: str) -> None:
        if finished is not None and finished.usage is not None:
            u = finished.usage
            self.ledger.llm_usage(
                input_tokens=u.input_tokens,
                cached_tokens=u.cached_tokens,
                cache_write_tokens=u.cache_write_tokens,
                output_tokens=u.output_tokens,
                reasoning_tokens=u.reasoning_tokens,
            )
        else:  # aborted or failed stream: no usage reported, but it is billed — estimate it
            image_count = 0

            def text_only(value: Any) -> Any:
                nonlocal image_count
                if isinstance(value, dict):
                    if value.get("type") == "input_image":
                        image_count += 1
                        return {"type": "input_image", "detail": value.get("detail")}
                    return {k: text_only(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [text_only(v) for v in value]
                return value

            prompt = json.dumps(text_only({k: body.get(k) for k in ("instructions", "input", "tools")}))
            self.ledger.llm_usage(
                input_tokens=estimate_tokens(prompt) + image_count * self.cfg.screen.estimated_image_tokens,
                cached_tokens=0,
                output_tokens=estimate_tokens(streamed),
                estimated=True,
            )

    def _dispatch_actions(self, g: _Gen, actions: list[Action], decision_t: float) -> None:
        """Arm commands stay ordered, follow commands stay ordered, independent lanes run together."""
        turn_id = self.tools.turn_id
        tasks: list[asyncio.Task[None]] = []
        lanes: dict[str, asyncio.Task[None]] = {}
        barrier: asyncio.Task[None] | None = None
        seen: set[str] = set()
        arm_tools = {"play_robot_behavior", "go_to_robot_pose", "robot_go_rest"}
        allowed = {tool["name"] for tool in self.tools.action_catalog()}

        async def run(action: Action, call_id: str, dependencies: list[asyncio.Task[None]]) -> None:
            if dependencies:
                await asyncio.gather(*dependencies)
            if not self._running or self.tools.turn_id != turn_id:
                return
            elapsed = self._clock() - decision_t
            self.action_dispatch_s.append(elapsed)
            self._trace(g, f"action {action.tool} dispatch ({elapsed * 1000:.0f} ms after decision)")
            self._emit(ToolActivity(action.tool, call_id, "started"))
            result = await self.tools.execute(FunctionCall(call_id, action.tool, action.arguments_json))
            self._emit(
                ToolActivity(
                    action.tool, call_id, "duplicate" if result.duplicate else "ok" if result.ok else "error"
                )
            )
            log.info(
                "action gen=%d %s ok=%s elapsed_ms=%.0f",
                g.id,
                action.tool,
                result.ok,
                result.elapsed_s * 1000,
            )
            if not result.ok:
                self.last_action_error = result.content
                log.warning("action %s failed: %s", action.tool, result.content)
                self._notice("error", f"Action {action.tool} failed; see diagnostics")

        for action in actions:
            if action.tool not in allowed:
                self.last_action_error = json.dumps({"error": "unknown_action", "tool": action.tool})
                self._notice("error", f"Action {action.tool} is not available")
                continue
            try:
                arguments = json.dumps(json.loads(action.arguments_json), sort_keys=True)
            except ValueError:
                arguments = action.arguments_json
            fingerprint = hashlib.sha256((action.tool + arguments).encode()).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            lane = "arm" if action.tool in arm_tools else action.tool
            dependencies = [barrier] if barrier else []
            if lane in lanes:
                dependencies.append(lanes[lane])
            exclusive = action.tool in ("go_to_robot_pose", "robot_go_rest")
            if exclusive:
                dependencies = list(tasks)
            call_id = f"{self._action_namespace}-gen-{g.id}-{fingerprint[:16]}"
            task = asyncio.create_task(
                run(action, call_id, dependencies), name=f"action-{g.id}-{action.tool}"
            )
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
            tasks.append(task)
            lanes[lane] = task
            if exclusive:
                barrier = task

    # ------------------------------------------------------------------ speech output
    async def _speak(self, g: _Gen, text: str) -> None:
        tts = self.tts
        if tts is None or not self._current(g):
            return
        if not g.groups:
            self._trace(g, f"TTS Speak first segment: {text!r}")
        if not await tts.speak(g.id, text):
            return
        g.groups[g.group] = (g.groups.get(g.group, "") + " " + text).strip()
        self._emit(Transcript("assistant", text, final=False))  # shown as it is sent to the voice

    async def _flush(self, g: _Gen, *, final: bool) -> None:
        if not final:
            return  # Flux streams on Speak; Flush ends the entire speech turn.
        tts = self.tts
        if tts is not None and self._current(g):
            self._trace(g, "TTS final Flush")
            await tts.flush(g.id, 0, final=True)

    def _on_tts_audio(self, pcm: bytes, gen: int, group: int) -> None:
        g = self._gen
        if g is None or g.id != gen or not self._running:
            self.discarded_audio_bytes += len(pcm)
            return
        if group != g.marked_group:
            self.playback.mark(SegmentTag(gen, group))
            g.marked_group = group
        if g.first_audio_t is None:
            g.first_audio_t = self._clock()
            self._trace(g, "TTS first audio received")
        self.playback.write(pcm)

    def _on_group_done(self, fg: SpeechTurn) -> None:
        g = self._gen
        if g is None or g.id != fg.gen or not fg.final:
            return
        g.audio_done = True
        self.playback.mark_end_of_stream()
        if self.playback.state is PlaybackState.IDLE and self.playback.buffered_s() == 0:
            self._complete(g)  # nothing to play (empty answer) or already drained

    def _on_tts_interrupted(self, gen: int, heard: str) -> None:
        item = self._interrupted_items.pop(gen, None)
        if item is not None:
            item["content"] = heard.strip()
            self._remember_speech(heard)

    def _on_tts_lost(self, reason: str) -> None:
        g = self._gen
        if g is not None and not g.audio_done:
            self._abort_generation(g, f"text-to-speech connection lost ({reason})")

    # ------------------------------------------------------------------ playback callbacks
    def _on_playback(self, ev: PlaybackEvent) -> None:
        self.motion.on_playback(ev)
        g = self._gen
        tail = self.cfg.audio.half_duplex_tail_ms / 1000
        self._audible_until = float("inf") if ev.state is PlaybackState.PLAYING else self._clock() + tail
        if ev.state is PlaybackState.PLAYING:
            if g is not None and g.playback_t is None:
                g.playback_t = self._clock()
                log.info("generation %d audible", g.id)
                self._trace(g, "first sample handed to the speaker (+ device latency ~60 ms)")
                self._emit_latency(g)
            if self.phase is not Phase.USER_SPEAKING:
                self._set_phase(Phase.SPEAKING)
            return
        if ev.reason.startswith("flushed"):
            return
        if g is not None and g.audio_done:
            self._complete(g)
        elif g is not None and self.phase is Phase.SPEAKING:
            self._set_phase(Phase.THINKING)  # long gap mid-response (tool call, slow LLM)

    def _on_marker(self, mk: PlaybackMarker) -> None:
        tag = mk.tag
        g = self._gen
        if isinstance(tag, SegmentTag) and g is not None and tag.gen == g.id:
            g.started_groups.add(tag.index)

    def _emit_latency(self, g: _Gen) -> None:
        if g.latency_sent or g.is_greeting or g.observation is not None:
            return
        g.latency_sent = True

        def rel(t: float | None) -> float | None:
            return None if t is None else round(t - g.t_start, 3)

        lat = TurnLatency(g.id, g.stt_eot_s, rel(g.first_text_t), rel(g.first_audio_t), rel(g.playback_t))
        log.info("latency %s", lat)
        self._emit(lat)

    # ------------------------------------------------------------------ ending generations
    def _complete(self, g: _Gen) -> None:
        """All audio of ``g`` was played: commit it to the history."""
        if self._gen is not g:
            return
        self._commit(g, interrupted=False)
        self._gen = None
        self._defer_observation()
        if self.phase in (Phase.SPEAKING, Phase.THINKING):
            self._set_phase(Phase.LISTENING)

    def _interrupt(self, reason: str, *, notice: bool = True) -> bool:
        self.tools.new_turn()
        g = self._gen
        if g is None:
            return False
        self._gen = None
        self.interruptions += 1
        was_playing = self.playback.flush("barge_in")
        self.motion.force_off(reason)
        if self.tts is not None:
            self.tts.cancel(g.id)
        if g.task is not None and g.task is not asyncio.current_task():
            g.task.cancel()
        self._commit(g, interrupted=True)
        if notice:
            self._notice("info", f"interrupted ({reason})" if was_playing or g.text else reason)
            self._set_phase(Phase.INTERRUPTED)
        log.info("generation %d interrupted: %s", g.id, reason)
        return True

    def _abort_generation(self, g: _Gen, message: str, *, fatal: bool = False) -> None:
        if self._gen is g:
            self._interrupt(message, notice=False)
        self.last_error = message
        if fatal:
            self._fail(message)
        else:
            self._notice("error", message)
            if self.phase not in (Phase.USER_SPEAKING, Phase.ERROR):
                self._set_phase(Phase.LISTENING)

    def _commit(self, g: _Gen, *, interrupted: bool) -> None:
        if g.committed:
            return
        g.committed = True
        if g.observation is not None and interrupted and g.playback_t is None:
            return  # cancelled silent/preparing observations add no synthetic history
        if interrupted:
            # Flux returns word-aligned text only when given a LOCAL playback offset.
            # Do not treat a fully generated/buffered response as fully heard.
            item = self.history.add_interrupted("", new_turn=g.is_greeting or g.observation is not None)
            self._interrupted_items[g.id] = item
            while len(self._interrupted_items) > 8:
                self._interrupted_items.pop(next(iter(self._interrupted_items)))
            self.ledger.tts_discarded(sum(len(t) for t in g.groups.values()))
        else:
            self.history.add_assistant(g.text, new_turn=g.is_greeting or g.observation is not None)
            self._remember_speech(g.text)

    def _remember_speech(self, text: str) -> None:
        self._recent_speech = text
        self._recent_speech_until = self._clock() + self.cfg.input.echo_window_s

    # ================================================================== budget
    def on_cost(self, snap: CostSnapshot) -> None:
        """Ledger listener (wired by the app)."""
        now = self._clock()
        if snap.state is BudgetState.WARN and not self._warned_budget:
            self._warned_budget = True
            self._notice(
                "warning", f"estimated cost ${snap.session_usd:.3f} is near the ${snap.limit_usd:.2f} cap"
            )
        if snap.state is BudgetState.EXCEEDED and not self.budget_stopped and self._running:
            self._budget_stop()
        if now - self._last_cost_emit >= 0.5 or snap.state is BudgetState.EXCEEDED:
            self._last_cost_emit = now
            self._emit(CostUpdate(snap))

    def _budget_stop(self) -> None:
        if self.budget_stopped:
            return
        self.budget_stopped = True
        snap = self.ledger.snapshot()
        self._interrupt("estimated budget reached", notice=False)
        if self.stt is not None:
            self.stt.drop_backlog()
            self.stt.want(False)
        self._notice(
            "error",
            f"estimated session cost ${snap.session_usd:.3f} reached the ${snap.limit_usd or 0:.2f} cap: "
            "no new paid requests (raise [cost].max_session_estimated_usd to continue)",
        )
        self._set_phase(Phase.IDLE)

    # ================================================================== helpers
    async def _preconnect_tts(self) -> None:
        if self.tts is not None:
            try:
                await self.tts.connect()
            except TTSError as e:
                self._notice("warning", f"text-to-speech: {e}")

    def _new_gate(self) -> MicGate:
        i = self.cfg.input
        return MicGate(
            self.mode,
            sample_rate=self.cfg.stt.sample_rate,
            preroll_ms=i.preroll_ms,
            postroll_ms=i.postroll_ms,
            threshold_dbfs=i.vad_threshold_dbfs,
            attack_ms=i.vad_attack_ms,
        )

    def _spawn(self, coro: Any, name: str) -> None:
        t = asyncio.get_running_loop().create_task(coro, name=name)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _set_phase(self, phase: Phase) -> None:
        if phase is not self.phase:
            self.phase = phase
            self._emit(PhaseChanged(phase))

    def _fail(self, msg: str) -> None:
        self.last_error = msg
        log.error("%s", msg)
        self._notice("error", msg)
        self._set_phase(Phase.ERROR)

    def _notice(self, level: str, text: str) -> None:
        self._emit(Notice(level, text))

    def _emit(self, ev: UIEvent) -> None:
        try:
            self._on_event(ev)
        except Exception:
            log.exception("UI event handler failed")

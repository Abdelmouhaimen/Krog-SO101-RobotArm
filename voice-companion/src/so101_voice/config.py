"""Configuration: TOML file(s) for settings, environment (or a git-ignored ``.env``) for secrets.

Load order: ``config/default.toml`` → ``config/local.toml`` (git-ignored, optional) → ``--config FILE``.
Secrets are never read from TOML: ``DEEPGRAM_API_KEY``, ``OPENAI_API_KEY`` and ``SO101_API_TOKEN`` come
from the environment, and ``.env`` is loaded first if present (existing variables win).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from so101_voice.cost import Prices
from so101_voice.stt.flux import SUPPORTED_SAMPLE_RATES

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SttConfig(_M):
    provider: Literal["deepgram"] = "deepgram"
    url: str = "wss://api.deepgram.com/v2/listen"
    model: Literal["flux-general-en", "flux-general-multi"] = "flux-general-en"
    sample_rate: int = 16000
    send_chunk_ms: int = Field(default=80, ge=20, le=200)  # Deepgram: 80 ms strongly recommended
    eot_threshold: float | None = Field(default=None, ge=0.5, le=1.0)  # None = server default 0.7
    eot_timeout_ms: int | None = Field(default=None, ge=500, le=60000)  # None = server default 5000
    keyterms: list[str] = Field(default_factory=list)  # paid add-on (see [cost.prices])
    connect_timeout_s: float = 10.0
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 15.0
    reconnect_max_attempts: int = 8

    @model_validator(mode="after")
    def _check(self) -> SttConfig:
        if self.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(f"stt.sample_rate must be one of {SUPPORTED_SAMPLE_RATES}")
        return self


class LlmConfig(_M):
    provider: Literal["openai"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-6-luna"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "none"
    verbosity: Literal["low", "medium", "high"] | None = "low"
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)  # only allowed with effort "none"
    stream: Literal[True] = True
    max_output_tokens: int = Field(default=400, ge=16, le=8000)
    history_max_items: int = Field(default=40, ge=4, le=400)
    request_timeout_s: float = Field(default=30.0, gt=0)
    _prompt_source: str | None = PrivateAttr(default=None)

    @property
    def prompt_source(self) -> str:
        if self.prompt:
            return self._prompt_source or "programmatic llm.prompt override"
        return "built-in Krog persona + discovered body library"

    prompt: str | None = None  # None: the character persona (character/persona.py) + live robot library
    greeting: str | None = "Hmph. There you are. I was getting bored."

    @model_validator(mode="after")
    def _check(self) -> LlmConfig:
        if self.temperature is not None and self.reasoning_effort != "none":
            raise ValueError("llm.temperature is only accepted with reasoning_effort = 'none'")
        return self


class TtsConfig(_M):
    provider: Literal["deepgram"] = "deepgram"
    url: str = "wss://api.deepgram.com/v2/speak"
    model: str = Field(default="flux-cliff-en", pattern=r"^flux-[a-z]+-[a-z-]+$")
    sample_rate: Literal[8000, 16000, 24000, 32000, 44100, 48000] = 24000
    expressivity: Literal[-2, -1, 0, 1, 2] = 0
    chunk_min_chars: int = Field(default=40, ge=1)
    chunk_soft_max_chars: int = Field(default=160, ge=20)
    chunk_hard_max_chars: int = Field(default=260, ge=40, le=2000)
    connect_timeout_s: float = Field(default=10.0, gt=0)
    interrupt_timeout_s: float = Field(default=1.0, gt=0)
    max_connection_s: float = Field(default=55 * 60, gt=0, le=55 * 60)

    @model_validator(mode="after")
    def _flux_endpoint(self) -> TtsConfig:
        if not self.url.rstrip("/").endswith("/v2/speak"):
            raise ValueError("Flux TTS requires /v2/speak; migrate old Aura configuration")
        return self


class InputConfig(_M):
    mode: Literal["continuous", "push_to_talk", "vad"] = "continuous"
    preroll_ms: int = Field(default=300, ge=0, le=2000)
    postroll_ms: int = Field(default=800, ge=0, le=5000)
    vad_threshold_dbfs: float = Field(default=-42.0, ge=-90, le=0)
    vad_attack_ms: int = Field(default=60, ge=10, le=1000)
    idle_close_s: float = Field(default=4.0, ge=0.5, le=9.0)  # close the STT stream when the gate idles
    # Open speakers: Flux hears the assistant's voice. A user turn whose words mostly repeat what the
    # assistant is saying (or said within echo_window_s) neither interrupts nor gets an answer.
    echo_guard: bool = True
    echo_overlap: float = Field(default=0.6, gt=0, le=1)
    echo_window_s: float = Field(default=2.0, ge=0, le=10)


class AecConfig(_M):
    """PipeWire WebRTC echo cancellation (see ``audio/aec.py`` and ``scripts/aec_check.py``)."""

    topology: Literal["monitor", "sink"] = "monitor"  # monitor: game + system audio are cancelled too
    mic: str | None = None  # PipeWire node.name or description substring; None = default microphone
    speaker: str | None = None  # same for the speaker sink; None = default sink
    noise_suppression: bool = True  # measured: ~20 dB of extra residual-echo suppression
    gain_control: bool = (
        False  # measured: +8 dB speech, but it lifted residual echo up to 18 dB above the floor
    )
    high_pass_filter: bool = True
    # If AEC cannot start: "error" refuses to run (default); "raw" / "half_duplex" run with a warning.
    on_failure: Literal["error", "raw", "half_duplex"] = "error"
    start_timeout_s: float = Field(default=5.0, gt=0, le=30)


class AudioConfig(_M):
    # aec: full duplex with PipeWire echo cancellation (open microphone + room speakers)
    # raw: the microphone as is (diagnostics; the assistant can hear itself)
    # half_duplex: user turns are ignored while assistant audio plays (fallback, no barge-in by voice)
    mode: Literal["aec", "raw", "half_duplex"] = "aec"
    aec: AecConfig = Field(default_factory=AecConfig)
    input_device: str | int | None = None  # raw/half_duplex: name substring or index; None = default
    output_device: str | int | None = None
    # Device buffer size. Measured 2026-11-08 on this laptop (Intel SOF HDA DSP): 20 ms periods made
    # PipeWire drive the sound card with tiny chunks and the ALSA driver fell into a "Broken pipe"
    # recovery loop (3 of 4 runs silent); 40 ms and 60 ms ran clean (10 of 10).
    input_period_ms: int = Field(default=40, ge=5, le=200)
    output_period_ms: int = Field(default=40, ge=5, le=200)
    prebuffer_ms: int = Field(default=40, ge=0, le=1000)
    output_latency_ms: int | None = Field(default=None, ge=0, le=1000)  # None: estimated from periods
    half_duplex_tail_ms: int = Field(default=300, ge=0, le=3000)  # keep ignoring turns after playback


class RobotConfig(_M):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8765"
    timeout_s: float = Field(default=1.0, gt=0, le=10)
    talk_intensity: Literal["gentle", "normal", "excited"] = "normal"
    lease_s: float = Field(default=2.0, gt=0, le=60)
    renew_interval_s: float = Field(default=0.8, gt=0)
    stop_grace_s: float = Field(default=0.25, ge=0, le=2)
    retry_interval_s: float = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def _lease(self) -> RobotConfig:
        if self.lease_s < 2 * self.renew_interval_s:
            raise ValueError("robot.lease_s must be at least 2 × renew_interval_s")
        if self.lease_s - self.renew_interval_s <= self.stop_grace_s:
            # No renewal is sent during the stop grace, so the current lease must outlive it.
            raise ValueError("robot.lease_s - renew_interval_s must exceed stop_grace_s")
        return self


class CharacterConfig(_M):
    """The companion's character and how it animates the SO-101 (M3B)."""

    name: str = "Krog"
    default_idle: str = "idle_watching"
    calm_idles: list[str] = Field(default_factory=lambda: ["idle_watching", "idle_curious"])  # variety
    listening_idle: str | None = "idle_listening"  # while the user speaks (None: no change)
    talking_idle: str | None = "idle_talking"  # while the assistant speaks (None: no change)
    session_start_reaction: str | None = "wake_up"  # played when a session starts, then default_idle
    reactive_gestures: bool = True  # offer the gesture tools to the LLM
    automatic_idles: bool = True  # listening / talking / calm idles follow the conversation
    mood_ttl_s: float = Field(default=120.0, ge=0)  # an idle the LLM chose stays the "calm" idle this long
    min_switch_interval_s: float = Field(default=3.0, ge=0)  # no automatic idle change more often
    rest_on_stop: bool = True  # idles loop forever: Go Rest when the session ends


class RobotToolsConfig(_M):
    """Robot tools for the LLM and the behavior director. ``enabled = false``: voice only."""

    enabled: bool = True
    # Motion on a REAL arm (the robot reports a non-fake driver). On by the owner's choice (2026-11-08);
    # the robot service's own allow_physical_control, faults and Emergency Hold still apply.
    allow_physical: bool = True
    library_refresh_s: float = Field(default=60.0, ge=5)
    command_poll_s: float = Field(default=0.15, ge=0.02, le=2)
    status_poll_s: float = Field(default=1.0, ge=0.1, le=10)


class SessionConfig(_M):
    """Every session ends by itself, so a forgotten one never keeps listening (and billing)."""

    max_minutes: float = Field(default=120.0, gt=0, le=24 * 60)
    extend_minutes: float = Field(default=120.0, gt=0, le=24 * 60)
    warn_minutes: float = Field(default=5.0, ge=0, le=60)


class CostConfig(_M):
    enabled: bool = True
    max_session_estimated_usd: float | None = Field(default=0.25, gt=0)
    warn_at_fraction: float = Field(default=0.8, gt=0, le=1)
    ledger_file: str | None = "logs/cost-ledger.jsonl"
    prices: Prices = Field(default_factory=Prices)


class PerceptionConfig(_M):
    enabled: bool = False
    receiver_url: str = "http://192.168.50.1:8766"
    timeout_s: float = Field(default=0.3, gt=0, le=2)


class ScreenConfig(_M):
    enabled: bool = False  # AI image consumption; never controls the Windows sender
    interval_s: float = Field(default=3.0, gt=0, le=3600)
    detail: Literal["low", "auto", "high"] = "low"
    max_age_s: float = Field(default=5.0, gt=0, le=5)
    estimated_image_tokens: int = Field(default=2000, ge=1)  # conservative fallback if usage is missing


class FollowConfig(_M):
    enabled: bool = True
    target_rate_hz: float = Field(default=20, ge=1, le=20)
    calibration_valid: bool = False
    acquire_s: float = Field(default=0.2, ge=0, le=2)
    allow_physical: bool = False  # separate supervised targeting authorization


class AppConfig(_M):
    stt: SttConfig = Field(default_factory=SttConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    input: InputConfig = Field(default_factory=InputConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    robot: RobotConfig = Field(default_factory=RobotConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    character: CharacterConfig = Field(default_factory=CharacterConfig)
    robot_tools: RobotToolsConfig = Field(default_factory=RobotToolsConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    follow: FollowConfig = Field(default_factory=FollowConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    log_file: str | None = "logs/so101-voice.log"

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        p = self.cost.prices
        if (p.stt_model, p.llm_model, p.tts_model) != (self.stt.model, self.llm.model, self.tts.model):
            raise ValueError(
                "[cost.prices] must describe the configured models "
                f"(prices: {p.stt_model}/{p.llm_model}/{p.tts_model}; "
                f"config: {self.stt.model}/{self.llm.model}/{self.tts.model}) — verify and update the prices"
            )
        return self


class Secrets(BaseModel):
    bridge_token: str | None = None
    deepgram_api_key: str | None
    openai_api_key: str | None
    so101_api_token: str | None


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


OBSOLETE_SECTIONS = {
    "deepgram": "the Deepgram Voice Agent was replaced in M3A.1; use [stt], [llm] and [tts]",
}


def load_config(extra: str | Path | None = None, root: Path = REPO_ROOT) -> AppConfig:
    if extra is not None and not Path(extra).exists():
        raise FileNotFoundError(extra)
    data: dict[str, Any] = {}
    prompt_source = None
    for p in (root / "config/default.toml", root / "config/local.toml", Path(extra) if extra else None):
        if p is not None and p.exists():
            with open(p, "rb") as f:
                layer = tomllib.load(f)
            for key, why in OBSOLETE_SECTIONS.items():
                if key in layer:
                    raise ConfigError(f"{p}: obsolete [{key}] section ({why}; see config/default.toml)")
            if "prompt" in layer.get("llm", {}):
                prompt_source = str(p.resolve())
            data = _deep_merge(data, layer)
    cfg = AppConfig.model_validate(data)
    cfg.llm._prompt_source = prompt_source
    return cfg


def load_dotenv(path: Path) -> None:
    """Minimal ``KEY=value`` loader (no interpolation). Existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_secrets(dotenv: Path | None = None) -> Secrets:
    for p in [dotenv] if dotenv else [Path.cwd() / ".env", REPO_ROOT / ".env"]:
        load_dotenv(p)
    return Secrets(
        bridge_token=os.environ.get("BRIDGE_TOKEN") or None,
        deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY") or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        so101_api_token=os.environ.get("SO101_API_TOKEN") or None,
    )

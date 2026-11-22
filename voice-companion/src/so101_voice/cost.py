"""Cost ledger: STT, LLM and TTS metered independently, with a session budget.

Every figure is an **estimate** computed locally from configured prices (``[cost.prices]`` in the
config, with their source URLs and verification date). They are not provider invoices. Where a
provider reports usage (OpenAI ``response.usage``), that is used; otherwise usage is estimated and
flagged so.

What is metered:
* **STT** (Deepgram Flux): audio seconds actually sent (the billing basis for streaming STT) and
  connection seconds (for information).
* **LLM** (OpenAI): input, cached-input and output tokens (reasoning tokens are billed as output),
  per request. An aborted stream that never reported usage is estimated at ~4 characters per token.
* **TTS** (Deepgram Flux TTS): every character sent in a ``Speak`` message, including text that was later
  cleared by a barge-in or never played. Discarded synthesis is assumed to be billed.

Budget: past ``warn_at_fraction`` × ``max_session_estimated_usd`` the ledger reports WARN. At the cap it
reports EXCEEDED. The conversation then refuses to start new paid requests and cancels active ones.
A cumulative total across sessions is kept in a JSON-lines file.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


class Prices(BaseModel):
    """Pay-as-you-go list prices, checked on the providers' pricing pages on ``verified_on``."""

    model_config = ConfigDict(extra="forbid")

    verified_on: str = "2026-11-11"
    stt_model: str = "flux-general-en"
    stt_usd_per_min: float = Field(default=0.0077, ge=0)  # billed per second of audio, no minimum
    stt_keyterm_usd_per_min: float = Field(default=0.0013, ge=0)  # add-on, only when keyterms are set
    stt_source: str = "https://deepgram.com/pricing"
    llm_model: str = "gpt-6-luna"
    llm_input_usd_per_mtok: float = Field(default=0.10, ge=0)
    llm_cached_input_usd_per_mtok: float = Field(default=0.01, ge=0)
    llm_cache_write_usd_per_mtok: float = Field(default=0.125, ge=0)  # 30-minute cache write
    llm_output_usd_per_mtok: float = Field(default=0.50, ge=0)  # reasoning tokens are billed as output
    llm_source: str = "https://developers.openai.com/api/docs/pricing"
    tts_model: str = "flux-cliff-en"
    tts_usd_per_1k_chars: float = Field(default=0.045, ge=0)  # billed per input character sent
    tts_source: str = "https://deepgram.com/pricing"


class BudgetState(enum.Enum):
    OK = "ok"
    WARN = "warn"
    EXCEEDED = "exceeded"
    DISABLED = "disabled"


@dataclass
class SttUsage:
    model: str
    audio_s: float = 0.0
    connection_s: float = 0.0
    connections: int = 0


@dataclass
class LlmUsage:
    model: str
    requests: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_requests: int = 0  # requests whose usage was estimated (aborted stream, no usage event)


@dataclass
class TtsUsage:
    model: str
    characters: int = 0
    provider_input_characters: int = 0
    provider_billable_characters: int = 0
    segments: int = 0
    discarded_characters: int = 0  # sent, then cleared or never played (still billed)


@dataclass(frozen=True)
class CostSnapshot:
    stt_usd: float
    llm_usd: float
    tts_usd: float
    session_usd: float
    cumulative_usd: float
    limit_usd: float | None
    state: BudgetState

    @property
    def fraction(self) -> float:
        return self.session_usd / self.limit_usd if self.limit_usd else 0.0


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class CostLedger:
    def __init__(
        self,
        prices: Prices,
        *,
        enabled: bool = True,
        max_session_usd: float | None = 0.25,
        stt_keyterms: bool = False,
        warn_at_fraction: float = 0.8,
        ledger_file: Path | None = None,
        on_change: Callable[[CostSnapshot], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.prices = prices
        self.enabled = enabled
        self.max_session_usd = max_session_usd
        self.warn_at_fraction = warn_at_fraction
        self.ledger_file = ledger_file
        self.stt_keyterms = stt_keyterms
        self._on_change = on_change
        self._clock = clock
        self._lock = threading.Lock()
        self.stt = SttUsage(prices.stt_model)
        self.llm = LlmUsage(prices.llm_model)
        self.tts = TtsUsage(prices.tts_model)
        self._tts_reports: dict[str, tuple[int, int]] = {}
        self.previous_usd = self._load_cumulative()
        self._stt_open_at: float | None = None
        self._last_state = BudgetState.OK
        self.started_at = _now_iso()

    # ------------------------------------------------------------------ STT
    def stt_connected(self) -> None:
        with self._lock:
            self.stt.connections += 1
            self._stt_open_at = self._clock()

    def stt_disconnected(self) -> None:
        with self._lock:
            if self._stt_open_at is not None:
                self.stt.connection_s += self._clock() - self._stt_open_at
                self._stt_open_at = None

    def stt_audio(self, seconds: float) -> None:
        with self._lock:
            self.stt.audio_s += seconds
        self._changed()

    # ------------------------------------------------------------------ LLM
    def llm_usage(
        self,
        *,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        reasoning_tokens: int = 0,
        cache_write_tokens: int = 0,
        estimated: bool = False,
    ) -> None:
        with self._lock:
            self.llm.requests += 1
            self.llm.input_tokens += input_tokens
            cached = min(cached_tokens, input_tokens)
            self.llm.cached_tokens += cached
            self.llm.cache_write_tokens += min(cache_write_tokens, input_tokens - cached)
            self.llm.output_tokens += output_tokens
            self.llm.reasoning_tokens += reasoning_tokens
            self.llm.estimated_requests += int(estimated)
        self._changed()

    # ------------------------------------------------------------------ TTS
    def tts_sent(self, characters: int, *, segment: bool = True) -> None:
        with self._lock:
            self.tts.characters += characters
            self.tts.segments += int(segment)
        self._changed()

    def tts_report(self, session: str, input_characters: int, billable_characters: int) -> None:
        """Replace a session's reported totals (not additive); unreported sends remain estimated."""
        with self._lock:
            self._tts_reports[session] = (max(0, input_characters), max(0, billable_characters))
            self.tts.provider_input_characters = sum(x[0] for x in self._tts_reports.values())
            self.tts.provider_billable_characters = sum(x[1] for x in self._tts_reports.values())
        self._changed()

    def tts_discarded(self, characters: int) -> None:
        with self._lock:
            self.tts.discarded_characters += characters

    # ------------------------------------------------------------------ totals and budget
    def stt_usd(self) -> float:
        rate = self.prices.stt_usd_per_min + (self.prices.stt_keyterm_usd_per_min if self.stt_keyterms else 0)
        return self.stt.audio_s / 60.0 * rate

    def llm_usd(self) -> float:
        p, u = self.prices, self.llm
        # Each input token is billed at exactly one of: uncached, cached or cache-write rate.
        uncached = u.input_tokens - u.cached_tokens - u.cache_write_tokens
        return (
            uncached * p.llm_input_usd_per_mtok
            + u.cached_tokens * p.llm_cached_input_usd_per_mtok
            + u.cache_write_tokens * p.llm_cache_write_usd_per_mtok
            + u.output_tokens * p.llm_output_usd_per_mtok  # includes reasoning tokens
        ) / 1e6

    def tts_usd(self) -> float:
        unreported = max(0, self.tts.characters - self.tts.provider_input_characters)
        return (
            (unreported + self.tts.provider_billable_characters) / 1000.0 * self.prices.tts_usd_per_1k_chars
        )

    def snapshot(self) -> CostSnapshot:
        with self._lock:
            stt, llm, tts = self.stt_usd(), self.llm_usd(), self.tts_usd()
        total = stt + llm + tts
        return CostSnapshot(
            stt, llm, tts, total, self.previous_usd + total, self.max_session_usd, self._state(total)
        )

    @property
    def state(self) -> BudgetState:
        return self.snapshot().state

    def allows_new_requests(self) -> bool:
        return self.state is not BudgetState.EXCEEDED

    def _state(self, total: float) -> BudgetState:
        if not self.enabled or not self.max_session_usd:
            return BudgetState.DISABLED
        if total >= self.max_session_usd:
            return BudgetState.EXCEEDED
        if total >= self.warn_at_fraction * self.max_session_usd:
            return BudgetState.WARN
        return BudgetState.OK

    def _changed(self) -> None:
        if self._on_change is None:
            return
        snap = self.snapshot()
        try:
            self._on_change(snap)
        except Exception:
            log.exception("cost listener failed")
        self._last_state = snap.state

    # ------------------------------------------------------------------ persistence
    def report(self) -> dict[str, object]:
        snap = self.snapshot()
        return {
            "started_at": self.started_at,
            "ended_at": _now_iso(),
            "estimate": True,
            "prices_verified_on": self.prices.verified_on,
            "stt": asdict(self.stt) | {"usd": round(snap.stt_usd, 6)},
            "llm": asdict(self.llm) | {"usd": round(snap.llm_usd, 6)},
            "tts": asdict(self.tts) | {"usd": round(snap.tts_usd, 6)},
            "session_usd": round(snap.session_usd, 6),
        }

    def persist(self) -> None:
        """Append this session to the ledger file (only if anything was spent)."""
        if self.ledger_file is None or self.snapshot().session_usd <= 0:
            return
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_file.open("a") as f:
            f.write(json.dumps(self.report()) + "\n")

    def _load_cumulative(self) -> float:
        if self.ledger_file is None or not self.ledger_file.exists():
            return 0.0
        total = 0.0
        for line in self.ledger_file.read_text().splitlines():
            try:
                total += float(json.loads(line).get("session_usd", 0.0))
            except (ValueError, AttributeError):
                log.warning("ignoring a malformed cost ledger line")
        return total


@dataclass
class SlidingWindow:
    """Sliding-window budget for generic rolling limits (used by non-TTS components and tests)."""

    limit: float
    window_s: float
    clock: Callable[[], float] = time.monotonic
    events: list[tuple[float, float]] = field(default_factory=list)  # (time, amount)

    def used(self) -> float:
        cutoff = self.clock() - self.window_s
        self.events = [e for e in self.events if e[0] > cutoff]
        return sum(a for _, a in self.events)

    def wait_s(self, amount: float = 1) -> float:
        """Seconds until ``amount`` more fits in the window (0 = now)."""
        if amount > self.limit:
            raise ValueError(f"{amount} exceeds the window limit {self.limit}")
        excess = self.used() + amount - self.limit
        if excess <= 0:
            return 0.0
        freed = 0.0
        for t, a in self.events:
            freed += a
            if freed >= excess:
                return max(0.0, t + self.window_s - self.clock())
        return self.window_s  # unreachable: amount <= limit

    def add(self, amount: float = 1) -> None:
        self.events.append((self.clock(), amount))

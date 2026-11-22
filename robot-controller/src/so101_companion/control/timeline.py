"""Stall-aware logical clock for timed trajectories (owner decisions D1/D4, ADR-013/ADR-014).

Each issued control tick advances the logical time by the real elapsed time, but by at most
``max_step`` (one nominal control period). After a 350 ms serial stall the next target is therefore the
next point of the trajectory, not a point 350 ms further on: the timeline is delayed, nothing is skipped
and nothing is replayed in a burst. The accumulated difference is reported as ``delay_s``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StallAwareClock:
    max_step: float
    started_at: float
    elapsed: float = 0.0  # logical seconds served so far
    delay_s: float = 0.0  # wall time NOT turned into progress (stalls, late ticks)
    _last: float | None = None

    def __post_init__(self) -> None:
        if self.max_step <= 0:
            raise ValueError("max_step must be > 0")
        self._last = self.started_at

    def advance(self, now: float) -> float:
        """Advance for one issued tick at monotonic time ``now``; return the logical elapsed time."""
        last = self._last if self._last is not None else now
        dt = max(0.0, now - last)
        self._last = now
        step = min(dt, self.max_step)
        self.elapsed += step
        self.delay_s += dt - step
        return self.elapsed

    def rebase(self, now: float) -> None:
        """Continue from ``now`` without turning the time since the last tick into progress or delay
        (used when a paused playback resumes: the pause is neither playback nor a stall)."""
        self._last = now

"""Follow Me (shoulder_pan) and Talk Mode (gripper) overlays — Milestone 2.6, ADR-019. Pure; no threads.

The controller (the single owner of the arm) composes ONE six-joint target per tick:

    active motion (behavior / pose / rest / sliders) -> Follow Me replaces shoulder_pan
    -> Talk Mode replaces gripper -> the unchanged calibrated clamp -> one send

Everything here is a deterministic function of its inputs and the per-tick ``dt`` (the controller passes a
stall-aware dt, so a serial stall delays motion instead of producing a jump).

Follow Me geometry (verified on the official model; CW-from-above convention, s = pan sign):

    gripper bearing (model frame)  beta = s*q_pan + psi            psi: representative direction
    target                          beta = F + theta                F: configured front, theta: request
    pan                             q    = (F + theta - psi + 360k) / s

Calibrated pan travel (209.8° here) is < 360°, so at most one k is inside the bounds; otherwise the pan
goes to the bound with the smaller bearing residual (with hysteresis) and the result is LIMITED or
UNREACHABLE — never reported as tracking.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from so101_companion.behaviors.direction import Direction, wrap180
from so101_companion.behaviors.plan import MIN_JERK_PEAK, MIN_JERK_PEAK_ACCEL
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

PAN: Final = "shoulder_pan"
GRIPPER: Final = "gripper"
LIMITED_TOLERANCE_DEG: Final = (
    20.0  # clamped at a bound with a residual up to this: LIMITED; above: UNREACHABLE
)
REACHED_TOLERANCE_DEG: Final = 0.5
BOUND_HYSTERESIS_DEG: Final = 10.0
TARGET_DEADBAND_DEG: Final = 0.5  # ignore target changes smaller than this (perception noise)
HANDOFF_MIN_S: Final = 0.25


def min_jerk(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * x * (10.0 - 15.0 * x + 6.0 * x * x)


def min_jerk_duration(distance: float, v_max: float, a_max: float, minimum: float = 0.0) -> float:
    """Shortest min-jerk duration for ``distance`` within a peak speed and a peak acceleration."""
    d = abs(distance)
    return max(minimum, MIN_JERK_PEAK * d / v_max, math.sqrt(MIN_JERK_PEAK_ACCEL * d / a_max))


# ======================================================================== solver
@dataclass(frozen=True)
class FollowSolution:
    pan_target: float
    reach: str  # REACHABLE | LIMITED | UNREACHABLE
    residual_deg: float  # requested minus achievable bearing (0 when reachable)
    bound: str | None  # "low" | "high" when clamped
    reachable_theta: tuple[float, float]  # (from, to) clockwise interval of reachable target angles


def reachable_theta(psi: float, front: float, lo: float, hi: float, s: float = 1.0) -> tuple[float, float]:
    """Clockwise interval [from, to] (wrapped to ±180) of target angles this direction can face."""
    a = wrap180(s * lo + psi - front)
    b = wrap180(s * hi + psi - front)
    return (a, b) if s > 0 else (b, a)


def solve_pan(
    theta: float,
    psi: float,
    front: float,
    lo: float,
    hi: float,
    *,
    s: float = 1.0,
    current_bound: str | None = None,
    current_pan: float | None = None,
) -> FollowSolution:
    base = (front + theta - psi) / s
    span = 360.0 / abs(s)
    k0 = math.floor((lo - base) / span) - 1
    for k in range(k0, k0 + 4):
        q = base + k * span
        if lo - 1e-9 <= q <= hi + 1e-9:
            return FollowSolution(
                min(max(q, lo), hi), "REACHABLE", 0.0, None, reachable_theta(psi, front, lo, hi, s)
            )

    def residual(q: float) -> float:
        return wrap180(front + theta - (s * q + psi))

    r_lo, r_hi = residual(lo), residual(hi)
    choice = "low" if abs(r_lo) <= abs(r_hi) else "high"
    # Continuity first (found on the real arm, 2026-11-07): never swing across the whole pan travel for a
    # marginal gain. Prefer the bound already in use, else the bound nearer the current pan, unless the
    # other bound is better by more than the hysteresis.
    preferred = current_bound
    if preferred not in ("low", "high") and current_pan is not None:
        preferred = "low" if abs(current_pan - lo) <= abs(current_pan - hi) else "high"
    if preferred in ("low", "high") and preferred != choice:
        cur, other = (abs(r_lo), abs(r_hi)) if preferred == "low" else (abs(r_hi), abs(r_lo))
        if cur - other < BOUND_HYSTERESIS_DEG:
            choice = preferred
    q, r = (lo, r_lo) if choice == "low" else (hi, r_hi)
    reach = "LIMITED" if abs(r) <= LIMITED_TOLERANCE_DEG else "UNREACHABLE"
    return FollowSolution(q, reach, r, choice, reachable_theta(psi, front, lo, hi, s))


# ======================================================================== smooth motion pieces
class PanTracker:
    """Online second-order trajectory generator: position AND velocity continuous, bounded speed and
    acceleration (the existing ADR-015 per-joint transition limits, scaled by a preset), no overshoot,
    latest target only — never a queue."""

    def __init__(self, v_max: float, a_max: float) -> None:
        self.v_max = v_max
        self.a_max = a_max
        self.q = 0.0
        self.v = 0.0

    def reset(self, q: float, v: float = 0.0) -> None:
        self.q, self.v = q, v

    def step(self, target: float, dt: float) -> float:
        if dt <= 0.0:
            return self.q
        e = target - self.q
        if abs(e) < 1e-4 and abs(self.v) <= self.a_max * dt:
            self.q, self.v = target, 0.0
            return self.q
        # discrete-time braking speed: braking by a·dt per step from v covers v²/(2a) + v·dt/2 — exactly |e|
        ad = self.a_max * dt
        v_brake = ad * (math.sqrt(0.25 + 2.0 * abs(e) / (ad * dt)) - 0.5)
        v_des = math.copysign(min(self.v_max, v_brake, abs(e) / dt), e)
        dv = min(max(v_des - self.v, -self.a_max * dt), self.a_max * dt)
        self.v += dv
        self.q += self.v * dt  # (discretization may pass the target by at most a·dt²/2, then settles)
        return self.q


@dataclass
class Handoff:
    """Smooth return of a joint to the motion's own value: value = base + offset·(1 − s(u))."""

    offset: float
    duration: float
    elapsed: float = 0.0

    @classmethod
    def plan(cls, offset: float, v_max: float, a_max: float) -> Handoff:
        return cls(offset, min_jerk_duration(offset, v_max, a_max, HANDOFF_MIN_S))

    def step(self, base: float, dt: float) -> float:
        self.elapsed = min(self.duration, self.elapsed + max(dt, 0.0))
        return base + self.offset * (1.0 - min_jerk(self.elapsed / self.duration if self.duration else 1.0))

    @property
    def done(self) -> bool:
        return self.elapsed >= self.duration


def profile_limits(joint: str, profile: TransitionProfile) -> tuple[float, float]:
    return (
        policy.TRANSITION_MAX_SPEED[joint] * profile.speed_scale,
        policy.TRANSITION_MAX_ACCEL[joint] * profile.accel_scale,
    )


# ======================================================================== Talk Mode generator
@dataclass(frozen=True)
class TalkPreset:
    """All presets stay inside the ADR-015 gripper limits verified on this arm (≤150 units/s, ≤700 units/s²)
    and the owner-approved ~1.5 Hz maximum rhythm (D9): a segment never lasts less than ``segment_min_s``."""

    name: str
    amplitude: float  # gripper units above the closed level (1 unit = 1.32° on this arm)
    v_max: float
    a_max: float
    pause_probability: float
    pause_s: tuple[float, float]
    segment_min_s: float  # 1 / (2 · 1.5 Hz) = 0.333 s or more


TALK_CLOSED_LEVEL: Final = 3.0  # not 0: avoids stalling the jaw against its closed stop
TALK_PRESETS: Final[Mapping[str, TalkPreset]] = {
    "gentle": TalkPreset("gentle", 10.0, 60.0, 300.0, 0.25, (0.15, 0.45), 0.45),
    "normal": TalkPreset("normal", 20.0, 100.0, 500.0, 0.15, (0.15, 0.40), 0.36),
    "excited": TalkPreset("excited", 32.0, 150.0, 700.0, 0.08, (0.12, 0.30), 0.34),
}
TALK_MAX_RHYTHM_HZ: Final = 1.5
TALK_LEASE_DEFAULT_S: Final = 10.0
TALK_LEASE_MAX_S: Final = 60.0


@dataclass
class _Segment:
    g0: float
    g1: float
    duration: float
    elapsed: float = 0.0
    pause: bool = False


class TalkGenerator:
    """Procedural mouth-like gripper animation: alternating min-jerk openings and closings with
    randomized size (mostly small, occasionally large) and duration, and brief pauses. Never a new random
    value per tick. Deterministic for a given seed."""

    def __init__(self, preset: TalkPreset, start: float, lo: float, hi: float, seed: int | None = None):
        self.preset = preset
        self.lo, self.hi = lo, hi
        self.rng = random.Random(seed)
        self.value = start
        self._open_next = True
        self._pending: TalkPreset | None = None
        self.segments = 0
        self._seg = self._next(start)

    def set_preset(self, preset: TalkPreset) -> None:
        self._pending = preset  # applies from the next segment (no discontinuity)

    def reset_at(self, value: float) -> None:
        """Pause/resume: restart from ``value`` (the held gripper target) without a jump."""
        self.value = value
        self._open_next = True
        self._seg = self._next(value)

    def _closed(self) -> float:
        return min(max(TALK_CLOSED_LEVEL, self.lo), self.hi)

    def _next(self, g0: float) -> _Segment:
        if self._pending is not None:
            self.preset, self._pending = self._pending, None
        p = self.preset
        self.segments += 1
        if not self._open_next and self.rng.random() < p.pause_probability and self.segments > 1:
            self._open_next = True
            return _Segment(g0, g0, self.rng.uniform(*p.pause_s), pause=True)
        if self._open_next:
            f = self.rng.betavariate(5, 2) if self.rng.random() < 0.10 else self.rng.betavariate(2, 5)
            g1 = self._closed() + p.amplitude * f
        else:
            g1 = self._closed() + p.amplitude * self.rng.uniform(0.0, 0.15)
        g1 = min(max(g1, self.lo), self.hi)
        self._open_next = not self._open_next
        t = min_jerk_duration(g1 - g0, p.v_max, p.a_max, p.segment_min_s) * self.rng.uniform(1.0, 1.5)
        return _Segment(g0, g1, t)

    def step(self, dt: float) -> float:
        remaining = max(dt, 0.0)
        while True:
            seg = self._seg
            take = min(remaining, seg.duration - seg.elapsed)
            seg.elapsed += take
            remaining -= take
            u = seg.elapsed / seg.duration if seg.duration > 0 else 1.0
            self.value = seg.g0 + (seg.g1 - seg.g0) * min_jerk(u)
            if seg.elapsed < seg.duration or remaining <= 0.0:
                if seg.elapsed >= seg.duration:
                    self._seg = self._next(seg.g1)
                return self.value
            self._seg = self._next(seg.g1)


# ======================================================================== front reference
@dataclass(frozen=True)
class FrontReference:
    """The physical direction that means target angle 0: a CLOCKWISE model-frame bearing about the pan axis.
    Persisted per calibration and model identity (a different calibration or model invalidates it)."""

    bearing_deg: float
    calibration_fingerprint: str | None  # None = simulation-only reference
    model_id: str
    set_at: str
    method: str  # "current facing" | "manual" | "default (pan center)"

    def to_json(self) -> dict[str, Any]:
        return {
            "bearing_deg": round(self.bearing_deg, 3),
            "calibration_fingerprint": self.calibration_fingerprint,
            "model_id": self.model_id,
            "set_at": self.set_at,
            "method": self.method,
        }


@dataclass
class FollowState:
    """Follow Me overlay state (owned by the controller thread)."""

    enabled: bool = False
    target_deg: float = 0.0
    target_updated_at: float | None = None
    target_updates: int = 0
    profile: TransitionProfile = TransitionProfile.NORMAL
    direction: Direction | None = None
    suspended: str | None = None  # why tracking is suspended (pan held at its last target)
    solution: FollowSolution | None = None
    pan_command: float | None = None
    handoff: Handoff | None = None
    tracker: PanTracker = field(
        default_factory=lambda: PanTracker(*profile_limits(PAN, TransitionProfile.NORMAL))
    )
    frozen: bool = False
    last_reason: str = ""

    @property
    def engaged(self) -> bool:
        """Follow Me currently writes shoulder_pan (tracking, suspended-hold or handing back)."""
        return self.enabled or self.handoff is not None


@dataclass
class TalkState:
    active: bool = False
    preset: TalkPreset = field(default_factory=lambda: TALK_PRESETS["normal"])
    generator: TalkGenerator | None = None
    lease_until: float | None = None
    started_at: float | None = None
    renewed_at: float | None = None
    gripper_command: float | None = None
    handoff: Handoff | None = None
    frozen: bool = False
    last_reason: str = ""
    seed: int | None = None

    @property
    def engaged(self) -> bool:
        return self.active or self.handoff is not None

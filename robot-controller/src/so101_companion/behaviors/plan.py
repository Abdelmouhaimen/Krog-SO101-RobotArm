"""Effective playback trajectory (M2 contract ``trajectory-clock.md``; ADR-013). Pure and deterministic.

Pipeline (the stored source is never modified)::

    canonical source series -> trim (boundary samples linearly interpolated) -> clamp to the CURRENT
    calibrated bounds (separate, audited, derived copy) -> time / speed -> entry transition from the
    measured start pose + content + [bridge + content]*  -> PlaybackPlan (hash = preview token)

* Recorded content: piecewise LINEAR interpolation at its recorded timing ÷ speed (0.5–2×). It is
  never speed-limited or smoothed (owner D2).
* Entry and loop bridges (owner D8, ADR-015): synchronized joint-space minimum-jerk segments, as SHORT
  as the per-joint limits ``policy.TRANSITION_MAX_SPEED`` / ``TRANSITION_MAX_ACCEL`` allow, at least
  ``policy.TRANSITION_MIN_DURATION_S``. A transition whose poses already match within
  ``LOOP_SEAMLESS_TOL_*`` is skipped (duration 0): the loop repeats immediately.
* Angles are interpolated in the M1 logical frame, never "shortest way round": with the ADR-012
  re-centred calibration the wrist arc is continuous (±163° usable) and the seam sits in the
  mechanical dead zone, so no transition can turn into an unpreviewed ±360° rotation.
* Clamping uses the CALIBRATED bounds only (never the MuJoCo model ranges, owner requirement 5).
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from so101_companion.behaviors.model import JOINT_FRAME_ID, Behavior, Origin, PlaybackSettings, Pose
from so101_companion.behaviors.validate import BehaviorFormatError, settings_problems
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

MIN_JERK_PEAK: Final = 1.875  # max of d/dx (10x³ − 15x⁴ + 6x⁵) at x = 0.5
MIN_JERK_PEAK_ACCEL: Final = 10.0 / math.sqrt(3.0)  # max |d²/dx²| (at x = 1/2 ∓ √3/6) ≈ 5.7735

Bounds = Mapping[str, tuple[float, float]]


class CompatibilityError(ValueError):
    """The behavior cannot be played with the current calibration / context."""


# ------------------------------------------------------------------------------------ segments
@dataclass(frozen=True)
class SmoothTransition:
    start: Pose
    goal: Pose
    duration: float

    @classmethod
    def plan(
        cls,
        start: Mapping[str, float],
        goal: Mapping[str, float],
        profile: TransitionProfile = TransitionProfile.FAST,
    ) -> SmoothTransition:
        """The shortest synchronized min-jerk move within every joint's speed AND acceleration limit
        (D8), scaled down by the operator preset. Poses that already match within the seamless tolerance
        need no transition at all."""
        dist = {n: abs(goal[n] - start[n]) for n in JOINT_NAMES}
        if poses_match(start, goal):
            return cls(dict(start), dict(goal), 0.0)
        v_scale, a_scale = profile.speed_scale, profile.accel_scale
        need = max(
            max(
                MIN_JERK_PEAK * dist[n] / (policy.TRANSITION_MAX_SPEED[n] * v_scale),
                math.sqrt(MIN_JERK_PEAK_ACCEL * dist[n] / (policy.TRANSITION_MAX_ACCEL[n] * a_scale)),
            )
            for n in JOINT_NAMES
        )
        return cls(dict(start), dict(goal), max(profile.min_duration_s, need))

    def at(self, tau: float) -> Pose:
        x = 1.0 if self.duration <= 0 else min(max(tau / self.duration, 0.0), 1.0)
        s = x * x * x * (10.0 - 15.0 * x + 6.0 * x * x)
        return {n: self.start[n] + s * (self.goal[n] - self.start[n]) for n in JOINT_NAMES}

    def peak_speeds(self) -> dict[str, float]:
        """Per-joint peak speed (deg/s, gripper units/s) of this segment."""
        if self.duration <= 0:
            return dict.fromkeys(JOINT_NAMES, 0.0)
        return {n: MIN_JERK_PEAK * abs(self.goal[n] - self.start[n]) / self.duration for n in JOINT_NAMES}

    def peak_accels(self) -> dict[str, float]:
        """Per-joint peak acceleration (deg/s², gripper units/s²) of this segment."""
        if self.duration <= 0:
            return dict.fromkeys(JOINT_NAMES, 0.0)
        d2 = self.duration * self.duration
        return {n: MIN_JERK_PEAK_ACCEL * abs(self.goal[n] - self.start[n]) / d2 for n in JOINT_NAMES}


def poses_match(a: Mapping[str, float], b: Mapping[str, float]) -> bool:
    """True when two poses are within the seamless-loop tolerance on every joint (D8)."""
    return all(
        abs(b[n] - a[n])
        <= (policy.LOOP_SEAMLESS_TOL_GRIPPER if n == "gripper" else policy.LOOP_SEAMLESS_TOL_ARM_DEG)
        for n in JOINT_NAMES
    )


@dataclass(frozen=True)
class LinearContent:
    times: tuple[float, ...]  # playback seconds (source time ÷ speed), times[0] == 0
    poses: tuple[Pose, ...]

    @property
    def duration(self) -> float:
        return self.times[-1]

    def at(self, tau: float) -> Pose:
        if tau <= 0.0:
            return dict(self.poses[0])
        if tau >= self.times[-1]:
            return dict(self.poses[-1])
        i = bisect.bisect_right(self.times, tau)  # times[i-1] <= tau < times[i]
        t0, t1 = self.times[i - 1], self.times[i]
        a, b = self.poses[i - 1], self.poses[i]
        f = (tau - t0) / (t1 - t0)
        return {n: a[n] + (b[n] - a[n]) * f for n in JOINT_NAMES}


def _interp(times: Sequence[float], poses: Sequence[Pose], t: float) -> Pose:
    return LinearContent(tuple(times), tuple(poses)).at(t)


def trimmed_series(b: Behavior, settings: PlaybackSettings) -> tuple[list[float], list[Pose]]:
    """Source series cut to [trim_start, trim_end], boundary samples interpolated, time from 0."""
    problems = settings_problems(settings, b.duration_s)
    if problems:
        raise BehaviorFormatError(problems)
    times, poses = b.series()
    start = settings.trim_start_s
    end = b.duration_s if settings.trim_end_s is None else settings.trim_end_s
    out_t = [0.0]
    out_p = [_interp(times, poses, start)]
    for t, p in zip(times, poses, strict=True):
        if start < t < end:
            out_t.append(t - start)
            out_p.append(dict(p))
    out_t.append(end - start)
    out_p.append(_interp(times, poses, end))
    return out_t, out_p


# ------------------------------------------------------------------------------------ clamp audit
@dataclass(frozen=True)
class ClampChange:
    joint: str
    index: int
    t: float  # source-relative seconds after trim
    before: float
    after: float


@dataclass(frozen=True)
class ClampAudit:
    checked: bool  # False: no calibrated bounds known (disconnected preview) -> nothing clamped
    bounds: Mapping[str, tuple[float, float]] | None
    changes: tuple[ClampChange, ...]

    def by_joint(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for c in self.changes:
            d = out.setdefault(c.joint, {"samples": 0, "max_change": 0.0, "first_t": c.t, "last_t": c.t})
            d["samples"] += 1
            d["max_change"] = max(d["max_change"], abs(c.after - c.before))
            d["first_t"] = min(d["first_t"], c.t)
            d["last_t"] = max(d["last_t"], c.t)
        return out

    def describe(self) -> str:
        if not self.checked:
            return "Calibrated bounds unknown (not connected): nothing clamped — provisional only."
        if not self.changes:
            return "All samples inside the calibrated bounds: nothing clamped."
        parts = []
        for joint, d in self.by_joint().items():
            assert self.bounds is not None
            lo, hi = self.bounds[joint]
            parts.append(
                f"{joint}: {d['samples']:.0f} samples clamped into [{lo:+.1f}, {hi:+.1f}] "
                f"(max change {d['max_change']:.2f}, t={d['first_t']:.2f}–{d['last_t']:.2f}s)"
            )
        return "Clamped to calibrated bounds (effective copy only; raw capture unchanged): " + "; ".join(
            parts
        )


def clamp_series(
    poses: Sequence[Pose], times: Sequence[float], bounds: Bounds | None
) -> tuple[list[Pose], ClampAudit]:
    if bounds is None:
        return [dict(p) for p in poses], ClampAudit(False, None, ())
    out: list[Pose] = []
    changes: list[ClampChange] = []
    for i, (t, p) in enumerate(zip(times, poses, strict=True)):
        q = {}
        for n in JOINT_NAMES:
            lo, hi = bounds[n]
            v = min(max(p[n], lo), hi)
            if v != p[n]:
                changes.append(ClampChange(n, i, t, p[n], v))
            q[n] = v
        out.append(q)
    return out, ClampAudit(True, dict(bounds), tuple(changes))


# ------------------------------------------------------------------------------------ the plan
@dataclass(frozen=True)
class PlanPoint:
    pose: Pose
    segment: str  # "entry" | "content" | "bridge" | "done"
    repetition: int  # 1-based repetition currently served (0 during entry)
    finished: bool


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _rounded(p: Mapping[str, float], nd: int = 6) -> list[float]:
    return [round(p[n], nd) for n in JOINT_NAMES]


@dataclass(frozen=True)
class PlaybackPlan:
    behavior_id: str
    behavior_revision: int
    behavior_name: str
    settings: PlaybackSettings
    start_pose: Pose
    entry: SmoothTransition
    content: LinearContent
    bridge: SmoothTransition
    repeat_count: int | None
    audit: ClampAudit
    calibration_fingerprint: str | None
    content_token: str  # behavior revision + settings + effective content + bounds + calibration
    preview_token: str  # content_token + start pose
    entry_profile: TransitionProfile = TransitionProfile.FAST
    bridge_profile: TransitionProfile = TransitionProfile.FAST

    @property
    def cycle(self) -> float:
        return self.content.duration + self.bridge.duration

    @property
    def total_duration(self) -> float | None:
        if self.repeat_count is None:
            return None
        n = self.repeat_count
        return self.entry.duration + n * self.content.duration + (n - 1) * self.bridge.duration

    def nominal_duration_for(self, repetitions: int) -> float:
        n = max(1, repetitions)
        return self.entry.duration + n * self.content.duration + (n - 1) * self.bridge.duration

    def at(self, tau: float) -> PlanPoint:
        if tau < self.entry.duration:
            return PlanPoint(self.entry.at(tau), "entry", 0, False)
        total = self.total_duration
        if total is not None and tau >= total:
            return PlanPoint(dict(self.content.poses[-1]), "done", self.repeat_count or 1, True)
        u = tau - self.entry.duration
        k = int(u // self.cycle) if self.cycle > 0 else 0
        r = u - k * self.cycle
        if r < self.content.duration:
            return PlanPoint(self.content.at(r), "content", k + 1, False)
        return PlanPoint(self.bridge.at(r - self.content.duration), "bridge", k + 1, False)

    def end_pose(self) -> Pose:
        return dict(self.content.poses[-1])

    def summary(self) -> dict[str, Any]:
        return {
            "behavior_id": self.behavior_id,
            "revision": self.behavior_revision,
            "name": self.behavior_name,
            "settings": self.settings.to_json(),
            "entry_s": round(self.entry.duration, 3),
            "content_s": round(self.content.duration, 3),
            "bridge_s": round(self.bridge.duration, 3),
            "seamless_loop": self.bridge.duration == 0.0,
            "entry_profile": self.entry_profile.value,
            "bridge_profile": self.bridge_profile.value,
            "nominal_total_s": None if self.total_duration is None else round(self.total_duration, 3),
            "clamped_samples": len(self.audit.changes),
            "bounds_checked": self.audit.checked,
        }


def compile_plan(
    b: Behavior,
    settings: PlaybackSettings,
    start_pose: Mapping[str, float],
    *,
    bounds: Bounds | None,
    calibration_fingerprint: str | None,
    entry_profile: TransitionProfile = TransitionProfile.FAST,
    bridge_profile: TransitionProfile = TransitionProfile.FAST,
) -> PlaybackPlan:
    """Build the exact effective trajectory. ``bounds``/``calibration_fingerprint`` come from the
    connected arm; ``None`` means a provisional (disconnected) preview that can never authorize a run."""
    if (
        b.origin is Origin.REAL_HAND_GUIDED
        and calibration_fingerprint is not None
        and b.calibration_fingerprint != calibration_fingerprint
    ):
        raise CompatibilityError(
            f"'{b.name}' was recorded with another calibration ({_short(b.calibration_fingerprint)}); "
            f"the connected arm uses {_short(calibration_fingerprint)}. "
            "Re-record it for this calibration."
        )
    times, poses = trimmed_series(b, settings)
    clamped, audit = clamp_series(poses, times, bounds)
    play_times = tuple(t / settings.speed for t in times)
    content = LinearContent(play_times, tuple(clamped))
    start = {n: float(start_pose[n]) for n in JOINT_NAMES}
    if not all(math.isfinite(v) for v in start.values()):
        raise CompatibilityError("start pose is not a finite six-joint pose")
    entry = SmoothTransition.plan(start, clamped[0], entry_profile)
    bridge = SmoothTransition.plan(clamped[-1], clamped[0], bridge_profile)
    content_token = _digest(
        {
            "frame": JOINT_FRAME_ID,
            "behavior": [b.id, b.revision],
            "settings": settings.to_json(),
            "times": [round(t, 6) for t in play_times],
            "poses": [_rounded(p) for p in clamped],
            "bounds": None if bounds is None else {n: list(bounds[n]) for n in JOINT_NAMES},
            "calibration": calibration_fingerprint,
            "transition": {
                "speed": dict(policy.TRANSITION_MAX_SPEED),
                "accel": dict(policy.TRANSITION_MAX_ACCEL),
                "min_s": policy.TRANSITION_MIN_DURATION_S,
                "seamless": [policy.LOOP_SEAMLESS_TOL_ARM_DEG, policy.LOOP_SEAMLESS_TOL_GRIPPER],
                "entry_profile": entry_profile.value,
                "bridge_profile": bridge_profile.value,
            },
        }
    )
    preview_token = _digest({"content": content_token, "start": _rounded(start, 3)})
    return PlaybackPlan(
        b.id,
        b.revision,
        b.name,
        settings,
        start,
        entry,
        content,
        bridge,
        settings.repeat_count,
        audit,
        calibration_fingerprint,
        content_token,
        preview_token,
        entry_profile,
        bridge_profile,
    )


def _short(fp: str | None) -> str:
    return "none" if not fp else fp.removeprefix("sha256:")[:12]


def pose_deviation(a: Mapping[str, float], b: Mapping[str, float]) -> dict[str, float]:
    """Joints whose difference exceeds the preview-start tolerance (D3), with the difference."""
    out = {}
    for n in JOINT_NAMES:
        tol = policy.PREVIEW_START_TOL_GRIPPER if n == "gripper" else policy.PREVIEW_START_TOL_ARM_DEG
        d = b[n] - a[n]
        if abs(d) > tol:
            out[n] = d
    return out

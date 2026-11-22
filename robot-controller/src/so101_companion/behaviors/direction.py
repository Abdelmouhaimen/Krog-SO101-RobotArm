"""Representative horizontal direction of a behavior / pose (Milestone 2.6, Follow Me; ADR-019). Pure math.

Convention (verified on the official SO-101 model, ``planning/FOLLOW_ME_TALK_DESIGN_REVIEW.md``):

* Angles are degrees, CLOCKWISE seen from above (= the direction of positive shoulder_pan, the robot's
  right). ``psi`` is the bearing of the gripper center measured about the SHOULDER-PAN AXIS (not the base
  origin, 38.8 mm behind it) with shoulder_pan held at its reference 0.
* Because lift/elbow/wrist bend about parallel horizontal axes, psi is always within atan(8.1 mm / rho) of
  0° (NORMAL: gripper in front of the pan axis) or 180° (REVERSED: folded back over it). What matters is
  the SIDE; ambiguity is a side change (crossing over the axis) or too much time near the axis.

The FK itself lives in ``simulation/direction.py``; this module only classifies (bearing, rho, weight)
samples, so the controller and the API can use the result without MuJoCo.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final

ALGORITHM_VERSION: Final = 1
RHO_MIN_M: Final = 0.060  # below this the bearing is ill-defined (error up to atan(8.1/60) = 7.7°)
USABLE_MIN_FRACTION: Final = 0.80  # of the effective (trimmed) duration
MINORITY_MAX_FRACTION: Final = 0.05  # of the usable time on the "other" side before it is MIXED

NORMAL, REVERSED, MIXED, UNDEFINED = "NORMAL", "REVERSED", "MIXED", "UNDEFINED"


def wrap180(a: float) -> float:
    """Wrap an angle (degrees) into [-180, 180)."""
    return (a + 180.0) % 360.0 - 180.0


def circular_distance(a: float, b: float) -> float:
    return abs(wrap180(a - b))


def circular_median(angles: Sequence[float], weights: Sequence[float] | None = None) -> float:
    """argmin_c Σ wᵢ·|wrap180(c − aᵢ)| — the weighted circular median (robust; correct across ±180°).

    The minimum of this piecewise-linear cost lies at one of the samples or at a sample's antipode, so
    those candidates are evaluated exactly (no grid)."""
    if not angles:
        raise ValueError("no angles")
    w = list(weights) if weights is not None else [1.0] * len(angles)
    cands = {round(wrap180(a), 9) for a in angles} | {round(wrap180(a + 180.0), 9) for a in angles}
    best, best_cost = 0.0, math.inf
    for c in sorted(cands):
        cost = sum(wi * circular_distance(c, a) for a, wi in zip(angles, w, strict=True))
        if cost < best_cost - 1e-12:
            best, best_cost = c, cost
    return best


def time_weights(times: Sequence[float]) -> list[float]:
    """Each sample stands for half the interval to each neighbor (irregular sampling handled)."""
    n = len(times)
    if n == 1:
        return [1.0]
    w = [0.0] * n
    w[0] = (times[1] - times[0]) / 2.0
    w[-1] = (times[-1] - times[-2]) / 2.0
    for i in range(1, n - 1):
        w[i] = (times[i + 1] - times[i - 1]) / 2.0
    return [max(x, 0.0) for x in w]


@dataclass(frozen=True)
class Direction:
    psi_deg: (
        float | None
    )  # representative bearing (CW, about the pan axis, pan = reference), None if undefined
    classification: str  # NORMAL | REVERSED | MIXED | UNDEFINED
    usable_fraction: float
    minority_fraction: float
    spread_p90_deg: float
    spread_max_deg: float
    rho_min_mm: float
    rho_median_mm: float
    samples: int
    source: str = ""  # "behavior 'Talking' r3", "pose 'Watching'", "rest pose", "current pose"
    reason: str = ""  # why it is not compatible (empty when compatible)

    @property
    def compatible(self) -> bool:
        return self.classification in (NORMAL, REVERSED) and self.psi_deg is not None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["compatible"] = self.compatible
        for k in ("psi_deg", "usable_fraction", "minority_fraction", "spread_p90_deg", "spread_max_deg"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        d["rho_min_mm"] = round(self.rho_min_mm, 1)
        d["rho_median_mm"] = round(self.rho_median_mm, 1)
        return d


def classify(
    bearings: Sequence[float],
    rhos_m: Sequence[float],
    weights: Sequence[float],
    *,
    source: str = "",
) -> Direction:
    """Classify per-sample (bearing CW°, horizontal distance to the pan axis m, weight) — see module doc."""
    n = len(bearings)
    if n == 0:
        return Direction(None, UNDEFINED, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, source, "no samples")
    total = sum(weights) or float(n)
    if sum(weights) == 0:
        weights = [1.0] * n
    valid = [i for i in range(n) if rhos_m[i] >= RHO_MIN_M]
    usable_w = sum(weights[i] for i in valid)
    usable = usable_w / total
    rho_sorted = sorted(rhos_m)
    rho_min, rho_med = rho_sorted[0] * 1000.0, rho_sorted[n // 2] * 1000.0
    if not valid:
        return Direction(
            None,
            UNDEFINED,
            usable,
            0.0,
            0.0,
            0.0,
            rho_min,
            rho_med,
            n,
            source,
            f"gripper within {RHO_MIN_M * 1000:.0f} mm of the shoulder-pan axis for "
            f"{(1 - usable) * 100:.0f}% of the time — horizontal direction undefined",
        )
    front = sum(weights[i] for i in valid if abs(wrap180(bearings[i])) < 90.0)
    back = usable_w - front
    minority = min(front, back) / usable_w
    majority_side = NORMAL if front >= back else REVERSED
    crossing = minority > MINORITY_MAX_FRACTION
    if not crossing and usable < USABLE_MIN_FRACTION:
        return Direction(
            None,
            UNDEFINED,
            usable,
            0.0,
            0.0,
            0.0,
            rho_min,
            rho_med,
            n,
            source,
            f"gripper within {RHO_MIN_M * 1000:.0f} mm of the shoulder-pan axis for "
            f"{(1 - usable) * 100:.0f}% of the time — horizontal direction undefined",
        )
    psi = circular_median([bearings[i] for i in valid], [weights[i] for i in valid])
    devs = sorted((circular_distance(bearings[i], psi), weights[i]) for i in valid)
    acc, p90 = 0.0, devs[-1][0]
    for d, wi in devs:
        acc += wi
        if acc >= 0.9 * usable_w:
            p90 = d
            break
    if crossing:
        return Direction(
            None,
            MIXED,
            usable,
            minority,
            p90,
            devs[-1][0],
            rho_min,
            rho_med,
            n,
            source,
            f"the gripper crosses over the shoulder-pan axis ({minority * 100:.0f}% of the usable time "
            "on the "
            "opposite side) — no single facing reference",
        )
    return Direction(psi, majority_side, usable, minority, p90, devs[-1][0], rho_min, rho_med, n, source, "")

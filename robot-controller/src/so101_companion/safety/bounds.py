"""Per-joint allowed ranges in LOGICAL units, with the approved 5° margin on angular joints only.

Two sources, never silently substituted for each other:

* ``CALIBRATED`` — derived from an existing, validated LeRobot calibration in exactly the convention
  that ``SOFollower.send_action`` accepts (see ``devices.calibration.calibrated_degree_range``).
  Required for any hardware motion.
* ``SIM_PROVISIONAL`` — the official MJCF ``jnt_range`` mapped to logical degrees. Labelled
  provisional; used only for SIM_ONLY sliders/staging, never for hardware.

The gripper range is always 0..100 with NO margin (SR-07).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from so101_companion.domain.joints import ARM_JOINTS, GRIPPER, JOINT_NAMES

GRIPPER_RANGE = (0.0, 100.0)
LABEL_CALIBRATED = "CALIBRATED (LeRobot calibration file, 5° margin)"
LABEL_SIM = "SIM_PROVISIONAL (official MJCF ranges, 5° margin — not physical limits)"


class InvertedBoundsError(ValueError):
    pass


@dataclass(frozen=True)
class JointLimit:
    lower: float
    upper: float
    source: str

    def __post_init__(self) -> None:
        if not self.lower <= self.upper:
            raise InvertedBoundsError(f"lower {self.lower} > upper {self.upper} ({self.source})")


def apply_margin(lower: float, upper: float, margin: float, source: str) -> JointLimit:
    """Shrink [lower, upper] by ``margin`` on both sides. Rejects ranges that would invert."""
    if margin < 0:
        raise ValueError("margin must be >= 0")
    lo, hi = lower + margin, upper - margin
    if lo > hi:
        raise InvertedBoundsError(
            f"range [{lower:.2f}, {upper:.2f}] is narrower than 2 x {margin}° margin ({source})"
        )
    return JointLimit(lo, hi, f"{source} ±{margin:g}° margin")


@dataclass(frozen=True)
class JointBounds:
    limits: Mapping[str, JointLimit]
    label: str

    def __post_init__(self) -> None:
        if set(self.limits) != set(JOINT_NAMES):
            raise ValueError("JointBounds must cover exactly the six joints")
        g = self.limits[GRIPPER]
        if (g.lower, g.upper) != GRIPPER_RANGE:
            raise ValueError("Gripper bounds must be exactly 0..100 (no margin)")
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    def as_tuples(self) -> dict[str, tuple[float, float]]:
        return {n: (self.limits[n].lower, self.limits[n].upper) for n in JOINT_NAMES}

    def clamp(
        self, values: Mapping[str, float], hold_outside: Mapping[str, float] | None = None
    ) -> tuple[dict[str, float], list[str]]:
        """Clamp every joint into its range.

        ``hold_outside`` (the current staged pose) implements the defensive "no outward ratchet"
        rule (ADR-007): if a joint currently sits OUTSIDE its range (e.g. a rest pose against a
        mechanical stop, inside the 5° margin), its range is widened just enough to include where it
        already is. The joint may then move inward or stay, but is never commanded further outward
        and is never forced to move just because movement was enabled.
        """
        out: dict[str, float] = {}
        reasons: list[str] = []
        for name in JOINT_NAMES:
            lim = self.limits[name]
            lo, hi = lim.lower, lim.upper
            if hold_outside is not None and name in hold_outside:
                current = hold_outside[name]
                lo, hi = min(lo, current), max(hi, current)
            v = values[name]
            if v < lo:
                out[name] = lo
                reasons.append(f"{name}:range_min")
            elif v > hi:
                out[name] = hi
                reasons.append(f"{name}:range_max")
            else:
                out[name] = v
        return out, reasons

    def outside(self, values: Mapping[str, float]) -> list[str]:
        return [n for n in JOINT_NAMES if not self.limits[n].lower <= values[n] <= self.limits[n].upper]


def gripper_limit() -> JointLimit:
    return JointLimit(*GRIPPER_RANGE, "LeRobot RANGE_0_100 (no margin)")


def restrict(bounds: JointBounds, ranges: Mapping[str, tuple[float, float]], why: str) -> JointBounds:
    """Intersect selected joints' limits with fixed ranges (never widens a limit)."""
    limits = dict(bounds.limits)
    for name, (lo, hi) in ranges.items():
        cur = limits[name]
        new_lo, new_hi = max(cur.lower, lo), min(cur.upper, hi)
        if (new_lo, new_hi) != (cur.lower, cur.upper):
            limits[name] = JointLimit(new_lo, new_hi, f"{cur.source}; {why} [{lo:g}, {hi:g}]")
    return JointBounds(limits, bounds.label)


def build_bounds(
    arm_ranges: Mapping[str, tuple[float, float]], margin: float, source: str, label: str
) -> JointBounds:
    limits = {n: apply_margin(*arm_ranges[n], margin, source) for n in ARM_JOINTS}
    limits[GRIPPER] = gripper_limit()
    return JointBounds(limits, label)

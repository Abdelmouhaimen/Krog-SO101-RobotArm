"""Logical joint contract (milestone-1/contracts/interfaces.md).

Six named joints, always all six. The five arm joints use LeRobot calibrated DEGREES
(``use_degrees=True``); the gripper uses LeRobot's normalized 0-100 range (``RANGE_0_100``), which
is NOT an angle. Values are validated finite floats; booleans/strings/NaN/inf are rejected.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

JOINT_NAMES: Final[tuple[str, ...]] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)
ARM_JOINTS: Final[tuple[str, ...]] = JOINT_NAMES[:5]
GRIPPER: Final = "gripper"

Unit = Literal["deg", "pct"]
UNIT_DEG: Final[Unit] = "deg"
UNIT_PCT: Final[Unit] = "pct"  # LeRobot gripper normalized 0..100


def unit_of(name: str) -> Unit:
    if name == GRIPPER:
        return UNIT_PCT
    if name in ARM_JOINTS:
        return UNIT_DEG
    raise KeyError(f"Unknown joint {name!r}")


def unit_label(name: str) -> str:
    return "°" if unit_of(name) == UNIT_DEG else " %"


class JointFrameError(ValueError):
    """Invalid joint vector (shape, names, units or numeric validity)."""


def validate_joint_values(values: Mapping[str, Any]) -> dict[str, float]:
    """Return a plain dict of exactly the six joints as finite floats, in canonical order."""
    if not isinstance(values, Mapping):
        raise JointFrameError(f"Joint values must be a mapping, got {type(values).__name__}")
    keys = set(values)
    missing = [n for n in JOINT_NAMES if n not in keys]
    extra = sorted(str(k) for k in keys - set(JOINT_NAMES))
    if missing or extra:
        raise JointFrameError(f"Joint set mismatch: missing={missing} unexpected={extra}")
    out: dict[str, float] = {}
    for name in JOINT_NAMES:
        raw = values[name]
        # numbers.Real also accepts numpy scalars (np.float32/np.int64) a driver might return.
        if isinstance(raw, bool) or type(raw).__name__ == "bool_" or not isinstance(raw, numbers.Real):
            raise JointFrameError(f"{name}: expected a real number, got {type(raw).__name__}")
        value = float(raw)
        if not math.isfinite(value):
            raise JointFrameError(f"{name}: non-finite value {raw!r}")
        out[name] = value
    return out


@dataclass(frozen=True)
class JointFrame:
    """Immutable, validated six-joint vector in LOGICAL units with a monotonic timestamp and sequence."""

    values: Mapping[str, float]
    timestamp_mono_s: float
    seq: int = 0

    def __post_init__(self) -> None:
        clean = validate_joint_values(self.values)
        if isinstance(self.timestamp_mono_s, bool) or not math.isfinite(float(self.timestamp_mono_s)):
            raise JointFrameError(f"Invalid timestamp {self.timestamp_mono_s!r}")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise JointFrameError(f"Invalid sequence number {self.seq!r}")
        object.__setattr__(self, "values", MappingProxyType(clean))

    def __getitem__(self, name: str) -> float:
        return self.values[name]

    def as_dict(self) -> dict[str, float]:
        return dict(self.values)

    @classmethod
    def from_tagged(
        cls, tagged: Mapping[str, tuple[float, str]], timestamp_mono_s: float, seq: int = 0
    ) -> JointFrame:
        """Build from ``{name: (value, unit)}`` and reject values tagged with the wrong unit
        (e.g. a gripper value tagged ``deg`` or an arm angle tagged ``rad``/``pct``)."""
        values: dict[str, Any] = {}
        for name, item in tagged.items():
            if not (isinstance(item, tuple) and len(item) == 2):
                raise JointFrameError(f"{name}: expected (value, unit) tuple")
            value, unit = item
            if name in JOINT_NAMES and unit != unit_of(name):
                raise JointFrameError(f"{name}: unit {unit!r} is not the expected {unit_of(name)!r}")
            values[name] = value
        return cls(values, timestamp_mono_s, seq)


def format_value(name: str, value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}°" if unit_of(name) == UNIT_DEG else f"{value:.1f} %"

"""Behavior asset model, schema v1 (M2 contract ``contracts/behavior-format.md``, ADR-013).

Pure data: no Qt, no MuJoCo, no serial. A behavior keeps its captured ``samples`` IMMUTABLE for
recorded origins (hand-guided or slider recording); editing changes only ``settings`` (trim, speed,
repeats) and name/description, each change appended to the ``edits`` log with a new ``revision``.
For ``sim_keyframes`` the keyframes are the authored source; keyframe edits replace them and the
previous values are kept in the edit log entry.

Canonical channel: physical hand guidance -> ``actual`` (measured; ``requested``/``sent`` are null
because torque is off and nothing is commanded); simulation authoring -> ``requested`` (the authored
target; ``actual`` is null — the simulation has no encoder).
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from so101_companion.domain.joints import GRIPPER, JOINT_NAMES

SCHEMA_VERSION: Final = 1
ROBOT_MODEL: Final = "SO-101"
# The M1 logical joint frame (ADR-006): what the numbers in a behavior mean.
JOINT_FRAME_ID: Final = "so101-logical-v1 (arm: LeRobot 0.4.4 calibrated DEGREES; gripper: RANGE_0_100)"
JOINT_UNITS: Final[Mapping[str, str]] = {
    n: ("normalized_0_100" if n == GRIPPER else "deg") for n in JOINT_NAMES
}
REPEAT_MAX: Final = 1000


class Origin(enum.StrEnum):
    REAL_HAND_GUIDED = "real_hand_guided"
    SIM_SLIDERS = "sim_sliders"
    SIM_KEYFRAMES = "sim_keyframes"

    @property
    def canonical_channel(self) -> str:
        return "actual" if self is Origin.REAL_HAND_GUIDED else "requested"

    @property
    def is_recording(self) -> bool:
        """Timed captures (gap rule applies); keyframes are sparse by nature."""
        return self is not Origin.SIM_KEYFRAMES


Pose = dict[str, float]


@dataclass(frozen=True)
class Sample:
    t: float  # seconds since the first sample (monotonic-relative)
    actual: Pose | None = None
    requested: Pose | None = None
    sent: Pose | None = None

    def channel(self, name: str) -> Pose | None:
        value: Pose | None = getattr(self, name)
        return value

    def to_json(self) -> dict[str, Any]:
        return {
            "t_seconds": self.t,
            "actual": self.actual,
            "requested": self.requested,
            "sent": self.sent,
        }


@dataclass(frozen=True)
class PlaybackSettings:
    """Effective-trajectory settings. None of them changes the stored source samples."""

    trim_start_s: float = 0.0
    trim_end_s: float | None = None  # None = end of the source
    speed: float = 1.0  # 0.5 .. 2.0 (policy.PLAYBACK_SPEED_MIN/MAX)
    repeat_count: int | None = 1  # None = repeat until stopped
    # "After behavior" (Studio, ADR-017): what happens when ALL repetitions COMPLETED. A normal
    # interruption (Stop / Go Rest) always returns to the default rest pose instead.
    after_action: str = "rest"  # AFTER_ACTIONS: rest | hold | pose
    after_pose_id: str | None = None  # the named pose for after_action == "pose"

    def to_json(self) -> dict[str, Any]:
        return {
            "trim_start_s": self.trim_start_s,
            "trim_end_s": self.trim_end_s,
            "speed": self.speed,
            "repeat_count": self.repeat_count,
            "after": {"action": self.after_action, "pose_id": self.after_pose_id},
        }

    def describe(self) -> str:
        reps = "until stopped" if self.repeat_count is None else f"×{self.repeat_count}"
        end = "end" if self.trim_end_s is None else f"{self.trim_end_s:.2f}s"
        trim = f"trim {self.trim_start_s:.2f}s–{end}"
        return f"speed {self.speed:g}×, repeat {reps}, {trim}, after: {self.after_action}"


AFTER_ACTIONS = ("rest", "hold", "pose")


@dataclass(frozen=True)
class EditEntry:
    revision: int
    at_utc: str
    kind: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "at_utc": self.at_utc,
            "kind": self.kind,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class Behavior:
    id: str
    name: str
    description: str
    created_at_utc: str
    updated_at_utc: str
    revision: int
    origin: Origin
    samples: tuple[Sample, ...]
    settings: PlaybackSettings = PlaybackSettings()
    calibration_fingerprint: str | None = None
    calibration_robot_id: str | None = None
    joint_frame: str = JOINT_FRAME_ID
    app_version: str | None = None
    quality: Mapping[str, Any] = field(default_factory=dict)
    edits: tuple[EditEntry, ...] = ()

    @property
    def channel(self) -> str:
        return self.origin.canonical_channel

    def series(self) -> tuple[list[float], list[Pose]]:
        """The canonical source series (times, six-joint poses)."""
        times: list[float] = []
        poses: list[Pose] = []
        for s in self.samples:
            pose = s.channel(self.channel)
            if pose is None:  # validated away; defensive
                raise ValueError(f"sample at t={s.t} lacks the canonical {self.channel!r} channel")
            times.append(s.t)
            poses.append(dict(pose))
        return times, poses

    @property
    def duration_s(self) -> float:
        return self.samples[-1].t - self.samples[0].t if self.samples else 0.0

    def with_changes(self, **changes: Any) -> Behavior:
        return replace(self, **changes)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "revision": self.revision,
            "origin": self.origin.value,
            "robot_model": ROBOT_MODEL,
            "joint_frame": self.joint_frame,
            "joint_units": dict(JOINT_UNITS),
            "calibration_fingerprint": self.calibration_fingerprint,
            "calibration_robot_id": self.calibration_robot_id,
            "app_version": self.app_version,
            "duration_s": round(self.duration_s, 6),
            "sample_count": len(self.samples),
            "quality": dict(self.quality),
            "settings": self.settings.to_json(),
            "samples": [s.to_json() for s in self.samples],
            "edits": [e.to_json() for e in self.edits],
        }


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)

"""Persistent physical rest pose (T-039, implemented 2026-11-07 with the arm present).

The pose is RECORDED from the measured arm (never invented), clamped to the calibrated bounds
(owner decision: joints resting inside the 5° margin are saved at the bound), and stored per LeRobot
calibration identity, so a pose recorded for one calibration is never used with another.

Since M2 (owner decision D5, ADR-014) a rest pose also carries the CALIBRATION FINGERPRINT
(``devices.calibration.calibration_fingerprint``). The robot id alone is not enough: the ADR-012 wrist
re-centring changed the calibration but kept the id. A file without a fingerprint ("legacy") is never
used silently; the operator must explicitly adopt it for the current calibration (the original file is
kept as a backup) or record a new one. Saving never destroys an existing file: it is backed up first.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from so101_companion.domain.joints import JOINT_NAMES, validate_joint_values


class RestPoseError(ValueError):
    pass


@dataclass(frozen=True)
class RestPose:
    robot_id: str
    pose: dict[str, float]  # clamped, commandable target (logical units)
    measured: dict[str, float]  # what was actually measured when recording
    clamped_joints: tuple[str, ...]
    recorded_at: str
    calibration_fingerprint: str | None = None  # None = legacy file (pre-M2): needs explicit adoption
    provenance: dict[str, str] = field(default_factory=dict)  # e.g. migration record

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "robot_id": self.robot_id,
            "calibration_fingerprint": self.calibration_fingerprint,
            "pose": self.pose,
            "measured": self.measured,
            "clamped_joints": list(self.clamped_joints),
            "recorded_at": self.recorded_at,
            "units": "arm: LeRobot calibrated degrees; gripper: LeRobot 0..100",
        }
        if self.provenance:
            out["provenance"] = dict(self.provenance)
        return out

    @classmethod
    def from_json(cls, data: Any) -> RestPose:
        try:
            pose = validate_joint_values(data["pose"])
            measured = validate_joint_values(data["measured"])
            clamped = tuple(str(j) for j in data.get("clamped_joints", []))
            robot_id = str(data["robot_id"])
            recorded_at = str(data["recorded_at"])
            fingerprint = data.get("calibration_fingerprint")
            provenance = {str(k): str(v) for k, v in dict(data.get("provenance") or {}).items()}
            if "note" in data:  # free-text note written by hand (e.g. the ADR-012 wrist shift)
                provenance.setdefault("note", str(data["note"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RestPoseError(f"invalid rest pose file: {exc}") from exc
        if not set(clamped) <= set(JOINT_NAMES) or not robot_id:
            raise RestPoseError("invalid rest pose file: bad robot id or joint names")
        if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
            raise RestPoseError("invalid rest pose file: bad calibration_fingerprint")
        return cls(robot_id, pose, measured, clamped, recorded_at, fingerprint, provenance)


class RestPoseStore(Protocol):
    def load(self, robot_id: str) -> RestPose | None: ...
    def save(self, rest: RestPose) -> str: ...


@dataclass
class FileRestPoseStore:
    directory: Path
    last_backup: str | None = None  # path of the backup made by the latest save(), if any

    def path_for(self, robot_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in robot_id)
        return Path(self.directory) / f"rest_pose.{safe}.json"

    def load(self, robot_id: str) -> RestPose | None:
        p = self.path_for(robot_id)
        if not p.is_file():
            return None
        try:
            rest = RestPose.from_json(json.loads(p.read_text()))
        except json.JSONDecodeError as exc:
            raise RestPoseError(f"{p}: corrupt JSON: {exc}") from exc
        if rest.robot_id != robot_id:
            raise RestPoseError(f"{p} belongs to robot id {rest.robot_id!r}, not {robot_id!r}")
        return rest

    def save(self, rest: RestPose) -> str:
        """Atomically write ``rest``. An existing file is first copied to a timestamped backup
        (``rest_pose.<id>.bak-<time>.json``) — a rest pose is never destroyed by saving."""
        p = self.path_for(rest.robot_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            self.last_backup = self._backup(p)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rest.to_json(), indent=2) + "\n")
        tmp.replace(p)
        return str(p)

    @staticmethod
    def _backup(p: Path) -> str:
        import shutil

        stamp = time.strftime("%Y%m%dT%H%M%S")
        stem = p.name.removesuffix(".json")
        backup = p.with_name(f"{stem}.bak-{stamp}.json")
        n = 1
        while backup.exists():
            n += 1
            backup = p.with_name(f"{stem}.bak-{stamp}-{n}.json")
        shutil.copy2(p, backup)
        return str(backup)


@dataclass
class MemoryRestPoseStore:
    poses: dict[str, RestPose] = field(default_factory=dict)

    def load(self, robot_id: str) -> RestPose | None:
        return self.poses.get(robot_id)

    def save(self, rest: RestPose) -> str:
        self.poses[rest.robot_id] = rest
        return f"memory:{rest.robot_id}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@dataclass(frozen=True)
class RestTrajectory:
    """Straight joint-space line from ``start`` to ``goal``; all joints arrive together."""

    start: dict[str, float]
    goal: dict[str, float]
    t0: float
    duration: float

    @classmethod
    def plan(
        cls, start: dict[str, float], goal: dict[str, float], t0: float, speed: float, min_duration: float
    ) -> RestTrajectory:
        longest = max(abs(goal[n] - start[n]) for n in JOINT_NAMES)
        return cls(dict(start), dict(goal), t0, max(min_duration, longest / speed))

    def at(self, t: float) -> dict[str, float]:
        s = 1.0 if self.duration <= 0 else min(max((t - self.t0) / self.duration, 0.0), 1.0)
        return {n: self.start[n] + s * (self.goal[n] - self.start[n]) for n in JOINT_NAMES}

    def done(self, t: float) -> bool:
        return t >= self.t0 + self.duration


def within(actual: dict[str, float], goal: dict[str, float], arm_tol: float, gripper_tol: float) -> bool:
    return all(
        math.isfinite(actual[n]) and abs(actual[n] - goal[n]) <= (gripper_tol if n == "gripper" else arm_tol)
        for n in JOINT_NAMES
    )

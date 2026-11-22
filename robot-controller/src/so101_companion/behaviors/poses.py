"""Named pose library (Studio, ADR-017). Pure: no Qt, no hardware.

A ``NamedPose`` is six joint positions in the M1 logical frame (arm: calibrated degrees, gripper 0..100)
plus metadata: stable id, name, where it came from (the simulation or the real arm's measured pose) and,
for real-arm poses, the calibration fingerprint they were captured with. Files live as
``poses/<id>.json`` (local, gitignored), written atomically.

Before a pose is used on the real arm, ``pose_problems_for_arm`` checks it against the CURRENT
calibration: a pose captured under another calibration is refused (the same numbers would mean another
physical pose), and every joint must lie inside the calibrated bounds (a pose is never silently clamped).
"""

from __future__ import annotations

import enum
import json
import math
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from so101_companion.domain.joints import JOINT_NAMES

POSE_SCHEMA_VERSION = 1
BOUNDS_EPS = 1e-6


class PoseFormatError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class PoseSource(enum.StrEnum):
    SIMULATION = "simulation"  # the 3D arm's pose (Simulation Only)
    REAL_ARM = "real_arm"  # the arm's measured pose (clamped to the calibrated bounds when saved)


@dataclass(frozen=True)
class NamedPose:
    id: str
    name: str
    pose: dict[str, float]
    source: PoseSource
    calibration_fingerprint: str | None
    created_at_utc: str
    updated_at_utc: str
    clamped_joints: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": POSE_SCHEMA_VERSION,
            "kind": "so101_named_pose",
            "id": self.id,
            "name": self.name,
            "pose": {n: self.pose[n] for n in JOINT_NAMES},
            "joint_units": {"arm": "calibrated_degrees", "gripper": "lerobot_0_100"},
            "source": self.source.value,
            "calibration_fingerprint": self.calibration_fingerprint,
            "clamped_joints": list(self.clamped_joints),
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
        }

    def target(self) -> PoseTarget:
        return PoseTarget(self.id, self.name, dict(self.pose), self.calibration_fingerprint)


@dataclass(frozen=True)
class PoseTarget:
    """What the controller needs to move to (or end a behavior at) a named pose."""

    id: str
    name: str
    pose: dict[str, float]
    calibration_fingerprint: str | None


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_pose_values(values: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
    problems: list[str] = []
    pose: dict[str, float] = {}
    for n in JOINT_NAMES:
        v = values.get(n)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            problems.append(f"{n}: expected a finite number, got {v!r}")
        else:
            pose[n] = float(v)
    extra = set(values) - set(JOINT_NAMES)
    if extra:
        problems.append(f"unknown joints {sorted(extra)}")
    return pose, problems


def pose_from_json(data: Any) -> NamedPose:
    if not isinstance(data, dict):
        raise PoseFormatError(["a pose file must contain a JSON object"])
    problems: list[str] = []
    if data.get("schema_version") != POSE_SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {data.get('schema_version')!r}")
    pid, name = data.get("id"), data.get("name")
    if not isinstance(pid, str) or not pid:
        problems.append("id must be a non-empty string")
    if not isinstance(name, str) or not name.strip():
        problems.append("name must be a non-empty string")
    raw = data.get("pose")
    pose, pose_problems = validate_pose_values(raw if isinstance(raw, dict) else {})
    problems += pose_problems
    try:
        source = PoseSource(str(data.get("source")))
    except ValueError:
        problems.append(f"unknown source {data.get('source')!r}")
        source = PoseSource.SIMULATION
    fp = data.get("calibration_fingerprint")
    if fp is not None and not isinstance(fp, str):
        problems.append("calibration_fingerprint must be a string or null")
    if source is PoseSource.REAL_ARM and not fp:
        problems.append("a real-arm pose must carry the calibration fingerprint it was captured with")
    if problems:
        raise PoseFormatError(problems)
    return NamedPose(
        id=str(pid),
        name=str(name).strip(),
        pose=pose,
        source=source,
        calibration_fingerprint=fp,
        created_at_utc=str(data.get("created_at_utc", "")),
        updated_at_utc=str(data.get("updated_at_utc", "")),
        clamped_joints=tuple(str(j) for j in data.get("clamped_joints", []) or []),
    )


def pose_problems_for_arm(
    pose: NamedPose | PoseTarget,
    calibration_fingerprint: str | None,
    bounds: Mapping[str, tuple[float, float]] | None,
) -> list[str]:
    """Why this pose must NOT be used on the connected arm ([] = usable)."""
    problems: list[str] = []
    fp = pose.calibration_fingerprint
    if fp is not None and calibration_fingerprint is not None and fp != calibration_fingerprint:
        problems.append(
            f"pose '{pose.name}' was saved with another calibration ({fp.removeprefix('sha256:')[:12]}); "
            "save it again for this calibration"
        )
    if bounds is not None:
        for n in JOINT_NAMES:
            lo, hi = bounds[n]
            v = pose.pose[n]
            if not lo - BOUNDS_EPS <= v <= hi + BOUNDS_EPS:
                problems.append(
                    f"pose '{pose.name}': {n} {v:+.1f} is outside this arm's range [{lo:+.1f}, {hi:+.1f}]"
                )
    return problems


class PoseRepository:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path_for(self, pose_id: str) -> Path:
        if not pose_id or "/" in pose_id or pose_id.startswith("."):
            raise PoseFormatError([f"invalid pose id {pose_id!r}"])
        return self.directory / f"{pose_id}.json"

    def list(self) -> tuple[list[NamedPose], list[str]]:
        poses: list[NamedPose] = []
        problems: list[str] = []
        if not self.directory.is_dir():
            return poses, problems
        for path in sorted(self.directory.glob("*.json")):
            try:
                poses.append(pose_from_json(json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError, PoseFormatError) as exc:
                problems.append(f"{path.name}: {exc}")
        poses.sort(key=lambda p: (p.name.lower(), p.created_at_utc))
        return poses, problems

    def exists(self, pose_id: str) -> bool:
        try:
            return self.path_for(pose_id).is_file()
        except PoseFormatError:
            return False

    def load(self, pose_id: str) -> NamedPose:
        path = self.path_for(pose_id)
        try:
            return pose_from_json(json.loads(path.read_text()))
        except FileNotFoundError as exc:
            raise PoseFormatError([f"pose {pose_id} does not exist (deleted?)"]) from exc
        except json.JSONDecodeError as exc:
            raise PoseFormatError([f"pose {pose_id}: {exc}"]) from exc

    def create(
        self,
        name: str,
        values: Mapping[str, float],
        source: PoseSource,
        *,
        calibration_fingerprint: str | None = None,
        clamped_joints: tuple[str, ...] = (),
    ) -> NamedPose:
        pose, problems = validate_pose_values(values)
        if not name.strip():
            problems.append("name must not be empty")
        if source is PoseSource.REAL_ARM and not calibration_fingerprint:
            problems.append("a real-arm pose needs the calibration fingerprint")
        if problems:
            raise PoseFormatError(problems)
        now = utc_now()
        p = NamedPose(
            str(uuid.uuid4()), name.strip(), pose, source, calibration_fingerprint, now, now, clamped_joints
        )
        self._write(p)
        return p

    def rename(self, pose_id: str, name: str) -> NamedPose:
        if not name.strip():
            raise PoseFormatError(["name must not be empty"])
        p = replace(self.load(pose_id), name=name.strip(), updated_at_utc=utc_now())
        self._write(p)
        return p

    def delete(self, pose_id: str) -> None:
        self.path_for(pose_id).unlink()

    def _write(self, p: NamedPose) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".pose-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(p.to_json(), f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path_for(p.id))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

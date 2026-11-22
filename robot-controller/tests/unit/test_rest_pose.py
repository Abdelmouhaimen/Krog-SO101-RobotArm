"""Rest pose persistence and the timed return trajectory (T-039, ADR-011)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from so101_companion.devices.rest_pose import (
    FileRestPoseStore,
    RestPose,
    RestPoseError,
    RestTrajectory,
    within,
)
from so101_companion.domain.joints import JOINT_NAMES

POSE = {
    "shoulder_pan": 10.0,
    "shoulder_lift": -90.0,
    "elbow_flex": 85.0,
    "wrist_flex": 60.0,
    "wrist_roll": -70.0,
    "gripper": 5.0,
}


def rest(robot_id: str = "left_follower") -> RestPose:
    return RestPose(robot_id, dict(POSE), {**POSE, "shoulder_lift": -103.8}, ("shoulder_lift",), "2026-11-07")


def test_file_store_round_trip_and_identity(tmp_path: Path) -> None:
    store = FileRestPoseStore(tmp_path)
    location = store.save(rest())
    assert Path(location).name == "rest_pose.left_follower.json"
    loaded = store.load("left_follower")
    assert loaded == rest()
    assert store.load("other_arm") is None  # never used for another calibration


def test_file_store_rejects_corrupt_or_foreign_files(tmp_path: Path) -> None:
    store = FileRestPoseStore(tmp_path)
    store.path_for("x").write_text("{nope")
    with pytest.raises(RestPoseError):
        store.load("x")
    store.path_for("y").write_text(json.dumps(rest("someone_else").to_json()))
    with pytest.raises(RestPoseError, match="belongs to"):
        store.load("y")
    bad = rest().to_json() | {"pose": {**POSE, "elbow_flex": float("nan")}}
    store.path_for("z").write_text(json.dumps(bad).replace("NaN", "NaN"))
    with pytest.raises(RestPoseError):
        store.load("z")


def test_trajectory_is_synchronized_and_speed_bounded() -> None:
    start = dict.fromkeys(JOINT_NAMES, 0.0)
    goal = {**start, "shoulder_pan": 90.0, "elbow_flex": -30.0, "gripper": 15.0}
    traj = RestTrajectory.plan(start, goal, t0=10.0, speed=30.0, min_duration=0.5)
    assert traj.duration == pytest.approx(3.0)  # longest joint (90°) at 30°/s
    mid = traj.at(11.5)
    assert mid["shoulder_pan"] == pytest.approx(45.0) and mid["elbow_flex"] == pytest.approx(-15.0)
    assert traj.at(9.0) == start and traj.at(20.0) == goal  # clamped before/after
    # per 50 ms tick no joint moves more than 30°/s * 0.05 s
    pts = [traj.at(10.0 + k * 0.05) for k in range(70)]
    assert max(abs(b[n] - a[n]) for a, b in itertools.pairwise(pts) for n in JOINT_NAMES) <= 1.5 + 1e-9
    assert not traj.done(12.9) and traj.done(13.0)


def test_short_trajectory_has_minimum_duration() -> None:
    start = dict.fromkeys(JOINT_NAMES, 0.0)
    traj = RestTrajectory.plan(start, {**start, "wrist_flex": 1.0}, 0.0, 30.0, 0.5)
    assert traj.duration == 0.5


def test_within_uses_separate_gripper_tolerance() -> None:
    goal = dict(POSE)
    assert within({**goal, "elbow_flex": 87.9, "gripper": 9.9}, goal, 3.0, 5.0)
    assert not within({**goal, "elbow_flex": 88.1}, goal, 3.0, 5.0)
    assert not within({**goal, "gripper": 10.1}, goal, 3.0, 5.0)

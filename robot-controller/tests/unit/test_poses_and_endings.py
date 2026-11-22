"""ADR-017 (pure parts): named pose library, calibration checks, the per-behavior "After behavior" setting
in the file format (incl. compatibility with existing files) and the preview of each ending."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.plan import compile_plan
from so101_companion.behaviors.poses import (
    PoseFormatError,
    PoseRepository,
    PoseSource,
    pose_problems_for_arm,
)
from so101_companion.behaviors.preview import PreviewSession
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.validate import behavior_from_json, settings_problems
from so101_companion.domain.joints import JOINT_NAMES

FP = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
ZERO = dict.fromkeys(JOINT_NAMES, 0.0)


def pose(**v: float) -> dict[str, float]:
    return ZERO | v


def wave(after: str = "rest", pose_id: str | None = None, reps: int | None = 1):  # type: ignore[no-untyped-def]
    samples = tuple(Sample(round(i * 0.05, 6), actual=pose(elbow_flex=float(i))) for i in range(21))
    b = new_behavior("wave", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=FP)
    return b.with_changes(
        settings=PlaybackSettings(repeat_count=reps, after_action=after, after_pose_id=pose_id)
    )


# ------------------------------------------------------------------ pose library
def test_save_load_rename_delete_poses(tmp_path: Path) -> None:
    repo = PoseRepository(tmp_path / "poses")
    idle = repo.create("Talking Idle", pose(elbow_flex=30.0), PoseSource.SIMULATION)
    watch = repo.create("Watching", pose(shoulder_pan=-20.0), PoseSource.REAL_ARM, calibration_fingerprint=FP)
    assert (tmp_path / "poses" / f"{idle.id}.json").is_file()
    assert [p.name for p in repo.list()[0]] == ["Talking Idle", "Watching"]
    assert repo.load(watch.id) == watch and watch.calibration_fingerprint == FP
    renamed = repo.rename(idle.id, "Talking idle 2")
    assert renamed.id == idle.id and repo.load(idle.id).name == "Talking idle 2"
    repo.delete(watch.id)
    assert not repo.exists(watch.id) and [p.name for p in repo.list()[0]] == ["Talking idle 2"]
    with pytest.raises(PoseFormatError, match="does not exist"):
        repo.load(watch.id)
    data = json.loads((tmp_path / "poses" / f"{idle.id}.json").read_text())
    assert (
        data["schema_version"] == 1
        and data["source"] == "simulation"
        and set(data["pose"]) == set(JOINT_NAMES)
    )


def test_invalid_poses_are_refused_and_bad_files_left_untouched(tmp_path: Path) -> None:
    repo = PoseRepository(tmp_path / "poses")
    with pytest.raises(PoseFormatError):
        repo.create("nan", pose(elbow_flex=float("nan")), PoseSource.SIMULATION)
    with pytest.raises(PoseFormatError, match="fingerprint"):
        repo.create("real without calibration", ZERO, PoseSource.REAL_ARM)
    with pytest.raises(PoseFormatError):
        repo.create("  ", ZERO, PoseSource.SIMULATION)
    (tmp_path / "poses").mkdir(parents=True, exist_ok=True)
    (tmp_path / "poses" / "broken.json").write_text("{not json")
    poses, problems = repo.list()
    assert poses == [] and problems and (tmp_path / "poses" / "broken.json").exists()


def test_poses_are_checked_against_the_current_calibration() -> None:
    repo_pose = PoseRepository.__new__(PoseRepository)  # no file access needed for the check
    del repo_pose
    from so101_companion.behaviors.poses import NamedPose

    bounds = {n: (-90.0, 90.0) for n in JOINT_NAMES} | {"gripper": (0.0, 100.0)}
    real = NamedPose("a", "Watch", pose(elbow_flex=30.0), PoseSource.REAL_ARM, FP, "", "")
    assert pose_problems_for_arm(real, FP, bounds) == []
    assert "another calibration" in pose_problems_for_arm(real, OTHER, bounds)[0]
    sim_far = NamedPose("b", "Far", pose(elbow_flex=95.0), PoseSource.SIMULATION, None, "", "")
    assert (
        "outside this arm's range" in pose_problems_for_arm(sim_far, FP, bounds)[0]
    )  # never clamped silently
    sim_ok = NamedPose("c", "Ok", pose(elbow_flex=10.0), PoseSource.SIMULATION, None, "", "")
    assert pose_problems_for_arm(sim_ok, FP, bounds) == []


# ------------------------------------------------------------------ "After behavior" in the file format
def test_after_behavior_is_saved_with_the_behavior(tmp_path: Path) -> None:
    repo = BehaviorRepository(tmp_path / "behaviors")
    b = repo.create(wave())
    b2 = repo.update(b, "after_behavior", settings=PlaybackSettings(after_action="pose", after_pose_id="p1"))
    loaded = repo.load(b.id)
    assert loaded.settings.after_action == "pose" and loaded.settings.after_pose_id == "p1"
    raw = json.loads((tmp_path / "behaviors" / f"{b.id}.json").read_text())
    assert raw["settings"]["after"] == {"action": "pose", "pose_id": "p1"} and b2.revision == 2


def test_existing_files_without_an_ending_keep_the_default_rest(tmp_path: Path) -> None:
    doc = wave().to_json()
    del doc["settings"]["after"]  # exactly what files written before ADR-017 contain
    b = behavior_from_json(doc)
    assert b.settings.after_action == "rest" and b.settings.after_pose_id is None


def test_invalid_endings_are_reported() -> None:
    assert any("after-behavior" in p for p in settings_problems(PlaybackSettings(after_action="dance"), 1.0))
    assert any("pose id" in p for p in settings_problems(PlaybackSettings(after_action="pose"), 1.0))
    assert settings_problems(PlaybackSettings(after_action="hold"), 1.0) == []


# ------------------------------------------------------------------ preview of each ending
def test_preview_shows_the_selected_ending() -> None:
    rest_goal = pose(shoulder_lift=-90.0)
    b = wave(after="hold")
    plan = compile_plan(b, b.settings, ZERO, bounds=None, calibration_fingerprint=None)
    hold = PreviewSession(plan, rest_goal, hardware_context=False, after="hold")
    assert hold.duration == pytest.approx(hold.behavior_s) and hold.rest is None
    assert hold.pose_at(hold.duration + 1)[1].startswith("hold last position")
    b = wave(after="pose", pose_id="p")
    plan = compile_plan(b, b.settings, ZERO, bounds=None, calibration_fingerprint=None)
    goal = pose(elbow_flex=50.0, wrist_flex=20.0)
    to_pose = PreviewSession(
        plan, rest_goal, hardware_context=False, after="pose", end_pose_goal=goal, end_pose_name="Idle"
    )
    assert to_pose.end_move is not None and to_pose.duration > to_pose.behavior_s
    end_pose, label = to_pose.pose_at(to_pose.duration)
    assert label == "move to pose 'Idle' (planned)" and end_pose == pytest.approx(goal)
    b = wave(after="hold", reps=None)  # until stopped: it ends by interruption -> normal rest policy
    plan = compile_plan(b, b.settings, ZERO, bounds=None, calibration_fingerprint=None)
    forever = PreviewSession(plan, rest_goal, hardware_context=False, after="hold")
    assert forever.rest is not None

"""T-003 / A03: the unmodified official scene loads with six named hinge joints + position actuators.
No GL context is needed for these tests."""

from __future__ import annotations

import math

import mujoco
import numpy as np
import pytest

from so101_companion.simulation.mujoco_pose import (
    EXPECTED_JOINTS,
    ModelStructureError,
    PoseScene,
    inspect_joints,
)

# Ranges (radians) as declared in the pinned so101_new_calib.xml.
OFFICIAL_RANGES = {
    "shoulder_pan": (-1.9198621771937616, 1.9198621771937634),
    "shoulder_lift": (-1.7453292519943224, 1.7453292519943366),
    "elbow_flex": (-1.69, 1.69),
    "wrist_flex": (-1.6580628494556928, 1.6580627293335335),
    "wrist_roll": (-2.7438472969992493, 2.841206309382605),
    "gripper": (-0.17453297762778586, 1.7453291995659765),
}


def test_six_named_hinge_joints_and_position_actuators(pose_scene: PoseScene) -> None:
    joints = pose_scene.joints
    assert tuple(joints) == EXPECTED_JOINTS
    for name, j in joints.items():
        assert pose_scene.model.jnt_type[j.joint_id] == mujoco.mjtJoint.mjJNT_HINGE
        assert mujoco.mj_id2name(pose_scene.model, mujoco.mjtObj.mjOBJ_ACTUATOR, j.actuator_id) == name
        assert j.range_rad == pytest.approx(OFFICIAL_RANGES[name])
        assert j.range_rad[0] < j.qpos0_rad < j.range_rad[1] or j.qpos0_rad == 0.0


def test_meshes_resolved(pose_scene: PoseScene) -> None:
    m = pose_scene.model
    assert m.nmesh == 13
    assert all(m.mesh_vertnum[i] > 0 for i in range(m.nmesh))
    mesh_geoms = [g for g in range(m.ngeom) if m.geom_type[g] == mujoco.mjtGeom.mjGEOM_MESH]
    assert len(mesh_geoms) >= 13


def test_joint_lookup_is_by_name_not_position(pose_scene: PoseScene) -> None:
    m = pose_scene.model
    for name, j in pose_scene.joints.items():
        assert mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, name) == j.joint_id
        assert m.jnt_qposadr[j.joint_id] == j.qpos_adr


@pytest.mark.parametrize(
    ("joint", "moves", "stays"),
    [
        ("shoulder_pan", "gripper", "base"),
        ("shoulder_lift", "lower_arm", "shoulder"),
        ("elbow_flex", "wrist", "upper_arm"),
        ("wrist_flex", "gripper", "lower_arm"),
        ("gripper", "moving_jaw_so101_v1", "gripper"),
    ],
)
def test_changing_named_joint_moves_expected_part(
    pose_scene: PoseScene, joint: str, moves: str, stays: str
) -> None:
    """Kinematic preview: set qpos + mj_forward; distal bodies move, proximal bodies do not."""

    def pose(body: str) -> np.ndarray:
        bid = mujoco.mj_name2id(pose_scene.model, mujoco.mjtObj.mjOBJ_BODY, body)
        return np.concatenate([pose_scene.data.xpos[bid], pose_scene.data.xquat[bid]])

    before_moves, before_stays = pose(moves), pose(stays)
    lo, hi = pose_scene.joints[joint].range_rad
    pose_scene.set_model_joint_rad({joint: lo + 0.8 * (hi - lo)})
    assert not np.allclose(pose(moves), before_moves, atol=1e-4)
    np.testing.assert_allclose(pose(stays), before_stays, atol=1e-9)


def test_wrist_roll_rotates_gripper_frame(pose_scene: PoseScene) -> None:
    bid = mujoco.mj_name2id(pose_scene.model, mujoco.mjtObj.mjOBJ_BODY, "gripper")
    before = pose_scene.data.xquat[bid].copy()
    pose_scene.set_model_joint_rad({"wrist_roll": math.radians(60)})
    assert not np.allclose(pose_scene.data.xquat[bid], before)


def test_pose_preview_does_not_step_physics(pose_scene: PoseScene) -> None:
    pose_scene.set_model_joint_rad({"elbow_flex": 0.5})
    assert pose_scene.data.time == 0.0
    assert pose_scene.model_joint_rad()["elbow_flex"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    "values",
    [{"nope": 0.0}, {"elbow_flex": float("nan")}, {"elbow_flex": float("inf")}, {"elbow_flex": 10.0}],
)
def test_invalid_pose_values_rejected_without_partial_update(
    pose_scene: PoseScene, values: dict[str, float]
) -> None:
    before = pose_scene.model_joint_rad()
    with pytest.raises((KeyError, ValueError)):
        pose_scene.set_model_joint_rad({"shoulder_pan": 0.3, **values})
    assert pose_scene.model_joint_rad() == before


def test_structure_check_rejects_model_missing_a_joint() -> None:
    xml = """<mujoco><worldbody><body><joint name="shoulder_pan" type="hinge" range="-1 1"/>
             <geom size="0.1"/></body></worldbody></mujoco>"""
    with pytest.raises(ModelStructureError):
        inspect_joints(mujoco.MjModel.from_xml_string(xml))

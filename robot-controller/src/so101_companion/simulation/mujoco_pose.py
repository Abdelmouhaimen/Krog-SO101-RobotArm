"""Official SO-101 model loading, named joint/actuator discovery and kinematic pose preview.

Loads the unmodified official scene, proves the six expected hinge joints and position actuators
exist (looked up by NAME, never by array position), and sets joint angles for a kinematic
``qpos`` + ``mj_forward`` pose preview. This is a POSE PREVIEW, not a motor/torque simulation:
``mj_step`` is never called. Logical units are converted by ``simulation.mapping``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import mujoco
import numpy as np

from so101_companion.simulation.mapping import JointMapping

EXPECTED_JOINTS: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)


class ModelStructureError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelJoint:
    name: str
    joint_id: int
    qpos_adr: int
    range_rad: tuple[float, float]
    actuator_id: int
    ctrlrange_rad: tuple[float, float]
    qpos0_rad: float


def load_scene(scene_path: Path) -> mujoco.MjModel:
    if not scene_path.is_file():
        raise FileNotFoundError(f"MuJoCo scene not found: {scene_path}")
    return mujoco.MjModel.from_xml_path(str(scene_path))


def inspect_joints(model: mujoco.MjModel) -> dict[str, ModelJoint]:
    """Validate the model exposes exactly the six expected hinge joints and matching position actuators."""
    hinge_names = {
        mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, j)
        for j in range(model.njnt)
        if model.jnt_type[j] == mujoco.mjtJoint.mjJNT_HINGE
    }
    if hinge_names != set(EXPECTED_JOINTS) or model.njnt != len(EXPECTED_JOINTS):
        raise ModelStructureError(
            f"Expected exactly hinge joints {sorted(EXPECTED_JOINTS)}, model has "
            f"{model.njnt} joints, hinges {sorted(n for n in hinge_names if n)}"
        )
    if model.nu != len(EXPECTED_JOINTS):
        raise ModelStructureError(f"Expected {len(EXPECTED_JOINTS)} actuators, model has {model.nu}")

    joints: dict[str, ModelJoint] = {}
    for name in EXPECTED_JOINTS:
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        aid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, name)
        if jid < 0 or aid < 0:
            raise ModelStructureError(f"Missing joint or actuator named {name!r}")
        if not model.jnt_limited[jid]:
            raise ModelStructureError(f"Joint {name!r} has no range limits")
        if model.actuator_trntype[aid] != mujoco.mjtTrn.mjTRN_JOINT or model.actuator_trnid[aid, 0] != jid:
            raise ModelStructureError(f"Actuator {name!r} does not drive joint {name!r}")
        is_position = (
            model.actuator_gaintype[aid] == mujoco.mjtGain.mjGAIN_FIXED
            and model.actuator_biastype[aid] == mujoco.mjtBias.mjBIAS_AFFINE
            and np.isclose(model.actuator_biasprm[aid, 1], -model.actuator_gainprm[aid, 0])
        )
        if not is_position:
            raise ModelStructureError(f"Actuator {name!r} is not a position actuator")
        lo, hi = (float(v) for v in model.jnt_range[jid])
        clo, chi = (float(v) for v in model.actuator_ctrlrange[aid])
        adr = int(model.jnt_qposadr[jid])
        joints[name] = ModelJoint(
            name=name,
            joint_id=int(jid),
            qpos_adr=adr,
            range_rad=(lo, hi),
            actuator_id=int(aid),
            ctrlrange_rad=(clo, chi),
            qpos0_rad=float(model.qpos0[adr]),
        )
    return joints


class PoseScene:
    """Kinematic pose preview of the official model. Owns one MjModel/MjData pair."""

    def __init__(self, model: mujoco.MjModel) -> None:
        self.model = model
        self.data = mujoco.MjData(model)
        self.joints = inspect_joints(model)
        mujoco.mj_forward(model, self.data)

    @classmethod
    def from_scene_path(cls, scene_path: Path) -> PoseScene:
        return cls(load_scene(scene_path))

    def set_model_joint_rad(
        self, values: Mapping[str, float], beyond_range_ok: frozenset[str] = frozenset()
    ) -> None:
        """Set raw model joint angles (radians). Rejects unknown names, non-finite and out-of-range
        values. Joints in ``beyond_range_ok`` (full-turn joints) may be displayed outside the MJCF
        jnt_range: limits only act in dynamics, and this is a kinematic qpos + mj_forward preview."""
        for name, value in values.items():
            joint = self.joints.get(name)
            if joint is None:
                raise KeyError(f"Unknown joint {name!r}")
            if not np.isfinite(value):
                raise ValueError(f"Non-finite value for {name!r}: {value!r}")
            lo, hi = joint.range_rad
            if name not in beyond_range_ok and not lo <= value <= hi:
                raise ValueError(f"{name!r}={value:.4f} rad outside model range [{lo:.4f}, {hi:.4f}]")
        for name, value in values.items():
            self.data.qpos[self.joints[name].qpos_adr] = value
        mujoco.mj_forward(self.model, self.data)

    def model_ranges_rad(self) -> dict[str, tuple[float, float]]:
        return {name: j.range_rad for name, j in self.joints.items()}

    def set_logical_pose(self, values: Mapping[str, float], mapping: JointMapping) -> list[str]:
        """Display a LOGICAL pose (arm degrees, gripper 0-100). Returns display-clamp reasons."""
        model, reasons = mapping.logical_to_model(values)
        self.set_model_joint_rad(model, beyond_range_ok=frozenset(mapping.full_turn))
        return reasons

    def logical_pose(self, mapping: JointMapping) -> dict[str, float]:
        return {n: mapping.model_to_logical(n, q) for n, q in self.model_joint_rad().items()}

    def model_joint_rad(self) -> dict[str, float]:
        return {name: float(self.data.qpos[j.qpos_adr]) for name, j in self.joints.items()}

    def body_position(self, body_name: str) -> np.ndarray:
        bid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, body_name)
        if bid < 0:
            raise KeyError(f"Unknown body {body_name!r}")
        return np.array(self.data.xpos[bid], copy=True)

"""Forward-kinematics direction analysis on the official SO-101 model (Milestone 2.6, ADR-019).

* Reference point: the ``gripperframe`` site, measured horizontally from the SHOULDER-PAN AXIS anchor
  (38.8 mm in front of the base origin) — never from the base origin.
* shoulder_pan is held at its reference (logical 0) for every evaluation, so a behavior recorded at any
  pan gets the same intrinsic direction.
* Kinematic mapping: the configured joint SIGNS and gripper direction (visually verified in B02) but NO
  display offsets (those are a 3D-view alignment aid, not physical transforms).
* Results are cached by (item id, revision, effective trim, channel, calibration, model, mapping,
  algorithm version). Nothing here runs per control tick, and behavior files are never written.

Each instance owns its own MuJoCo model/data and a lock, so the controller thread and the UI/API threads
use separate instances and never contend.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import mujoco

from so101_companion.behaviors.direction import ALGORITHM_VERSION, Direction, classify, time_weights
from so101_companion.behaviors.model import Behavior, PlaybackSettings
from so101_companion.behaviors.plan import trimmed_series
from so101_companion.config import SimulationSection
from so101_companion.domain.joints import ARM_JOINTS
from so101_companion.simulation import model_assets
from so101_companion.simulation.mapping import mapping_from_config
from so101_companion.simulation.mujoco_pose import PoseScene


class DirectionAnalyzer:
    def __init__(self, scene_path: Path, simulation: SimulationSection) -> None:
        self.scene = PoseScene.from_scene_path(scene_path)
        m, d = self.scene.model, self.scene.data
        display = mapping_from_config(self.scene.model_ranges_rad(), simulation)
        self.mapping = display.with_changes(offset_deg=dict.fromkeys(ARM_JOINTS, 0.0))
        self._site = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, "gripperframe")
        pan = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "shoulder_pan")
        if self._site < 0 or pan < 0:
            raise ValueError("model lacks the gripperframe site or the shoulder_pan joint")
        mujoco.mj_forward(m, d)
        axis = d.xaxis[pan]
        if abs(abs(float(axis[2])) - 1.0) > 1e-6:
            raise ValueError(f"shoulder_pan axis is not vertical in this model: {axis}")
        self.pan_axis_xy = (float(d.xanchor[pan][0]), float(d.xanchor[pan][1]))
        # Clockwise-from-above bearing changes by s·Δq_logical. A model axis along −z makes a positive model
        # angle clockwise (right-hand rule); the configured logical->model sign multiplies that.
        model_cw = 1.0 if axis[2] < 0 else -1.0
        self.pan_sign = model_cw * float(display.sign.get("shoulder_pan", 1.0))
        self.model_id = _model_identity(scene_path)
        self.mapping_id = (
            "signs:"
            + ",".join(f"{k}={display.sign[k]:+.0f}" for k in ARM_JOINTS)
            + (f";gripper0={display.gripper_0_maps_to}")
        )
        self._lock = threading.Lock()
        self._cache: dict[tuple[Any, ...], Direction] = {}
        self.evaluations = 0

    # ------------------------------------------------------------------ FK
    def bearing(self, pose: Mapping[str, float]) -> tuple[float, float]:
        """(clockwise bearing °, horizontal distance m) of the gripper center about the pan axis, with
        shoulder_pan held at its reference."""
        p = dict(pose)
        p["shoulder_pan"] = 0.0
        with self._lock:
            self.scene.set_logical_pose(p, self.mapping)
            x, y = (float(v) for v in self.scene.data.site_xpos[self._site][:2])
            self.evaluations += 1
        rx, ry = x - self.pan_axis_xy[0], y - self.pan_axis_xy[1]
        return -math.degrees(math.atan2(ry, rx)), math.hypot(rx, ry)

    def bearing_at_pan(self, pose: Mapping[str, float]) -> float:
        """Full FK bearing (clockwise, about the pan axis) INCLUDING the pose's own shoulder_pan — used by
        tests and evidence to check the yaw equation, never by the controller."""
        with self._lock:
            self.scene.set_logical_pose(dict(pose), self.mapping)
            x, y = (float(v) for v in self.scene.data.site_xpos[self._site][:2])
        return -math.degrees(math.atan2(y - self.pan_axis_xy[1], x - self.pan_axis_xy[0]))

    # ------------------------------------------------------------------ analysis
    def analyze_pose(self, pose: Mapping[str, float], source: str = "pose", key: Any = None) -> Direction:
        ck = ("pose", key, self.model_id, self.mapping_id, ALGORITHM_VERSION) if key is not None else None
        if ck is not None and ck in self._cache:
            return self._cache[ck]
        b, rho = self.bearing(pose)
        result = classify([b], [rho], [1.0], source=source)
        if ck is not None:
            self._cache[ck] = result
        return result

    def analyze_behavior(self, b: Behavior, settings: PlaybackSettings | None = None) -> Direction:
        s = settings or b.settings
        ck = (
            "behavior",
            b.id,
            b.revision,
            round(s.trim_start_s, 6),
            None if s.trim_end_s is None else round(s.trim_end_s, 6),
            b.origin.canonical_channel,
            b.calibration_fingerprint,
            self.model_id,
            self.mapping_id,
            ALGORITHM_VERSION,
        )
        hit = self._cache.get(ck)
        if hit is not None:
            return hit
        times, poses = trimmed_series(b, s)
        bearings, rhos = [], []
        for p in poses:
            br, rho = self.bearing(p)
            bearings.append(br)
            rhos.append(rho)
        result = classify(bearings, rhos, time_weights(times), source=f"behavior '{b.name}' r{b.revision}")
        self._cache[ck] = result
        return result

    def cache_size(self) -> int:
        return len(self._cache)


def _model_identity(scene_path: Path) -> str:
    try:
        manifest = model_assets.verify_assets(model_assets.vendor_dir_for_scene(scene_path))
        repo = manifest.repo_url.rsplit("/", 1)[-1].removesuffix(".git")
        return f"{repo}@{manifest.commit[:12]}:{scene_path.name}"
    except Exception:
        return f"unverified:{scene_path.name}"

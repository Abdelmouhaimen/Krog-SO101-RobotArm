"""D8 responsive transitions checked in MuJoCo PHYSICS (no GL): the official model's force-limited
position actuators follow the exact 20 Hz targets the controller sends (zero-order hold per control
period), under gravity. Synthetic poses only; this approximates, and does not replace, the supervised
real-arm test (Gate B step B11).

Checks, for a large entry, a bridged loop and a seamless loop:
* the transitions are several times shorter than under the old 30 °/s rule;
* the simulated arm keeps up (tracking error stays small) and touches nothing new (no new contacts).
"""

from __future__ import annotations

import math

import mujoco
import pytest

from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.plan import MIN_JERK_PEAK, PlaybackPlan, compile_plan
from so101_companion.behaviors.repository import new_behavior
from so101_companion.config import load_config
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.simulation.mapping import mapping_from_config
from so101_companion.simulation.mujoco_pose import PoseScene

PERIOD = 0.05
FP = "sha256:" + "ab" * 32
REST_LIKE = {"shoulder_pan": 0.0, "shoulder_lift": -95.0, "elbow_flex": 88.0, "wrist_flex": -90.0,
             "wrist_roll": 0.0, "gripper": 2.0}  # fmt: skip
BOUNDS = {"shoulder_pan": (-104.9, 104.9), "shoulder_lift": (-99.9, 99.9), "elbow_flex": (-92.3, 92.3),
          "wrist_flex": (-97.1, 97.1), "wrist_roll": (-163.0, 163.0), "gripper": (0.0, 100.0)}  # fmt: skip


def behavior(name: str, poses: list[dict[str, float]]):  # type: ignore[no-untyped-def]
    samples = tuple(Sample(round(i * PERIOD, 6), actual=p) for i, p in enumerate(poses))
    return new_behavior(name, Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=FP)


def old_rule(a: dict[str, float], b: dict[str, float]) -> float:
    return max(0.5, MIN_JERK_PEAK * max(abs(b[n] - a[n]) for n in JOINT_NAMES) / 30.0)


def touches_floor(scene: PoseScene, poses: list[dict[str, float]]) -> bool:
    """Whether any of the test gesture's own poses touches the floor (synthetic data must not)."""
    mp = mapping_from_config(scene.model_ranges_rad(), load_config(None).simulation)
    floor = 0  # MuJoCo's world body
    for p in poses:
        scene.set_logical_pose(p, mp)
        d = scene.data
        for i in range(d.ncon):
            g1, g2 = d.contact[i].geom1, d.contact[i].geom2
            bodies = (scene.model.geom_bodyid[g1], scene.model.geom_bodyid[g2])
            if d.contact[i].dist < -1e-4 and floor in bodies:
                return True
    return False


def simulate(scene: PoseScene, plan: PlaybackPlan) -> tuple[dict[str, float], set[tuple[str, str]]]:
    """Max tracking error (deg) per transition segment kind, and contacts that were not there at start."""
    m, d = scene.model, scene.data
    mp = mapping_from_config(scene.model_ranges_rad(), load_config(None).simulation)
    act = {n: scene.joints[n].actuator_id for n in JOINT_NAMES}

    def body(g: int) -> str:
        return mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, m.geom_bodyid[g]) or str(g)

    def contacts() -> set[tuple[str, str]]:
        out = set()
        for i in range(d.ncon):
            if d.contact[i].dist < -1e-4:
                a, b = sorted((body(d.contact[i].geom1), body(d.contact[i].geom2)))
                out.add((a, b))
        return out

    mujoco.mj_resetData(m, d)
    start, _ = mp.logical_to_model(plan.start_pose)
    for n, q in start.items():
        d.qpos[scene.joints[n].qpos_adr] = q
        d.ctrl[act[n]] = q
    for _ in range(500):  # settle 1 s
        mujoco.mj_step(m, d)
    base = contacts()
    err: dict[str, float] = {}
    new: set[tuple[str, str]] = set()
    t, per_tick = 0.0, round(PERIOD / m.opt.timestep)
    while True:
        point = plan.at(t)
        ctrl, _ = mp.logical_to_model(point.pose)
        for n, q in ctrl.items():
            d.ctrl[act[n]] = q
        for _ in range(per_tick):
            mujoco.mj_step(m, d)
        if point.segment in ("entry", "bridge"):
            e = max(abs(math.degrees(d.qpos[scene.joints[n].qpos_adr] - ctrl[n])) for n in JOINT_NAMES)
            err[point.segment] = max(err.get(point.segment, 0.0), e)
        new |= contacts() - base
        if point.finished:
            return err, new
        t += PERIOD


def test_large_entry_is_short_tracked_and_collision_free(pose_scene: PoseScene) -> None:
    far = {"shoulder_pan": -90.0, "shoulder_lift": 40.0, "elbow_flex": -60.0, "wrist_flex": 80.0,
           "wrist_roll": -140.0, "gripper": 80.0}  # fmt: skip
    plan = compile_plan(behavior("far", [far] * 6), PlaybackSettings(), REST_LIKE, bounds=BOUNDS,
                        calibration_fingerprint=FP)  # fmt: skip
    assert plan.entry.duration < old_rule(REST_LIKE, far) / 3.0  # ~3 s instead of ~10 s
    err, new = simulate(pose_scene, plan)
    assert err["entry"] < 10.0, err
    assert not new, new


@pytest.mark.parametrize("speed", [1.0, 2.0])
def test_bridged_and_seamless_loops_are_short_tracked_and_collision_free(
    pose_scene: PoseScene, speed: float
) -> None:
    n = 21
    nod = [REST_LIKE | {"shoulder_lift": -90.0 + 25.0 * i / (n - 1), "wrist_flex": -40.0 + 30.0 * i / (n - 1)}
           for i in range(n)]  # fmt: skip  # ends 25°/30° away from its start -> bridged loop
    wave = [REST_LIKE | {"shoulder_lift": -80.0 + 15.0 * math.sin(math.pi * i / (n - 1)),
                         "wrist_flex": -30.0 + 25.0 * math.sin(2 * math.pi * i / (n - 1))}
            for i in range(n)]  # fmt: skip  # ends where it starts -> seamless loop
    bridged = compile_plan(behavior("nod", nod), PlaybackSettings(speed=speed, repeat_count=4), REST_LIKE,
                           bounds=BOUNDS, calibration_fingerprint=FP)  # fmt: skip
    seamless = compile_plan(behavior("wave", wave), PlaybackSettings(speed=speed, repeat_count=4), REST_LIKE,
                            bounds=BOUNDS, calibration_fingerprint=FP)  # fmt: skip
    assert not touches_floor(pose_scene, nod + wave)
    assert 0.0 < bridged.bridge.duration < old_rule(nod[-1], nod[0]) / 2.5
    assert seamless.bridge.duration == 0.0
    for plan in (bridged, seamless):
        err, new = simulate(pose_scene, plan)
        assert max(err.values()) < 10.0, err
        assert not new, new

"""ADR-019 geometry and direction metadata on the REAL official SO-101 model (MuJoCo FK, no rendering).

* The shoulder-pan axis is vertical (pointing down), 38.8 mm in front of the base origin; positive pan
  turns clockwise seen from above (s = +1 in the clockwise convention).
* gripper bearing = s·q + psi holds exactly about the pan axis, and is WRONG about the base origin.
* psi ≈ 0° (NORMAL) / 180° (REVERSED); the solver's pan makes FK face the target; unreachable targets
  report the true residual.
* Behavior metadata: normal / reversed recordings, wraparound, crossing (MIXED), near-axis (UNDEFINED),
  irregular sampling, trim, cache invalidation, and behavior files left untouched.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import mujoco
import pytest

from overlay_helpers import NORMAL_POSE, REVERSED_POSE, OverlayEnv, analyzer, fresh_analyzer
from so101_companion.behaviors.direction import MIXED, NORMAL, REVERSED, UNDEFINED, wrap180
from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.repository import new_behavior
from so101_companion.control.overlays import solve_pan

TUCKED = {
    "shoulder_pan": 0.0,
    "shoulder_lift": -95.0,
    "elbow_flex": 90.0,
    "wrist_flex": -85.0,
    "wrist_roll": 0.0,
    "gripper": 2.0,
}
LO, HI = -104.9, 104.9


def test_pan_axis_geometry_and_sign() -> None:
    a = analyzer()
    assert a.pan_axis_xy[0] == pytest.approx(0.0388, abs=1e-4) and abs(a.pan_axis_xy[1]) < 1e-6
    assert a.pan_sign == 1.0  # clockwise from above = +pan = the robot's right


@pytest.mark.parametrize("pose", [NORMAL_POSE, REVERSED_POSE, TUCKED, NORMAL_POSE | {"wrist_roll": 90.0}])
def test_yaw_equation_is_exact_about_the_pan_axis(pose: dict[str, float]) -> None:
    a = analyzer()
    psi, _rho = a.bearing(pose)
    for q in (-104.9, -90.0, -45.0, 0.0, 23.0, 45.0, 90.0, 104.9):
        fk = a.bearing_at_pan(pose | {"shoulder_pan": q})
        assert abs(wrap180(fk - (a.pan_sign * q + psi))) < 1e-9


def test_base_origin_is_the_wrong_reference() -> None:
    a = fresh_analyzer()
    m, d = a.scene.model, a.scene.data
    site = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, "gripperframe")

    def about_origin(pose: dict[str, float]) -> float:
        a.scene.set_logical_pose(pose, a.mapping)
        x, y = d.site_xpos[site][:2]
        return -math.degrees(math.atan2(y, x))

    worst = 0.0
    for q in (-90.0, 23.0, 90.0):
        p = TUCKED | {"shoulder_pan": q}
        worst = max(worst, abs(wrap180(about_origin(p) - (q + about_origin(TUCKED)))))
    assert worst > 20.0  # measured from the base origin the "yaw" is off by tens of degrees near the axis


def test_normal_and_reversed_directions_and_the_lateral_bound() -> None:
    a = analyzer()
    assert a.analyze_pose(NORMAL_POSE).classification == NORMAL
    rev = a.analyze_pose(REVERSED_POSE)
    assert rev.classification == REVERSED and abs(wrap180(rev.psi_deg - 180.0)) < 1.0
    assert a.analyze_pose(TUCKED).classification == UNDEFINED  # 58 mm from the axis
    rng = random.Random(0)
    for _ in range(300):  # psi is within atan(8.1 mm / rho) of 0° or 180° anywhere in the joint space
        p = {
            "shoulder_pan": 0.0,
            "shoulder_lift": rng.uniform(-99, 99),
            "elbow_flex": rng.uniform(-92, 92),
            "wrist_flex": rng.uniform(-97, 97),
            "wrist_roll": rng.uniform(-160, 160),
            "gripper": rng.uniform(0, 100),
        }
        b, rho = a.bearing(p)
        if rho > 0.02:
            dev = min(abs(wrap180(b)), abs(wrap180(b - 180.0)))
            assert dev <= math.degrees(math.atan(0.0083 / rho)) + 1e-6


@pytest.mark.parametrize(
    ("pose", "thetas"),
    [(NORMAL_POSE, (0.0, 23.0, 90.0, -60.0)), (REVERSED_POSE, (180.0, 120.0, -120.0, 100.0))],
)
def test_solver_pan_makes_fk_face_the_target(pose: dict[str, float], thetas: tuple[float, ...]) -> None:
    a = analyzer()
    psi = a.analyze_pose(pose).psi_deg
    assert psi is not None
    for front in (0.0, 30.0):
        for theta in thetas:
            sol = solve_pan(theta, psi, front, LO, HI, s=a.pan_sign)
            if sol.reach != "REACHABLE":
                continue
            fk = a.bearing_at_pan(pose | {"shoulder_pan": sol.pan_target})
            assert abs(wrap180(fk - (front + theta))) < 1e-6


def test_unreachable_target_reports_the_true_residual() -> None:
    a = analyzer()
    psi = a.analyze_pose(REVERSED_POSE).psi_deg
    assert psi is not None
    sol = solve_pan(0.0, psi, 0.0, LO, HI, s=a.pan_sign)
    assert sol.reach == "UNREACHABLE" and abs(sol.residual_deg) == pytest.approx(75.0, abs=1.0)
    fk = a.bearing_at_pan(REVERSED_POSE | {"shoulder_pan": sol.pan_target})
    assert abs(wrap180(0.0 - fk - sol.residual_deg)) < 1e-6  # residual = requested − FK-achieved


# ------------------------------------------------------------------ behavior metadata (cached)
def test_behavior_metadata_classes_trim_cache_and_files(calibration_file: Path, tmp_path: Path) -> None:
    env = OverlayEnv(calibration_file, tmp_path)
    a = env.service.directions
    rev = env.behavior("talk", REVERSED_POSE)
    norm = env.behavior("nod", NORMAL_POSE, "wrist_flex")
    mixed = env.behavior("cross", REVERSED_POSE, sweep={"shoulder_lift": (-60.0, 20.0)})
    tucked = env.behavior("tucked", TUCKED, "wrist_roll")
    path = env.service.repository.path_for(rev.id)
    before = path.read_bytes()
    d_rev = env.service.behavior_direction(rev)
    assert d_rev is not None and d_rev.classification == REVERSED and d_rev.compatible
    assert d_rev.usable_fraction == 1.0 and d_rev.spread_max_deg < 1.0  # recorded at pan −75: ignored
    assert env.service.behavior_direction(norm).classification == NORMAL  # type: ignore[union-attr]
    d_mix = env.service.behavior_direction(mixed)
    assert d_mix is not None and d_mix.classification == MIXED and not d_mix.compatible
    assert env.service.behavior_direction(tucked).classification == UNDEFINED  # type: ignore[union-attr]
    assert path.read_bytes() == before  # metadata is derived, never written into the behavior file
    # cache: same id/revision/trim -> no new FK; a trim or a new revision recomputes
    n = a.evaluations
    env.service.behavior_direction(rev)
    assert a.evaluations == n
    trimmed = PlaybackSettings(trim_start_s=0.0, trim_end_s=0.30)  # only the reversed part
    d_trim = env.service.behavior_direction(mixed, trimmed)
    assert d_trim is not None and d_trim.classification == REVERSED and a.evaluations > n
    renamed = env.service.repository.set_settings(rev, PlaybackSettings(speed=2.0))
    m = a.evaluations
    env.service.behavior_direction(renamed)
    assert a.evaluations > m  # new revision -> recomputed


def test_irregular_sampling_and_wraparound_in_a_recording() -> None:
    a = fresh_analyzer()
    # a reversed recording whose bearing straddles ±180° with irregular timing, plus a brief excursion
    times = [0.0, 0.01, 0.02, 0.5, 1.0, 1.5, 2.0, 2.02, 2.04]
    poses = [
        REVERSED_POSE | {"wrist_roll": r} for r in (40.0, -40.0, 40.0, -40.0, 40.0, -40.0, 40.0, 0.0, 0.0)
    ]
    samples = tuple(Sample(t, requested=p) for t, p in zip(times, poses, strict=True))
    b = new_behavior("wrap", Origin.SIM_KEYFRAMES, samples)  # keyframes: sparse timing is legal
    d = a.analyze_behavior(b)
    bearings = [a.bearing(p)[0] for p in poses]
    assert min(bearings) < -178.0 and max(bearings) > 178.0  # really wraps across ±180°
    assert d.classification == REVERSED and abs(wrap180(d.psi_deg - 180.0)) < 2.0

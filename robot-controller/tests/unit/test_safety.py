"""T-011 / offline test 4: calibrated ranges with 5° margin, gripper 0..100; fixed M1 policy (ADR-010)."""

from __future__ import annotations

import pytest

from so101_companion.domain.joints import ARM_JOINTS, JOINT_NAMES
from so101_companion.safety import policy
from so101_companion.safety.bounds import (
    InvertedBoundsError,
    JointBounds,
    JointLimit,
    apply_margin,
    build_bounds,
)


def bounds90() -> JointBounds:
    return build_bounds({n: (-90.0, 90.0) for n in ARM_JOINTS}, 5.0, "test", "T")


def zeros(**kw: float) -> dict[str, float]:
    return {**dict.fromkeys(JOINT_NAMES, 0.0), **kw}


def test_spec_example_88_clamped_to_85_and_gripper_0_100() -> None:
    out, reasons = bounds90().clamp(zeros(elbow_flex=88.0, wrist_flex=-120.0, gripper=105.0))
    assert out["elbow_flex"] == 85.0
    assert out["wrist_flex"] == -85.0
    assert out["gripper"] == 100.0
    assert set(reasons) == {"elbow_flex:range_max", "wrist_flex:range_min", "gripper:range_max"}
    out, reasons = bounds90().clamp(zeros(gripper=-1.0))
    assert out["gripper"] == 0.0 and reasons == ["gripper:range_min"]


def test_gripper_has_no_degree_margin() -> None:
    b = bounds90()
    assert (b.limits["gripper"].lower, b.limits["gripper"].upper) == (0.0, 100.0)
    out, reasons = b.clamp(zeros(gripper=100.0))
    assert out["gripper"] == 100.0 and reasons == []
    with pytest.raises(ValueError, match="Gripper"):
        JointBounds({**b.limits, "gripper": JointLimit(5.0, 95.0, "bad")}, "x")


def test_inverted_bounds_rejected() -> None:
    with pytest.raises(InvertedBoundsError):
        apply_margin(-4.0, 4.0, 5.0, "too narrow")
    assert apply_margin(-5.0, 5.0, 5.0, "exact").lower == 0.0
    with pytest.raises(InvertedBoundsError):
        JointLimit(1.0, -1.0, "inverted")


def test_inside_values_untouched() -> None:
    values = zeros(shoulder_pan=84.9, gripper=0.0)
    out, reasons = bounds90().clamp(values)
    assert out == values and reasons == []


def test_no_outward_ratchet_when_already_outside_margin() -> None:
    """ADR-007: a joint resting inside the 5° margin (outside the allowed range) is not forced to move,
    may move inward, and is never commanded further outward."""
    b = bounds90()
    staged = zeros(shoulder_lift=-88.0)
    out, _ = b.clamp(zeros(shoulder_lift=-88.0), hold_outside=staged)
    assert out["shoulder_lift"] == -88.0  # stays where it is
    out, reasons = b.clamp(zeros(shoulder_lift=-95.0), hold_outside=staged)
    assert out["shoulder_lift"] == -88.0 and reasons == ["shoulder_lift:range_min"]  # no further outward
    out, _ = b.clamp(zeros(shoulder_lift=-60.0), hold_outside=staged)
    assert out["shoulder_lift"] == -60.0  # inward allowed
    # once inside, the normal range applies again
    out, _ = b.clamp(zeros(shoulder_lift=-95.0), hold_outside=zeros(shoulder_lift=-60.0))
    assert out["shoulder_lift"] == -85.0


def test_fixed_policy_constants() -> None:
    """The M1 control/safety policy is fixed and centralized (no configuration surface)."""
    assert policy.ANGULAR_MARGIN_DEG == 5.0
    assert policy.MAX_RELATIVE_TARGET is None
    assert policy.DISABLE_TORQUE_ON_DISCONNECT is True
    import so101_companion.safety as pkg

    assert not hasattr(pkg, "step_limit")
    from pathlib import Path

    assert not (Path(pkg.__file__).parent / "step_limit.py").exists()


def test_large_targets_inside_bounds_are_untouched() -> None:
    """No step limiting: a 170° jump inside the allowed range passes the bounds stage unchanged."""
    out, reasons = bounds90().clamp(zeros(shoulder_pan=-85.0, elbow_flex=85.0, gripper=100.0))
    assert out == zeros(shoulder_pan=-85.0, elbow_flex=85.0, gripper=100.0) and reasons == []


def test_owner_wrist_roll_range_is_applied_to_hardware_and_sim_bounds(calibration_file) -> None:  # type: ignore[no-untyped-def]
    """Owner decision 2026-11-07: wrist_roll usable range -163..+163 (re-centred full-turn calibration)."""
    from so101_companion.config import SimulationSection
    from so101_companion.devices.calibration import calibrated_bounds, load_calibration
    from so101_companion.simulation.mapping import mapping_from_config, sim_provisional_bounds

    assert policy.JOINT_RANGE_LIMITS == {"wrist_roll": (-163.0, 163.0)}
    hw = calibrated_bounds(load_calibration(calibration_file), policy.ANGULAR_MARGIN_DEG)
    assert (hw.limits["wrist_roll"].lower, hw.limits["wrist_roll"].upper) == (-163.0, 163.0)
    official = {
        "shoulder_pan": (-1.92, 1.92),
        "shoulder_lift": (-1.75, 1.75),
        "elbow_flex": (-1.69, 1.69),
        "wrist_flex": (-1.66, 1.66),
        "wrist_roll": (-2.74, 2.84),
        "gripper": (-0.17, 1.75),
    }
    sim = sim_provisional_bounds(
        mapping_from_config(official, SimulationSection()), policy.ANGULAR_MARGIN_DEG
    )
    assert (sim.limits["wrist_roll"].lower, sim.limits["wrist_roll"].upper) == (-163.0, 163.0)
    out, reasons = hw.clamp(zeros(wrist_roll=-170.0))
    assert out["wrist_roll"] == -163.0 and reasons == ["wrist_roll:range_min"]


def test_restrict_never_widens() -> None:
    from so101_companion.safety.bounds import restrict

    b = restrict(bounds90(), {"wrist_roll": (-300.0, 300.0), "elbow_flex": (-10.0, 400.0)}, "t")
    assert (b.limits["wrist_roll"].lower, b.limits["wrist_roll"].upper) == (-85.0, 85.0)
    assert (b.limits["elbow_flex"].lower, b.limits["elbow_flex"].upper) == (-10.0, 85.0)

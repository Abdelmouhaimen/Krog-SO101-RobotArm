"""T-025 / FR-09 / SR-08: existing-calibration validation and degree-bound derivation."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from so101_companion.devices.calibration import (
    CalibrationError,
    MotorCal,
    calibrated_bounds,
    calibrated_degree_range,
    load_calibration,
)
from so101_companion.devices.fake import DEMO_CALIBRATION
from so101_companion.domain.joints import ARM_JOINTS


def write(tmp_path: Path, data: object, name: str = "arm.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_valid_file_loads_with_identity_from_stem(calibration_file: Path) -> None:
    cal = load_calibration(calibration_file)
    assert cal.robot_id == "test_follower"
    assert cal.calibration_dir == calibration_file.parent.resolve()
    assert cal.motors["gripper"].id == 6


def test_degree_range_matches_lerobot_normalization_and_vr_formula() -> None:
    """LeRobot 0.4.4 DEGREES: (raw - mid) * 360 / 4095. VR-Control margin: (rmin - mid) * 360/4095 + 5."""
    cal = MotorCal(id=2, drive_mode=0, homing_offset=0, range_min=830, range_max=3230)
    lo, hi = calibrated_degree_range(cal)
    mid = (830 + 3230) / 2
    assert lo == pytest.approx((830 - mid) * 360 / 4095)
    assert hi == pytest.approx((3230 - mid) * 360 / 4095)
    vr_min = (830 - mid) * 360.0 / 4095 + 5.0  # SO-101-VR-Control _tighten_limits_from_calibration
    vr_max = (3230 - mid) * 360.0 / 4095 - 5.0
    assert (lo + 5.0, hi - 5.0) == pytest.approx((vr_min, vr_max))


def test_calibrated_bounds_margin_on_arm_only(calibration_file: Path) -> None:
    b = calibrated_bounds(load_calibration(calibration_file), 5.0)
    for n in ARM_JOINTS:
        if n == "wrist_roll":
            continue  # full turn; owner-set usable range applies (checked below)
        lo, hi = calibrated_degree_range(load_calibration(calibration_file).motors[n])
        assert b.limits[n].lower == pytest.approx(lo + 5.0)
        assert b.limits[n].upper == pytest.approx(hi - 5.0)
    assert (b.limits["gripper"].lower, b.limits["gripper"].upper) == (0.0, 100.0)
    # full-turn wrist_roll (0..4095 -> ±180°, minus margin) intersected with the owner range ±163
    assert (b.limits["wrist_roll"].lower, b.limits["wrist_roll"].upper) == (-163.0, 163.0)
    assert "CALIBRATED" in b.label


def mutate(fn) -> dict:  # type: ignore[no-untyped-def]
    data = copy.deepcopy(DEMO_CALIBRATION)
    fn(data)
    return data


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (mutate(lambda d: d.pop("gripper")), "expected"),
        (mutate(lambda d: d.update(extra_motor=d["gripper"])), "expected"),
        (mutate(lambda d: d["elbow_flex"].pop("range_max")), "missing"),
        (mutate(lambda d: d["elbow_flex"].update(range_min="800")), "integer"),
        (mutate(lambda d: d["elbow_flex"].update(range_min=3200, range_max=3100)), "invalid range"),
        (mutate(lambda d: d["elbow_flex"].update(range_max=5000)), "invalid range"),
        (mutate(lambda d: d["elbow_flex"].update(range_min=1000, range_max=1005)), "implausibly small"),
        (mutate(lambda d: d["elbow_flex"].update(id=9)), "id"),
        (mutate(lambda d: d["elbow_flex"].update(drive_mode=3)), "drive_mode"),
        (mutate(lambda d: d["elbow_flex"].update(homing_offset=5000)), "homing_offset"),
        (mutate(lambda d: d["elbow_flex"].update(foo=1)), "unexpected"),
        ([1, 2], "object"),
    ],
)
def test_invalid_calibrations_rejected(tmp_path: Path, data: object, match: str) -> None:
    with pytest.raises(CalibrationError, match=match):
        load_calibration(write(tmp_path, data))


def test_missing_corrupt_or_unselected_file(tmp_path: Path) -> None:
    with pytest.raises(CalibrationError, match="not found"):
        load_calibration(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(CalibrationError, match="corrupt"):
        load_calibration(bad)
    with pytest.raises(CalibrationError, match="no calibration"):
        load_calibration("")
    with pytest.raises(CalibrationError, match=r"\.json"):
        load_calibration(tmp_path / "x.yaml")

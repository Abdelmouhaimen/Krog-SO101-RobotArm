"""T-007/T-008 / offline tests 2-3: logical <-> MuJoCo conversions, round trips, boundaries."""

from __future__ import annotations

import itertools
import math

import pytest

from so101_companion.config import SimulationSection
from so101_companion.domain.joints import ARM_JOINTS, JOINT_NAMES
from so101_companion.simulation.mapping import JointMapping, mapping_from_config, sim_provisional_bounds

OFFICIAL = {  # radians, pinned so101_new_calib.xml
    "shoulder_pan": (-1.9198621771937616, 1.9198621771937634),
    "shoulder_lift": (-1.7453292519943224, 1.7453292519943366),
    "elbow_flex": (-1.69, 1.69),
    "wrist_flex": (-1.6580628494556928, 1.6580627293335335),
    "wrist_roll": (-2.7438472969992493, 2.841206309382605),
    "gripper": (-0.17453297762778586, 1.7453291995659765),
}


def default_mapping() -> JointMapping:
    return mapping_from_config(OFFICIAL, SimulationSection())


def test_arm_degrees_to_radians_uses_pi_over_180() -> None:
    m = default_mapping()
    for n in ARM_JOINTS:
        assert m.logical_to_model_unclamped(n, 90.0) == pytest.approx(math.pi / 2)
        assert m.logical_to_model_unclamped(n, -45.0) == pytest.approx(-math.pi / 4)
        assert m.logical_to_model_unclamped(n, 0.0) == 0.0  # mid-range zero in both conventions


def test_gripper_endpoints_and_midpoint_provisional_direction() -> None:
    m = default_mapping()
    lo, hi = OFFICIAL["gripper"]
    assert m.logical_to_model_unclamped("gripper", 0.0) == pytest.approx(lo)  # closed end of the model
    assert m.logical_to_model_unclamped("gripper", 100.0) == pytest.approx(hi)  # open end of the model
    assert m.logical_to_model_unclamped("gripper", 50.0) == pytest.approx((lo + hi) / 2)
    flipped = JointMapping(OFFICIAL, m.sign, m.offset_deg, "model_max")
    assert flipped.logical_to_model_unclamped("gripper", 0.0) == pytest.approx(hi)
    assert flipped.logical_to_model_unclamped("gripper", 100.0) == pytest.approx(lo)


@pytest.mark.parametrize("gripper_end", ["model_min", "model_max"])
@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_round_trip_logical_model_logical(gripper_end: str, sign: float) -> None:
    m = JointMapping(OFFICIAL, dict.fromkeys(ARM_JOINTS, sign), dict.fromkeys(ARM_JOINTS, 3.0), gripper_end)
    for n in JOINT_NAMES:
        lo, hi = m.model_range_logical()[n]
        for v in (lo, (lo + hi) / 2, hi):
            q = m.logical_to_model_unclamped(n, v)
            assert m.model_to_logical(n, q) == pytest.approx(v, abs=1e-9)


def test_boundaries_clamped_for_display_only() -> None:
    m = default_mapping()
    values = {**dict.fromkeys(JOINT_NAMES, 0.0), "shoulder_pan": 150.0, "gripper": 120.0}
    q, reasons = m.logical_to_model(values)
    assert q["shoulder_pan"] == pytest.approx(OFFICIAL["shoulder_pan"][1])
    assert q["gripper"] == pytest.approx(OFFICIAL["gripper"][1])
    assert set(reasons) == {"shoulder_pan:display_clamped", "gripper:display_clamped"}
    q, reasons = m.logical_to_model(dict.fromkeys(JOINT_NAMES, 0.0))
    assert reasons == []


def test_sign_and_offset_semantics() -> None:
    m = JointMapping(
        OFFICIAL,
        {**dict.fromkeys(ARM_JOINTS, 1.0), "elbow_flex": -1.0},
        {**dict.fromkeys(ARM_JOINTS, 0.0), "elbow_flex": 10.0},
    )
    assert m.logical_to_model_unclamped("elbow_flex", 20.0) == pytest.approx(math.radians(-20.0 + 10.0))


def test_invalid_mapping_rejected() -> None:
    with pytest.raises(ValueError):
        JointMapping(
            OFFICIAL, {**dict.fromkeys(ARM_JOINTS, 1.0), "elbow_flex": 0.5}, dict.fromkeys(ARM_JOINTS, 0.0)
        )
    with pytest.raises(ValueError):
        JointMapping(OFFICIAL, dict.fromkeys(ARM_JOINTS, 1.0), dict.fromkeys(ARM_JOINTS, 0.0), "open")
    with pytest.raises(ValueError):
        JointMapping({"gripper": (0, 1)}, dict.fromkeys(ARM_JOINTS, 1.0), dict.fromkeys(ARM_JOINTS, 0.0))


def test_sim_provisional_bounds_are_model_range_minus_margin() -> None:
    b = sim_provisional_bounds(default_mapping(), 5.0)
    assert b.limits["shoulder_pan"].upper == pytest.approx(110.0 - 5.0)
    assert b.limits["elbow_flex"].lower == pytest.approx(math.degrees(-1.69) + 5.0)
    assert (b.limits["gripper"].lower, b.limits["gripper"].upper) == (0.0, 100.0)
    assert "PROVISIONAL" in b.label


def test_full_turn_wrist_roll_wraps_into_model_range() -> None:
    m = JointMapping(
        OFFICIAL,
        dict.fromkeys(ARM_JOINTS, 1.0),
        {**dict.fromkeys(ARM_JOINTS, 0.0), "wrist_roll": 160.0},
        full_turn=frozenset({"wrist_roll"}),
    )
    lo, hi = (math.degrees(v) for v in OFFICIAL["wrist_roll"])  # about -157.2 .. +162.8
    q = math.degrees(m.logical_to_model_unclamped("wrist_roll", 100.0))  # 260 -> -100
    assert q == pytest.approx(-100.0)
    for v in (-180.0, -100.0, -20.0, 0.0, 2.0, 90.0, 179.0):
        q = m.logical_to_model_unclamped("wrist_roll", v)
        if lo <= math.degrees(q) <= hi:
            assert m.model_to_logical("wrist_roll", q) == pytest.approx(v)
    _, reasons = m.logical_to_model({**dict.fromkeys(JOINT_NAMES, 0.0), "gripper": 50.0, "wrist_roll": 100.0})
    assert reasons == []  # wrapped, not clamped
    assert m.model_range_logical()["wrist_roll"] == (-180.0, 180.0)
    # Hardware finding: offset -80 put the real rest pose (-74 .. -105) into the model's 40° gap.
    # The display must stay continuous there (true angle), never jump to the other end of the range.
    m80 = m.with_changes(offset_deg={**dict.fromkeys(ARM_JOINTS, 0.0), "wrist_roll": -80.0})
    shown = [
        math.degrees(
            m80.logical_to_model({**dict.fromkeys(JOINT_NAMES, 0.0), "gripper": 50.0, "wrist_roll": v})[0][
                "wrist_roll"
            ]
        )
        for v in range(-74, -106, -1)
    ]
    unwrapped = [(s - shown[0] + 180) % 360 - 180 for s in shown]
    steps = [abs(b - a) for a, b in itertools.pairwise(unwrapped)]
    assert max(steps) == pytest.approx(1.0)  # 1° per 1° of measurement: no lap, no jump


def test_without_full_turn_no_wrapping() -> None:
    m = JointMapping(
        OFFICIAL, dict.fromkeys(ARM_JOINTS, 1.0), {**dict.fromkeys(ARM_JOINTS, 0.0), "wrist_roll": 160.0}
    )
    _, reasons = m.logical_to_model({**dict.fromkeys(JOINT_NAMES, 0.0), "gripper": 50.0, "wrist_roll": 100.0})
    assert reasons == ["wrist_roll:display_clamped"]

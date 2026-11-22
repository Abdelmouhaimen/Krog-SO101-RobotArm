"""T-006 / offline test 1: exact six names, finite numbers, units."""

from __future__ import annotations

import math

import pytest

from so101_companion.domain.joints import (
    ARM_JOINTS,
    GRIPPER,
    JOINT_NAMES,
    JointFrame,
    JointFrameError,
    format_value,
    unit_of,
    validate_joint_values,
)

GOOD = dict.fromkeys(JOINT_NAMES, 1.0)


def test_canonical_names_and_units() -> None:
    assert JOINT_NAMES == (
        "shoulder_pan",
        "shoulder_lift",
        "elbow_flex",
        "wrist_flex",
        "wrist_roll",
        "gripper",
    )
    assert all(unit_of(n) == "deg" for n in ARM_JOINTS)
    assert unit_of(GRIPPER) == "pct"
    with pytest.raises(KeyError):
        unit_of("shoulder_pan_xyz")


def test_valid_frame_is_immutable_and_ordered() -> None:
    frame = JointFrame({**GOOD, "gripper": 50}, 1.5, 3)
    assert list(frame.values) == list(JOINT_NAMES)
    assert frame["gripper"] == 50.0 and isinstance(frame["gripper"], float)
    with pytest.raises(TypeError):
        frame.values["gripper"] = 1.0  # type: ignore[index]


@pytest.mark.parametrize(
    "values",
    [
        {k: v for k, v in GOOD.items() if k != "gripper"},  # omitted gripper
        {**GOOD, "shoulder_pan_xyz": 1.0},  # extra
        {**{k: v for k, v in GOOD.items() if k != "shoulder_pan"}, "shoulder_pan_xyz": 1.0},  # renamed
        {**GOOD, "elbow_flex": math.nan},
        {**GOOD, "elbow_flex": math.inf},
        {**GOOD, "elbow_flex": -math.inf},
        {**GOOD, "elbow_flex": "10"},
        {**GOOD, "elbow_flex": None},
        {**GOOD, "elbow_flex": True},
    ],
)
def test_invalid_frames_rejected(values: dict[str, object]) -> None:
    with pytest.raises(JointFrameError):
        validate_joint_values(values)  # type: ignore[arg-type]
    with pytest.raises(JointFrameError):
        JointFrame(values, 0.0)  # type: ignore[arg-type]


def test_non_mapping_and_bad_metadata_rejected() -> None:
    with pytest.raises(JointFrameError):
        validate_joint_values([1, 2, 3, 4, 5, 6])  # type: ignore[arg-type]
    with pytest.raises(JointFrameError):
        JointFrame(GOOD, math.nan)
    with pytest.raises(JointFrameError):
        JointFrame(GOOD, 0.0, -1)


def test_numpy_scalars_accepted_numpy_bool_rejected() -> None:
    import numpy as np

    vals = {**GOOD, "elbow_flex": np.float32(12.5), "gripper": np.int64(40)}
    frame = JointFrame(vals, 0.0)
    assert frame["elbow_flex"] == 12.5 and type(frame["gripper"]) is float
    with pytest.raises(JointFrameError):
        validate_joint_values({**GOOD, "elbow_flex": np.bool_(True)})
    with pytest.raises(JointFrameError):
        validate_joint_values({**GOOD, "elbow_flex": np.float64("nan")})


def test_unit_tags_are_checked() -> None:
    tagged = {n: (1.0, "deg") for n in ARM_JOINTS} | {"gripper": (50.0, "pct")}
    assert JointFrame.from_tagged(tagged, 0.0)["gripper"] == 50.0
    with pytest.raises(JointFrameError, match="gripper"):
        JointFrame.from_tagged({**tagged, "gripper": (50.0, "deg")}, 0.0)
    with pytest.raises(JointFrameError, match="elbow_flex"):
        JointFrame.from_tagged({**tagged, "elbow_flex": (0.5, "rad")}, 0.0)


def test_formatting_shows_units() -> None:
    assert format_value("elbow_flex", 12.34) == "+12.3°"
    assert format_value("gripper", 12.34) == "12.3 %"
    assert format_value("gripper", None) == "—"

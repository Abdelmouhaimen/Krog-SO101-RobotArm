"""FINAL Milestone 1 control and safety policy — fixed, centralized, NOT configurable (ADR-010).

Decided by the project owner on 2026-11-07 after supervised hardware commissioning:

* Requested joint positions are clamped ONLY to the calibrated bounds (5° margin on the five arm
  joints, gripper 0..100) and are then sent directly through LeRobot. There is no per-tick step
  limiter and LeRobot's measured-relative cap is disabled (``max_relative_target=None``).
* Servo PID, operating mode and electrical protections are LeRobot's ``configure()`` defaults,
  unchanged. Physical speed and torque remain hardware-limited; movement is never instantaneous.
* Communication failures latch a fault; no command is sent until manual recovery; nothing resumes
  automatically. Emergency Hold and Torque Off stay manual, separate operator actions.
* Tracking divergence (a joint accelerating away from its goal) is DIAGNOSTIC ONLY: it is logged and
  shown, and never triggers torque changes or faults by itself.
* Motion timing and smoothness are the caller's responsibility (future Behavior Engine trajectories).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

ANGULAR_MARGIN_DEG: Final = 5.0  # five arm joints only; never applied to the gripper
MAX_RELATIVE_TARGET: Final = None  # LeRobot SOFollowerConfig.max_relative_target (disabled)
DISABLE_TORQUE_ON_DISCONNECT: Final = True  # LeRobot default; the UI warns before disconnecting

# Diagnostics only (never act on the robot): see safety/divergence.py.
DIVERGENCE_STEP: Final = 4.0  # deg (or gripper units) moved in one tick away from the goals...
DIVERGENCE_ERROR: Final = 6.0  # ...while at least this far from them

SUMMARY: Final = (
    "Fixed M1 policy: calibrated bounds −5° margin (gripper 0–100) → sent directly via LeRobot "
    "(no step limiter, max_relative_target=None). Speed is limited only by the servos."
)

# Normal Stop -> return to the recorded physical rest pose (owner decisions 2026-11-07, not configurable).
REST_RETURN_SPEED: Final = (
    30.0  # deg/s for arm joints and units/s for the gripper (straight-line, synchronized)
)
REST_MIN_DURATION_S: Final = 0.5
REST_ARRIVAL_TOL_ARM_DEG: Final = 3.0
REST_ARRIVAL_TOL_GRIPPER: Final = 5.0
REST_TIMEOUT_EXTRA_S: Final = 3.0  # arrival must happen within trajectory duration + this

# Owner-approved usable range for joints whose LeRobot calibration does not record a physical range
# (wrist_roll is calibrated as a full turn, 0..4095 -> ±180°). Intersected with the calibrated bounds
# (and the simulation slider bounds). Measured 2026-11-07: the wrist's mechanical dead zone was
# recentred onto the encoder seam (motor 5 Homing_Offset -669 -> 381), giving one continuous arc with
# end stops at -168.4° / +168.5°; minus the 5° margin -> ±163°. Valid for THIS calibration
# (left_follower.json, Homing_Offset 381); re-measure after any recalibration.
JOINT_RANGE_LIMITS: Final[dict[str, tuple[float, float]]] = {"wrist_roll": (-163.0, 163.0)}

# Stall-aware timelines (owner decisions D1/D4, 2026-11-07, ADR-013/ADR-014). Each issued control tick
# advances a trajectory's logical clock by at most ONE nominal control period: a serial stall delays the
# timeline instead of making the next target jump ahead (no skipped segments, no catch-up burst).
# Applies to behavior playback AND to the M1 return-to-rest trajectory.
TIMELINE_MAX_STEP_PERIODS: Final = 1.0

# ---------------------------------------------------------------- Milestone 2 Behavior Engine (ADR-013)
# Fixed, not configurable. None of these limits the recorded behavior content itself (linear
# interpolation at its recorded timing x playback speed); they shape only the generated transitions
# and the workflow gates.
#
# Entry and loop-bridge transitions (owner decision D8, 2026-11-07, ADR-015; replaces the 30°/s cap of
# D2). Synchronized minimum-jerk segments, as short as the PER-JOINT speed AND acceleration limits below
# allow. The return to rest is NOT affected (REST_RETURN_SPEED / REST_MIN_DURATION_S above).
# Basis: STS3215 no-load speed ~270°/s at 7.4 V (datasheet, falls with voltage; this arm runs from a 5 V
# supply that has caused one undervoltage error); verified on this arm: the wrist at ~220°/s on fast
# slider moves (ADR-012). Not yet measured on this arm: the gravity-loaded shoulder_lift / elbow_flex,
# hence their lower values. Gate B step B11 measures the real tracking before these are raised.
TRANSITION_MAX_SPEED: Final[Mapping[str, float]] = MappingProxyType(
    {
        "shoulder_pan": 120.0,
        "shoulder_lift": 90.0,
        "elbow_flex": 110.0,
        "wrist_flex": 150.0,
        "wrist_roll": 150.0,
        "gripper": 150.0,  # units/s on the 0..100 scale
    }
)
TRANSITION_MAX_ACCEL: Final[Mapping[str, float]] = MappingProxyType(  # deg/s² (gripper units/s²)
    {
        "shoulder_pan": 500.0,
        "shoulder_lift": 350.0,
        "elbow_flex": 450.0,
        "wrist_flex": 700.0,
        "wrist_roll": 700.0,
        "gripper": 700.0,
    }
)
TRANSITION_MIN_DURATION_S: Final = 0.15  # 3 control periods: even a tiny transition is a ramp, not a step
# A loop whose last pose is within this of its first pose repeats immediately (no bridge). The step at
# the seam is then at most this large, i.e. smaller than what the recorded content itself does per tick.
LOOP_SEAMLESS_TOL_ARM_DEG: Final = 1.0
LOOP_SEAMLESS_TOL_GRIPPER: Final = 2.0


class TransitionProfile(enum.StrEnum):
    """Operator presets for the entry/resume move and the loop bridge (Studio UI, 2026-11-07). Each is a
    FIXED scale of the per-joint limits above (never above them): FAST = the limits verified in B11,
    GENTLE ~ the old 30 °/s convention. The seamless-loop skip applies to every preset."""

    GENTLE = "gentle"
    NORMAL = "normal"
    FAST = "fast"

    @property
    def speed_scale(self) -> float:
        return {"gentle": 0.3, "normal": 0.6, "fast": 1.0}[self.value]

    @property
    def accel_scale(self) -> float:
        return {"gentle": 0.15, "normal": 0.45, "fast": 1.0}[self.value]

    @property
    def min_duration_s(self) -> float:
        return {"gentle": 0.5, "normal": 0.25, "fast": TRANSITION_MIN_DURATION_S}[self.value]

    @property
    def label(self) -> str:
        return self.value.capitalize()


DEFAULT_ENTRY_PROFILE: Final = TransitionProfile.NORMAL  # an entry can start from anywhere
DEFAULT_BRIDGE_PROFILE: Final = TransitionProfile.FAST  # bridges are short by construction
PLAYBACK_SPEED_MIN: Final = 0.5
PLAYBACK_SPEED_MAX: Final = 2.0
RECORDING_MAX_GAP_S: Final = 0.250  # adjacent samples further apart invalidate a recording (0.250 is valid)
RECORDING_MAX_DURATION_S: Final = 600.0  # a take is stopped automatically after 10 minutes
# The authorizing preview starts from the measured pose; Play needs a new preview if the arm has moved
# further than this since (same convention as the rest-arrival tolerance).
PREVIEW_START_TOL_ARM_DEG: Final = REST_ARRIVAL_TOL_ARM_DEG
PREVIEW_START_TOL_GRIPPER: Final = REST_ARRIVAL_TOL_GRIPPER

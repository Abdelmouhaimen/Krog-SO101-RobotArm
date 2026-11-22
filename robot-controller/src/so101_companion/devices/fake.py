"""Deterministic fake SO-101 for tests and offline demos. Never touches any device.

Like LeRobot with the fixed M1 policy (``max_relative_target=None``) it forwards every goal
unchanged (sent == staged). Fault injection covers read/write/torque/connect failures, corrupt
telemetry, blocking latency, servo lag/inertia (``follow``/``momentum``) and runaway joints.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from so101_companion.devices.calibration import CalibrationData, load_calibration
from so101_companion.devices.protocol import (
    ConnectError,
    ReadError,
    TelemetryError,
    TorqueCommandError,
    WriteError,
)
from so101_companion.domain.joints import JOINT_NAMES, JointFrame

DEFAULT_POSE = dict.fromkeys(JOINT_NAMES, 0.0)


@dataclass
class FaultPlan:
    fail_connect: bool = False
    calibration_mismatch: bool = False
    fail_read_after: int | None = None  # number of successful observe() calls before failures start
    fail_write_after: int | None = None  # number of successful send() calls before failures start
    fail_torque_off: bool = False
    corrupt_read_after: int | None = None  # return NaN telemetry after N good reads
    read_latency_s: float = 0.0
    write_latency_s: float = 0.0
    unplugged: bool = False  # every bus call fails, like a removed USB adapter
    # Per-joint drift applied on every send while that joint has torque: emulates a servo running away
    # from its goal (observed on hardware: wrist_roll spun ~337° on goal == present).
    runaway: dict[str, float] = field(default_factory=dict)


@dataclass
class FakeRobot:
    """Fake RobotPort. ``follow`` is the fraction of the remaining distance the fake motors travel
    toward the goal after each send (1.0 = reach instantly)."""

    initial_pose: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_POSE))
    follow: float = 1.0
    momentum: float = 0.0  # fraction of the previous step's velocity carried into the next (inertia)
    faults: FaultPlan = field(default_factory=FaultPlan)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    disable_torque_on_disconnect: bool = True
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._pose = dict(self.initial_pose)
        self._goal = dict(self.initial_pose)
        self._connected = False
        self._torque = False
        self._reads = 0
        self._writes = 0
        self._seq = 0
        self._calibration: CalibrationData | None = None
        self.torque_off_set: set[str] = set()
        self._velocity = dict.fromkeys(JOINT_NAMES, 0.0)

    # -- RobotPort -------------------------------------------------------------------------
    @property
    def calibration(self) -> CalibrationData | None:
        return self._calibration

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def torque_enabled(self) -> bool:
        return self._torque

    @property
    def pose(self) -> dict[str, float]:
        return dict(self._pose)

    def count(self, name: str) -> int:
        return sum(1 for c, _ in self.calls if c == name)

    def initialize(self, calibration_path: Path, port: str) -> None:
        self.calls.append(("initialize", (str(calibration_path), port)))
        self._calibration = load_calibration(calibration_path)
        if self.faults.fail_connect or self.faults.unplugged:
            raise ConnectError(f"fake: could not open {port}")
        self._connected = True
        # LeRobot configure(): torque disabled while configuring, then re-enabled.
        self._torque = True

    def verify_calibration(self) -> bool:
        self.calls.append(("verify_calibration", None))
        self._check_link(ReadError)
        return not self.faults.calibration_mismatch

    def observe(self) -> JointFrame:
        self.calls.append(("observe", None))
        if self.faults.read_latency_s:
            self.sleep(self.faults.read_latency_s)
        self._check_link(ReadError)
        if self.faults.fail_read_after is not None and self._reads >= self.faults.fail_read_after:
            raise ReadError("fake: sync_read Present_Position failed")
        if self.faults.corrupt_read_after is not None and self._reads >= self.faults.corrupt_read_after:
            raise TelemetryError("fake: observation contained NaN for elbow_flex")
        self._reads += 1
        return self._frame(self._pose)

    def send(self, targets: JointFrame) -> JointFrame:
        self.calls.append(("send", targets.as_dict()))
        if self.faults.write_latency_s:
            self.sleep(self.faults.write_latency_s)
        self._check_link(WriteError)
        if self.faults.fail_write_after is not None and self._writes >= self.faults.fail_write_after:
            raise WriteError("fake: sync_write Goal_Position failed")
        goal = targets.as_dict()
        self._writes += 1
        with self._lock:
            self._goal = dict(goal)
            if self._torque:
                for n in JOINT_NAMES:
                    if n in self.torque_off_set:
                        continue
                    if n in self.faults.runaway:
                        self._pose[n] += self.faults.runaway[n]
                    else:
                        step = self.follow * (goal[n] - self._pose[n]) + self.momentum * self._velocity[n]
                        self._velocity[n] = step
                        self._pose[n] += step
        return self._frame(goal)

    def torque_off(self) -> None:
        self.calls.append(("torque_off", None))
        self._check_link(TorqueCommandError)
        if self.faults.fail_torque_off:
            raise TorqueCommandError("fake: Torque_Enable write failed")
        self._torque = False
        self.torque_off_set = set(JOINT_NAMES)

    def enable_torque_holding_present(self) -> JointFrame:
        self.calls.append(("enable_torque_holding_present", None))
        self.torque_off_set.clear()
        self._check_link(TorqueCommandError)
        self._goal = dict(self._pose)
        self._torque = True
        return self._frame(self._pose)

    def disconnect(self) -> None:
        self.calls.append(("disconnect", None))
        was_connected = self._connected
        self._connected = False
        if self.faults.unplugged:
            raise ConnectError("fake: port vanished during disconnect")
        if was_connected and self.disable_torque_on_disconnect:
            self._torque = False

    def describe(self) -> dict[str, str]:
        return {"driver": "FakeRobot (offline test double)", "lerobot_version": "n/a"}

    # -- test helpers ----------------------------------------------------------------------
    def move_by_hand(self, **values: float) -> None:
        """Simulate the operator moving joints (e.g. with torque off)."""
        with self._lock:
            self._pose.update(values)

    def _check_link(self, err: type[Exception]) -> None:
        if self.faults.unplugged:
            raise err("fake: [TxRxResult] There is no status packet!")
        if not self._connected:
            raise err("fake: not connected")

    def _frame(self, values: Mapping[str, float]) -> JointFrame:
        self._seq += 1
        return JointFrame(dict(values), self.clock(), self._seq)


DEMO_CALIBRATION: dict[str, dict[str, int]] = {
    # Plausible SO-101 values in LeRobot 0.4.4 file format — FOR TESTS/DEMO ONLY, not a real arm.
    "shoulder_pan": {"id": 1, "drive_mode": 0, "homing_offset": -1450, "range_min": 760, "range_max": 3340},
    "shoulder_lift": {"id": 2, "drive_mode": 0, "homing_offset": 1180, "range_min": 830, "range_max": 3230},
    "elbow_flex": {"id": 3, "drive_mode": 0, "homing_offset": 980, "range_min": 880, "range_max": 3120},
    "wrist_flex": {"id": 4, "drive_mode": 0, "homing_offset": -1700, "range_min": 860, "range_max": 3190},
    "wrist_roll": {"id": 5, "drive_mode": 0, "homing_offset": 160, "range_min": 0, "range_max": 4095},
    "gripper": {"id": 6, "drive_mode": 0, "homing_offset": 1320, "range_min": 2030, "range_max": 3460},
}


def write_demo_calibration(directory: Path, robot_id: str = "fake_follower") -> Path:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{robot_id}.json"
    path.write_text(json.dumps(DEMO_CALIBRATION, indent=4))
    return path

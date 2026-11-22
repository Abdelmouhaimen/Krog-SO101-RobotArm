"""Real SO-101 follower adapter over LeRobot's ``SOFollower`` (pinned LeRobot 0.4.4, ADR-006).

LeRobot is imported LAZILY, only inside :meth:`LeRobotSO101Adapter.initialize`, which the controller
calls only after explicit operator confirmation. Importing this module imports nothing from LeRobot.

Every LeRobot call used here was verified in the installed 0.4.4 source (== git tag v0.4.4,
8fff0fde7c79f23a93d845d1a50e985de01f8b8a):

* ``SOFollowerRobotConfig(port, id, calibration_dir, use_degrees, max_relative_target,
  disable_torque_on_disconnect)``; ``Robot.__init__`` loads ``calibration_dir/<id>.json``.
* ``SOFollower.connect(calibrate=False)`` → ``bus.connect()`` (handshake) then ``configure()``, which
  runs inside ``bus.torque_disabled()`` (torque OFF, then ON again) and writes operating mode, PID
  and the gripper's protective torque/current limits. Not read-only — hence the confirmation.
* ``SOFollower.is_calibrated`` → ``bus.read_calibration()`` (register reads) compared with the file.
* ``get_observation()`` → ``{"<motor>.pos": float}``; ``send_action(action)`` writes Goal_Position and
  RETURNS the goal actually sent. Per the fixed M1 policy (ADR-010) ``max_relative_target=None``, so
  LeRobot applies NO measured-relative clipping and does not read Present_Position inside
  send_action; all six keys are still always sent.
* ``bus.disable_torque(num_retry)`` (also what SO-101-VR-Control uses), ``bus.enable_torque()``,
  ``bus.sync_read/sync_write``, ``bus.disconnect(disable_torque)``, ``SOFollower.disconnect()``.

Hardware behaviour: connect/observe/send/torque-off/disconnect exercised on the user's SO-101 on
2026-11-07 (see IMPLEMENTATION_STATUS.md).
"""

from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from so101_companion.devices.calibration import CalibrationData, load_calibration
from so101_companion.devices.protocol import (
    ConnectError,
    HardwareError,
    ReadError,
    TelemetryError,
    TorqueCommandError,
    UnsupportedDriverError,
    WriteError,
)
from so101_companion.domain.joints import ARM_JOINTS, GRIPPER, JOINT_NAMES, JointFrame, JointFrameError
from so101_companion.safety import policy

ACTION_KEYS = tuple(f"{n}.pos" for n in JOINT_NAMES)
TORQUE_OFF_RETRIES = 5  # same retry count LeRobot's own bus.disconnect() uses for disable_torque


@dataclass(frozen=True)
class LeRobotApi:
    version: str
    robot_cls: Any
    config_cls: Any


def load_lerobot_api() -> LeRobotApi:
    try:
        from lerobot.robots.so_follower.so_follower import SOFollower, SOFollowerRobotConfig
    except Exception as exc:  # ImportError, or LeRobot's own import-time errors
        raise UnsupportedDriverError(
            "LeRobot is not installed/importable. Install the hardware lock: "
            "uv pip sync --torch-backend cpu requirements/hardware-linux-x86_64-py312.lock "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    return LeRobotApi(importlib.metadata.version("lerobot"), SOFollower, SOFollowerRobotConfig)


# Abandoned LeRobot robot objects are parked here so that garbage collection cannot trigger
# LeRobot's ``Robot.__del__`` -> ``disconnect()`` -> torque-disable writes mid-session (FR-13:
# never toggle torque blindly after a link failure).
_PARKED: list[Any] = []


class LeRobotSO101Adapter:
    def __init__(
        self,
        *,
        allowed_versions: tuple[str, ...],
        api_loader: Callable[[], LeRobotApi] = load_lerobot_api,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._disable_torque_on_disconnect = policy.DISABLE_TORQUE_ON_DISCONNECT
        self._allowed_versions = tuple(allowed_versions)
        self._api_loader = api_loader
        self._clock = clock
        self._robot: Any = None
        self._api: LeRobotApi | None = None
        self._calibration: CalibrationData | None = None
        self._port = ""
        self._seq = 0

    # -- properties ------------------------------------------------------------------------
    @property
    def calibration(self) -> CalibrationData | None:
        return self._calibration

    @property
    def disable_torque_on_disconnect(self) -> bool:
        return self._disable_torque_on_disconnect

    @property
    def robot(self) -> Any:
        return self._robot

    def describe(self) -> dict[str, str]:
        return {
            "driver": "LeRobot SOFollower",
            "lerobot_version": self._api.version if self._api else "not loaded",
            "robot_id": self._calibration.robot_id if self._calibration else "",
            "max_relative_target": str(policy.MAX_RELATIVE_TARGET),
        }

    # -- lifecycle -------------------------------------------------------------------------
    def initialize(self, calibration_path: Path, port: str) -> None:
        if self._robot is not None:
            raise ConnectError("adapter already initialized; disconnect first")
        port = (port or "").strip()
        if not port.startswith("/dev/"):
            err = ConnectError(f"invalid port {port!r}: select the servo bus device explicitly (/dev/...)")
            err.port_touched = False
            raise err
        calibration = load_calibration(calibration_path)  # CalibrationError before any driver import
        api = self._api_loader()
        if api.version not in self._allowed_versions:
            raise UnsupportedDriverError(
                f"LeRobot {api.version} is not in the source-verified allow-list {self._allowed_versions}"
            )
        cfg = api.config_cls(
            port=port,
            id=calibration.robot_id,
            calibration_dir=calibration.calibration_dir,
            use_degrees=True,
            max_relative_target=policy.MAX_RELATIVE_TARGET,
            disable_torque_on_disconnect=self._disable_torque_on_disconnect,
        )
        robot = api.robot_cls(cfg)  # does not open the port; loads the calibration file
        self._check_driver_contract(robot, calibration)
        try:
            robot.connect(calibrate=False)
        except Exception as exc:
            self._close_without_torque_change(robot)
            raise ConnectError(f"LeRobot connect failed on {port}: {type(exc).__name__}: {exc}") from exc
        self._robot, self._api, self._calibration, self._port = robot, api, calibration, port

    def _check_driver_contract(self, robot: Any, calibration: CalibrationData) -> None:
        motors = getattr(robot.bus, "motors", {})
        if tuple(motors) != JOINT_NAMES:
            raise UnsupportedDriverError(f"LeRobot SOFollower motors {tuple(motors)} != {JOINT_NAMES}")
        modes = {n: str(getattr(motors[n].norm_mode, "value", motors[n].norm_mode)) for n in JOINT_NAMES}
        if any(modes[n] != "degrees" for n in ARM_JOINTS) or modes[GRIPPER] != "range_0_100":
            raise UnsupportedDriverError(f"unexpected normalization modes {modes}")
        if set(robot.action_features) != set(ACTION_KEYS):
            raise UnsupportedDriverError(f"unexpected action features {sorted(robot.action_features)}")
        if robot.config.max_relative_target is not policy.MAX_RELATIVE_TARGET:
            raise UnsupportedDriverError("max_relative_target must be None (fixed M1 policy, ADR-010)")
        loaded = getattr(robot, "calibration", {}) or {}
        for name, cal in calibration.motors.items():
            got = loaded.get(name)
            if got is None or (got.id, got.drive_mode, got.homing_offset, got.range_min, got.range_max) != (
                cal.id,
                cal.drive_mode,
                cal.homing_offset,
                cal.range_min,
                cal.range_max,
            ):
                raise ConnectError(
                    f"LeRobot did not load the same calibration for {name!r} from {calibration.path}"
                )

    @staticmethod
    def _close_without_torque_change(robot: Any) -> None:
        bus = getattr(robot, "bus", None)
        try:
            if bus is not None and bus.is_connected:
                bus.disconnect(disable_torque=False)
        except Exception:
            pass
        _PARKED.append(robot)

    def verify_calibration(self) -> bool:
        robot = self._require()
        try:
            return bool(robot.is_calibrated)
        except Exception as exc:
            raise ReadError(f"reading calibration registers failed: {type(exc).__name__}: {exc}") from exc

    # -- data path -------------------------------------------------------------------------
    def observe(self) -> JointFrame:
        robot = self._require()
        try:
            obs = robot.get_observation()
        except Exception as exc:
            raise ReadError(f"get_observation failed: {type(exc).__name__}: {exc}") from exc
        return self._to_frame(obs, "observation")

    def send(self, targets: JointFrame) -> JointFrame:
        robot = self._require()
        action = {f"{n}.pos": float(targets[n]) for n in JOINT_NAMES}
        try:
            sent = robot.send_action(action)
        except Exception as exc:
            raise WriteError(f"send_action failed: {type(exc).__name__}: {exc}") from exc
        return self._to_frame(sent, "send_action return")

    def torque_off(self) -> None:
        robot = self._require()
        try:
            robot.bus.disable_torque(num_retry=TORQUE_OFF_RETRIES)
        except Exception as exc:
            raise TorqueCommandError(f"disable_torque failed: {type(exc).__name__}: {exc}") from exc

    def enable_torque_holding_present(self) -> JointFrame:
        """Goal_Position := Present_Position (written while torque is off, so no motion), then enable
        torque so the servos hold where the arm currently is instead of jumping to a stale goal."""
        robot = self._require()
        try:
            present = robot.bus.sync_read("Present_Position")
            robot.bus.sync_write("Goal_Position", dict(present))
            robot.bus.enable_torque()
        except Exception as exc:
            raise TorqueCommandError(f"enable torque failed: {type(exc).__name__}: {exc}") from exc
        return self._to_frame({f"{k}.pos": v for k, v in present.items()}, "present position")

    def disconnect(self) -> None:
        robot = self._robot
        self._robot = None
        if robot is None:
            return
        try:
            robot.disconnect()  # disables torque iff disable_torque_on_disconnect (LeRobot)
        except Exception as exc:
            _PARKED.append(robot)
            raise ConnectError(f"disconnect failed: {type(exc).__name__}: {exc}") from exc

    # -- helpers ---------------------------------------------------------------------------
    def _require(self) -> Any:
        if self._robot is None:
            raise HardwareError("robot not initialized")
        return self._robot

    def _to_frame(self, data: Any, what: str) -> JointFrame:
        if not isinstance(data, Mapping):
            raise TelemetryError(f"{what}: expected dict, got {type(data).__name__}")
        values = {k.removesuffix(".pos"): v for k, v in data.items() if k.endswith(".pos")}
        try:
            self._seq += 1
            return JointFrame(values, self._clock(), self._seq)
        except JointFrameError as exc:
            raise TelemetryError(f"{what}: {exc}") from exc

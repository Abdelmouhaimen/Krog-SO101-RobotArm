"""LeRobot 0.4.4 contract tests (T-024..T-029; fixed M1 policy ADR-010: max_relative_target=None).

The REAL ``lerobot.robots.so_follower.SOFollower`` / ``SOFollowerRobotConfig`` / ``send_action``
code runs; only the serial motors bus is replaced by ``FakeBus`` after
construction (constructing LeRobot's FeetechMotorsBus opens nothing — verified in scservo_sdk
PortHandler.__init__; the device guard would fail the test otherwise).
Skipped automatically when the hardware lock (LeRobot) is not installed.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("allow_driver_imports")

from helpers import HW_POSE  # noqa: E402
from so101_companion.control import requests as rq  # noqa: E402
from so101_companion.control.clock import FakeClock  # noqa: E402
from so101_companion.control.coordinator import ControllerCoordinator  # noqa: E402
from so101_companion.devices.lerobot_so101 import LeRobotApi, LeRobotSO101Adapter  # noqa: E402
from so101_companion.devices.protocol import (  # noqa: E402
    ConnectError,
    ReadError,
    UnsupportedDriverError,
    WriteError,
)
from so101_companion.domain.joints import JOINT_NAMES  # noqa: E402
from so101_companion.domain.state import Connection, Mode  # noqa: E402
from so101_companion.safety.bounds import build_bounds  # noqa: E402

CONTRACT_PORT = "/dev/ttyUSB-so101-contract-test"  # nonexistent; guarded


@pytest.fixture(scope="module")
def lerobot_mod() -> Any:
    from so101_companion.diagnostics import device_guard

    prev = device_guard.set_forbidden_modules(frozenset())
    try:
        so_follower = pytest.importorskip("lerobot.robots.so_follower.so_follower")
    finally:
        device_guard.set_forbidden_modules(prev)
    return so_follower


class FakeBus:
    """Implements exactly the SerialMotorsBus surface SOFollower 0.4.4 and our adapter use."""

    def __init__(self, real_bus: Any, present: dict[str, float]) -> None:
        self.motors = real_bus.motors
        self.calibration = real_bus.calibration
        self.present = dict(present)
        self.log: list[tuple[Any, ...]] = []
        self.connected = False
        self.torque = False
        self.calibration_matches = True
        self.fail_connect = False
        self.fail_reads = False
        self.fail_writes = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    def connect(self, handshake: bool = True) -> None:
        self.log.append(("connect", handshake))
        if self.fail_connect:
            self.connected = True  # port opened, handshake failed (LeRobot leaves it open)
            raise ConnectionError("Could not connect on port: handshake failed")
        self.connected = True

    @property
    def is_calibrated(self) -> bool:
        self.log.append(("read_calibration",))
        return self.calibration_matches

    @contextlib.contextmanager
    def torque_disabled(self, motors: Any = None) -> Iterator[None]:
        self.disable_torque(motors)
        try:
            yield
        finally:
            self.enable_torque(motors)

    def configure_motors(self) -> None:
        self.log.append(("configure_motors",))

    def write(self, data_name: str, motor: str, value: Any, **kw: Any) -> None:
        self.log.append(("write", data_name, motor, value))

    def sync_read(self, data_name: str, motors: Any = None, **kw: Any) -> dict[str, float]:
        self.log.append(("sync_read", data_name))
        if self.fail_reads:
            raise ConnectionError(
                "Failed to sync read 'Present_Position' [TxRxResult] There is no status packet!"
            )
        return dict(self.present)

    def sync_write(self, data_name: str, values: dict[str, float], **kw: Any) -> None:
        self.log.append(("sync_write", data_name, dict(values)))
        if self.fail_writes:
            raise ConnectionError("Failed to sync write 'Goal_Position'")

    def disable_torque(self, motors: Any = None, num_retry: int = 0) -> None:
        self.log.append(("disable_torque", num_retry))
        self.torque = False

    def enable_torque(self, motors: Any = None, num_retry: int = 0) -> None:
        self.log.append(("enable_torque",))
        self.torque = True

    def disconnect(self, disable_torque: bool = True) -> None:
        self.log.append(("bus_disconnect", disable_torque))
        if disable_torque:
            self.disable_torque(num_retry=5)
        self.connected = False


def make_api(
    lerobot_mod: Any, present: dict[str, float] | None = None, version: str | None = None, tweak: Any = None
) -> tuple[LeRobotApi, list[Any]]:
    import importlib.metadata

    created: list[Any] = []
    base = lerobot_mod.SOFollower

    class SOFollowerWithFakeBus(base):  # type: ignore[misc, valid-type]
        def __init__(self, config: Any) -> None:
            super().__init__(config)  # REAL LeRobot init: config, calibration file load, real bus object
            self.bus = FakeBus(self.bus, present or dict(HW_POSE))
            if tweak:
                tweak(self.bus)
            created.append(self)

    return (
        LeRobotApi(
            version or importlib.metadata.version("lerobot"),
            SOFollowerWithFakeBus,
            lerobot_mod.SOFollowerRobotConfig,
        ),
        created,
    )


def adapter(api: LeRobotApi, allowed: tuple[str, ...] = ("0.4.4",)) -> LeRobotSO101Adapter:
    return LeRobotSO101Adapter(allowed_versions=allowed, api_loader=lambda: api)


# ------------------------------------------------------------------------------------- T-024
def test_installed_lerobot_is_the_pinned_version_with_six_motors(lerobot_mod: Any) -> None:
    import importlib.metadata

    assert importlib.metadata.version("lerobot") == "0.4.4"
    cfg = lerobot_mod.SOFollowerRobotConfig(
        port=CONTRACT_PORT, id="x", calibration_dir=Path("/nonexistent-cal")
    )
    assert (
        cfg.use_degrees is True
        and cfg.disable_torque_on_disconnect is True
        and cfg.max_relative_target is None
    )


def test_initialize_builds_native_config_and_connects_without_calibrating(
    lerobot_mod: Any, calibration_file: Path
) -> None:
    api, created = make_api(lerobot_mod)
    a = adapter(api)
    a.initialize(calibration_file, CONTRACT_PORT)
    robot = created[0]
    cfg = robot.config
    assert cfg.port == CONTRACT_PORT and cfg.id == "test_follower" and cfg.use_degrees is True
    assert cfg.calibration_dir == calibration_file.parent.resolve()
    assert cfg.max_relative_target is None  # fixed M1 policy (ADR-010)
    assert cfg.disable_torque_on_disconnect is True
    assert tuple(robot.bus.motors) == JOINT_NAMES
    # LeRobot loaded the very same calibration file
    assert robot.calibration["elbow_flex"].range_min == 880
    log = robot.bus.log
    assert log[0] == ("connect", True)  # handshake
    assert (
        "read_calibration",
    ) in log  # connect() evaluates is_calibrated, calibrate=False -> no calibrate()
    names = [e[0] for e in log]
    i_off, i_on = names.index("disable_torque"), names.index("enable_torque")
    assert i_off < names.index("configure_motors") < i_on  # configure() inside torque_disabled()
    writes = {(e[1], e[2], e[3]) for e in log if e[0] == "write"}
    # existing LeRobot gripper protections preserved (no change by us)
    assert {
        ("Max_Torque_Limit", "gripper", 500),
        ("Protection_Current", "gripper", 250),
        ("Overload_Torque", "gripper", 25),
    } <= writes
    assert not any(e[0] == "sync_write" for e in log)  # connecting sends no goal positions


def test_calibration_register_check_is_reported(lerobot_mod: Any, calibration_file: Path) -> None:
    api, created = make_api(lerobot_mod)
    a = adapter(api)
    a.initialize(calibration_file, CONTRACT_PORT)
    assert a.verify_calibration() is True
    created[0].bus.calibration_matches = False
    assert a.verify_calibration() is False


# ------------------------------------------------------------------------------------- T-026 / T-027
def test_send_forwards_targets_directly_and_returns_sent(lerobot_mod: Any, calibration_file: Path) -> None:
    """Fixed M1 policy: max_relative_target=None -> LeRobot writes the targets unchanged and does
    NOT read Present_Position inside send_action."""
    from so101_companion.domain.joints import JointFrame

    present = {**HW_POSE, "shoulder_lift": 10.0, "gripper": 0.0}
    api, created = make_api(lerobot_mod, present)
    a = adapter(api)
    a.initialize(calibration_file, CONTRACT_PORT)
    created[0].bus.log.clear()
    staged = {**present, "shoulder_lift": 95.0, "gripper": 80.0, "elbow_flex": present["elbow_flex"] - 60.0}
    sent = a.send(JointFrame(staged, 0.0))
    assert sent.as_dict() == pytest.approx(staged)  # large jumps forwarded unchanged
    log = created[0].bus.log
    assert [e[0] for e in log] == ["sync_write"]  # no Present_Position read for a cap
    assert log[0][1] == "Goal_Position" and log[0][2] == pytest.approx(staged)


def test_observe_maps_pos_keys_only_from_observation(lerobot_mod: Any, calibration_file: Path) -> None:
    api, _ = make_api(lerobot_mod, {**HW_POSE, "wrist_roll": -12.5})
    a = adapter(api)
    a.initialize(calibration_file, CONTRACT_PORT)
    frame = a.observe()
    assert frame.as_dict() == pytest.approx({**HW_POSE, "wrist_roll": -12.5})


# ------------------------------------------------------------------------------------- T-028 / T-029
def test_read_and_write_failures_map_to_typed_errors(lerobot_mod: Any, calibration_file: Path) -> None:
    from so101_companion.domain.joints import JointFrame

    api, created = make_api(lerobot_mod)
    a = adapter(api)
    a.initialize(calibration_file, CONTRACT_PORT)
    created[0].bus.fail_reads = True
    with pytest.raises(ReadError):
        a.observe()
    created[0].bus.fail_reads = False
    created[0].bus.fail_writes = True
    with pytest.raises(WriteError):
        a.send(JointFrame(HW_POSE, 0.0))


def test_torque_off_and_enable_torque_use_verified_bus_api(lerobot_mod: Any, calibration_file: Path) -> None:
    api, created = make_api(lerobot_mod)
    a = adapter(api)
    a.initialize(calibration_file, CONTRACT_PORT)
    bus = created[0].bus
    bus.log.clear()
    a.torque_off()
    assert bus.log == [("disable_torque", 5)] and bus.torque is False
    bus.log.clear()
    frame = a.enable_torque_holding_present()
    assert [e[0] for e in bus.log] == ["sync_read", "sync_write", "enable_torque"]
    assert bus.log[1][1] == "Goal_Position" and bus.log[1][2] == pytest.approx(HW_POSE)
    assert frame.as_dict() == pytest.approx(HW_POSE) and bus.torque is True


def test_disconnect_disables_torque_per_fixed_policy(lerobot_mod: Any, calibration_file: Path) -> None:
    api, created = make_api(lerobot_mod)
    a = adapter(api)
    a.initialize(calibration_file, CONTRACT_PORT)
    a.disconnect()
    assert ("bus_disconnect", True) in created[0].bus.log
    assert created[0].bus.connected is False


def test_connect_failure_closes_port_without_touching_torque(
    lerobot_mod: Any, calibration_file: Path
) -> None:
    api, created = make_api(lerobot_mod, tweak=lambda bus: setattr(bus, "fail_connect", True))
    a = adapter(api)
    with pytest.raises(ConnectError):
        a.initialize(calibration_file, CONTRACT_PORT)
    log = created[0].bus.log
    assert ("bus_disconnect", False) in log
    assert not any(e[0] in ("disable_torque", "enable_torque") for e in log)


def test_unverified_lerobot_version_refused_before_connecting(
    lerobot_mod: Any, calibration_file: Path
) -> None:
    api, created = make_api(lerobot_mod, version="0.7.0")
    with pytest.raises(UnsupportedDriverError, match="allow-list"):
        adapter(api).initialize(calibration_file, CONTRACT_PORT)
    assert created == []


def test_port_must_be_explicit(lerobot_mod: Any, calibration_file: Path) -> None:
    api, created = make_api(lerobot_mod)
    for port in ("", "auto", "COM3"):
        with pytest.raises(ConnectError):
            adapter(api).initialize(calibration_file, port)
    assert created == []


# ------------------------------------------------------------------------------------- end to end
def test_coordinator_drives_real_lerobot_code_end_to_end(lerobot_mod: Any, calibration_file: Path) -> None:
    present = {**HW_POSE, "shoulder_pan": 5.0}
    api, created = make_api(lerobot_mod, present)
    clock = FakeClock()
    from so101_companion.config import AppConfig

    cfg = AppConfig()
    c = ControllerCoordinator(
        cfg,
        build_bounds({n: (-100.0, 100.0) for n in JOINT_NAMES[:5]}, 5.0, "t", "T"),
        dict(present),
        lambda: adapter(api),
        clock=clock,
    )
    for req in (
        rq.Connect(CONTRACT_PORT, str(calibration_file), confirmed=True),
        rq.SetMode(Mode.SIM_TO_REAL),
        rq.EnableMovement(confirmed=True),
    ):
        c.submit(req)
        c.process_requests()
    assert c.snapshot().connection == Connection.ARMED
    c.set_requested({**present, "shoulder_pan": 80.0})
    for _ in range(3):
        clock.advance(0.05)
        c.tick()
    s = c.snapshot()
    # fixed policy: the 75° request is staged and sent unchanged in the first tick
    assert s.staged.values["shoulder_pan"] == 80.0
    assert s.sent.values["shoulder_pan"] == pytest.approx(80.0)
    assert s.actual.values["shoulder_pan"] == pytest.approx(5.0)  # fake bus does not move
    created[0].bus.fail_reads = True
    clock.advance(0.05)
    c.tick()
    assert c.snapshot().connection == Connection.FAULT_LATCHED

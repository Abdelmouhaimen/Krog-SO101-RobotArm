"""Shared test rig: a coordinator wired to a FakeRobot and a FakeClock (no threads, no devices)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from so101_companion.config import AppConfig
from so101_companion.control import requests as rq
from so101_companion.control.clock import FakeClock
from so101_companion.control.coordinator import ControllerCoordinator
from so101_companion.devices.fake import FakeRobot
from so101_companion.domain.joints import ARM_JOINTS
from so101_companion.domain.state import Mode
from so101_companion.safety.bounds import build_bounds

FAKE_PORT = "/dev/fake-so101"
SIM_INITIAL = {
    "shoulder_pan": 0.0,
    "shoulder_lift": 0.0,
    "elbow_flex": 0.0,
    "wrist_flex": 0.0,
    "wrist_roll": 0.0,
    "gripper": 50.0,
}
HW_POSE = {
    "shoulder_pan": 5.0,
    "shoulder_lift": -20.0,
    "elbow_flex": 30.0,
    "wrist_flex": 10.0,
    "wrist_roll": 0.0,
    "gripper": 0.0,
}


def sim_bounds(half_range: float = 100.0) -> Any:
    return build_bounds({n: (-half_range, half_range) for n in ARM_JOINTS}, 5.0, "test", "TEST_SIM")


@dataclass
class Rig:
    coordinator: ControllerCoordinator
    clock: FakeClock
    robots: list[FakeRobot] = field(default_factory=list)
    calibration: Path | None = None

    @property
    def robot(self) -> FakeRobot:
        return self.robots[-1]

    def run(self, *requests: rq.Request) -> None:
        for r in requests:
            self.coordinator.submit(r)
        self.coordinator.process_requests()

    def tick(self, n: int = 1, dt: float = 0.05) -> None:
        for _ in range(n):
            self.clock.advance(dt)
            self.coordinator.tick()

    def snap(self) -> Any:
        return self.coordinator.snapshot()

    def connect(self, port: str = FAKE_PORT) -> None:
        assert self.calibration is not None
        self.run(rq.Connect(port, str(self.calibration), confirmed=True))

    def arm(self) -> None:
        self.run(rq.SetMode(Mode.SIM_TO_REAL))
        self.tick()
        self.run(rq.EnableMovement(confirmed=True))

    def request(self, **values: float) -> None:
        snap = self.snap()
        base = dict(snap.requested.values) if snap.requested.values is not None else dict(SIM_INITIAL)
        base.update(values)
        self.coordinator.set_requested(base)


def make_rig(
    calibration: Path | None = None,
    *,
    robot_kwargs: dict[str, Any] | None = None,
    cfg: AppConfig | None = None,
) -> Rig:
    clock = FakeClock()
    cfg = cfg or AppConfig()
    rig_holder: dict[str, Rig] = {}

    def factory() -> FakeRobot:
        kwargs: dict[str, Any] = {
            "initial_pose": dict(HW_POSE),
            "clock": clock,
            "sleep": clock.sleep,
        }
        kwargs.update(robot_kwargs or {})
        robot = FakeRobot(**kwargs)
        rig_holder["rig"].robots.append(robot)
        return robot

    coordinator = ControllerCoordinator(cfg, sim_bounds(), SIM_INITIAL, factory, clock=clock)
    rig = Rig(coordinator, clock, calibration=calibration)
    rig_holder["rig"] = rig
    return rig

"""Shared rig for Follow Me / Talk Mode tests (ADR-019): the real controller with a FakeRobot and a fake
clock, the REAL FK direction analyzer on the official model, the behavior/pose service and the API
command layer. No threads, no devices."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from helpers import FAKE_PORT, HW_POSE, Rig, make_rig
from so101_companion.api.service import ApiCommands
from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.poses import PoseSource
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.service import BehaviorService
from so101_companion.config import AppConfig
from so101_companion.control import requests as rq
from so101_companion.control.front import FileFrontStore
from so101_companion.devices.rest_pose import FileRestPoseStore
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Connection, Mode
from so101_companion.simulation import model_assets
from so101_companion.simulation.direction import DirectionAnalyzer

PERIOD = 0.05
# Representative configurations (verified with FK on the official model):
NORMAL_POSE = dict(HW_POSE)  # gripper in front of the pan axis (psi ≈ 0°)
REVERSED_POSE = {  # the owner's folded-back style (psi ≈ 180°), recorded at pan −75° like their takes
    "shoulder_pan": -75.0,
    "shoulder_lift": -60.0,
    "elbow_flex": -51.0,
    "wrist_flex": -55.0,
    "wrist_roll": 0.0,
    "gripper": 3.0,
}


@functools.cache
def analyzer() -> DirectionAnalyzer:
    return DirectionAnalyzer(model_assets.default_scene_path(), AppConfig().simulation)


def fresh_analyzer() -> DirectionAnalyzer:
    return DirectionAnalyzer(model_assets.default_scene_path(), AppConfig().simulation)


class OverlayEnv:
    def __init__(self, calibration_file: Path, tmp_path: Path, *, connect: bool = True, **robot: Any) -> None:
        self.rig: Rig = make_rig(calibration_file, robot_kwargs=robot or None)
        self.c = self.rig.coordinator
        self.c.directions = fresh_analyzer()
        self.c.rest_store = FileRestPoseStore(tmp_path / "config")
        self.c.front_store = FileFrontStore(tmp_path / "config")
        self.calibration_file = calibration_file
        self.service = BehaviorService(
            self.c, BehaviorRepository(tmp_path / "behaviors"), directions=analyzer()
        )
        self.api = ApiCommands(self.c, self.service, allow_physical_control=True, ack_timeout_s=0.0)
        if connect:
            self.rig.run(rq.EnterMode(Mode.SIM_TO_REAL, FAKE_PORT, str(calibration_file), confirmed=True))
            self.rig.run(rq.RecordRestPose(confirmed=True))  # default rest pose = HW_POSE (NORMAL)
            self.fp = self.rig.snap().extra["calibration_fingerprint"]
        else:
            self.fp = None
        self.publish()

    # -- stepping -------------------------------------------------------------------------
    def publish(self) -> None:
        self.c.published.set(self.c.snapshot())

    def run(self, *reqs: rq.Request) -> None:
        self.rig.run(*reqs)
        self.publish()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.rig.tick()
            self.publish()

    def run_while(self, state: Connection, limit: int = 1200) -> None:
        for _ in range(limit):
            if self.rig.snap().connection != state:
                return
            self.tick()
        raise AssertionError(f"still {state}")

    def snap(self) -> Any:
        return self.rig.snap()

    def follow(self) -> dict[str, Any]:
        return dict(self.snap().extra["follow"])

    def talk(self) -> dict[str, Any]:
        return dict(self.snap().extra["talk"])

    def sends(self) -> list[dict[str, float]]:
        return [a for n, a in self.rig.robot.calls if n == "send"]

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.c.events.records(exclude_ticks=True) if e["kind"] == kind]

    # -- library --------------------------------------------------------------------------
    def behavior(
        self,
        name: str,
        base: dict[str, float],
        joint: str = "elbow_flex",
        *,
        reps: int | None = 1,
        after: str = "rest",
        pose_id: str | None = None,
        amplitude: float = 1.0,
        sweep: dict[str, tuple[float, float]] | None = None,
    ):  # type: ignore[no-untyped-def]
        samples = []
        for i in range(21):
            pose = base | {joint: base[joint] + amplitude * i}
            for j, (a, b) in (sweep or {}).items():
                pose[j] = a + (b - a) * i / 20.0
            samples.append(Sample(round(i * PERIOD, 6), actual=pose))
        b = self.service.repository.create(
            new_behavior(name, Origin.REAL_HAND_GUIDED, tuple(samples), calibration_fingerprint=self.fp)
        )
        return self.service.repository.set_settings(
            b, PlaybackSettings(repeat_count=reps, after_action=after, after_pose_id=pose_id)
        )

    def pose(self, name: str, values: dict[str, float]):  # type: ignore[no-untyped-def]
        return self.service.poses.create(name, values, PoseSource.REAL_ARM, calibration_fingerprint=self.fp)

    def play(self, b: Any) -> None:
        from so101_companion.safety.policy import TransitionProfile

        self.service.play(
            b, b.settings, None, confirmed=True, enable_torque=False, entry_profile=TransitionProfile.FAST
        )
        self.c.process_requests()
        self.publish()


def pan_of(sends: list[dict[str, float]]) -> list[float]:
    return [s["shoulder_pan"] for s in sends]


def without(joints: set[str], sends: list[dict[str, float]]) -> list[tuple[float, ...]]:
    return [tuple(round(s[j], 9) for j in JOINT_NAMES if j not in joints) for s in sends]

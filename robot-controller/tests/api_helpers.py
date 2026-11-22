"""A LIVE stack for HTTP API tests: the real controller thread (ControllerWorker, real time, 20 Hz) driving a
FakeRobot (no device), the behavior/pose libraries, the API command layer and the FastAPI app.

Every robot call records the calling thread, so tests can prove that only the controller thread ever
touches the (fake) bus.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers import FAKE_PORT, HW_POSE, SIM_INITIAL, sim_bounds
from overlay_helpers import analyzer, fresh_analyzer
from so101_companion.api.auth import TokenVerifier, token_hash
from so101_companion.api.server import create_app
from so101_companion.api.service import ApiCommands
from so101_companion.api.video import SimulationVideo
from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.poses import PoseSource
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.service import BehaviorService
from so101_companion.config import AppConfig
from so101_companion.control import requests as rq
from so101_companion.control.coordinator import ControllerCoordinator
from so101_companion.control.worker import ControllerWorker
from so101_companion.devices.fake import FakeRobot
from so101_companion.devices.rest_pose import FileRestPoseStore
from so101_companion.domain.state import Connection, Mode

TOKEN = "so101_test-token-for-the-fake-stack"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class ThreadRecordingRobot(FakeRobot):
    def send(self, targets: Any) -> Any:
        self.bus_threads.add(threading.current_thread().name)
        return super().send(targets)

    def observe(self) -> Any:
        self.bus_threads.add(threading.current_thread().name)
        return super().observe()

    def __post_init__(self) -> None:
        super().__post_init__()
        self.bus_threads: set[str] = set()


def wait_until(pred: Callable[[], bool], timeout: float = 10.0, every: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(every)
    return False


class LiveStack:
    def __init__(
        self,
        tmp_path: Path,
        calibration_file: Path,
        *,
        physical: bool = True,
        video: SimulationVideo | None = None,
        ack_timeout_s: float = 0.5,
        arm: bool = True,
        directions: bool = True,
    ) -> None:
        self.robots: list[ThreadRecordingRobot] = []

        def factory() -> ThreadRecordingRobot:
            r = ThreadRecordingRobot(initial_pose=dict(HW_POSE), follow=0.6)
            self.robots.append(r)
            return r

        self.c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, factory)
        self.c.rest_store = FileRestPoseStore(tmp_path / "config")
        if directions:  # Follow Me metadata (ADR-019): the controller's own FK analyzer
            self.c.directions = fresh_analyzer()
        self.worker = ControllerWorker(self.c, lambda _s: None, 20.0)
        self.worker.start()
        if arm:
            self.c.submit(rq.EnterMode(Mode.SIM_TO_REAL, FAKE_PORT, str(calibration_file), confirmed=True))
            self.c.submit(rq.RecordRestPose(confirmed=True))
            assert wait_until(
                lambda: (
                    (s := self.c.published.get()) is not None
                    and s.connection == Connection.ARMED
                    and s.extra.get("rest_pose") is not None
                )
            )
        else:
            assert wait_until(lambda: self.c.published.get() is not None)
        self.service = BehaviorService(
            self.c,
            BehaviorRepository(tmp_path / "behaviors"),
            directions=analyzer() if directions else None,
        )
        self.commands = ApiCommands(
            self.c, self.service, allow_physical_control=physical, ack_timeout_s=ack_timeout_s
        )
        self.video = video
        self.app = create_app(self.commands, TokenVerifier(hashes=[token_hash(TOKEN)]), video)
        self.client = TestClient(self.app)

    @property
    def robot(self) -> ThreadRecordingRobot:
        return self.robots[-1]

    def snap(self) -> Any:
        return self.c.published.get()

    def fp(self) -> str:
        fp: str = self.snap().extra["calibration_fingerprint"]
        return fp

    def behavior(
        self, name: str = "talk", joint: str = "elbow_flex", reps: int | None = 1, after: str = "rest"
    ):  # type: ignore[no-untyped-def]
        samples = tuple(
            Sample(round(i * 0.05, 6), actual=HW_POSE | {joint: HW_POSE[joint] + 1.0 * i}) for i in range(21)
        )
        b = self.service.repository.create(
            new_behavior(name, Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=self.fp())
        )
        return self.service.repository.set_settings(
            b, PlaybackSettings(repeat_count=reps, after_action=after)
        )

    def pose(self, name: str = "Watching", pan: float = 30.0):  # type: ignore[no-untyped-def]
        values = HW_POSE | {"shoulder_pan": HW_POSE["shoulder_pan"] + pan, "wrist_flex": 25.0}
        return self.service.poses.create(name, values, PoseSource.REAL_ARM, calibration_fingerprint=self.fp())

    def wait_command(self, cid: str, states: tuple[str, ...], timeout: float = 15.0) -> dict[str, Any]:
        rec: dict[str, Any] = {}

        def done() -> bool:
            nonlocal rec
            rec = self.client.get(f"/api/v1/commands/{cid}", headers=AUTH).json()
            return rec["state"] in states

        assert wait_until(done, timeout, 0.05), rec
        return rec

    def close(self) -> None:
        self.client.close()
        if self.video is not None:
            self.video.close()
        self.worker.stop()

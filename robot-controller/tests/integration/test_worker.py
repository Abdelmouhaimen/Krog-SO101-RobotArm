"""T-013 / UX-03 / UX-04 / R08: real-thread scheduling, coalescing, overrun handling (fake robot)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from helpers import HW_POSE, SIM_INITIAL, sim_bounds
from so101_companion.config import AppConfig
from so101_companion.control import requests as rq
from so101_companion.control.coordinator import ControllerCoordinator
from so101_companion.control.worker import ControllerWorker
from so101_companion.devices.fake import FakeRobot, FaultPlan
from so101_companion.domain.state import Connection, Mode, Snapshot


def start(coordinator: ControllerCoordinator, hz: float = 20.0) -> tuple[ControllerWorker, list[Snapshot]]:
    snaps: list[Snapshot] = []
    w = ControllerWorker(coordinator, snaps.append, hz)
    w.start()
    return w, snaps


def wait_for(pred, timeout: float = 3.0) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_nominal_20hz_and_measured_rate() -> None:
    c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, FakeRobot)
    w, _ = start(c)
    try:
        time.sleep(1.05)
        ticks = c.tick_seq
        hz = c.tick_hz()
    finally:
        w.stop()
    assert 17 <= ticks <= 23, ticks
    assert 17.0 <= hz <= 22.0, hz


def test_requests_are_handled_between_ticks_quickly() -> None:
    c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, FakeRobot)
    w, _ = start(c, hz=2.0)  # long period: the request must not wait for a tick
    try:
        t0 = time.monotonic()
        c.submit(rq.Pause())
        assert wait_for(lambda: c.paused, 1.0)
        assert time.monotonic() - t0 < 0.2
    finally:
        w.stop()


def test_slow_serial_skips_deadlines_without_catch_up(calibration_file: Path) -> None:
    robot_holder: list[FakeRobot] = []

    def factory() -> FakeRobot:
        r = FakeRobot(
            initial_pose=dict(HW_POSE),
            follow=1.0,
            faults=FaultPlan(write_latency_s=0.2),
        )
        robot_holder.append(r)
        return r

    c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, factory)
    w, _ = start(c)
    try:
        c.submit(rq.Connect("/dev/fake-so101", str(calibration_file), confirmed=True))
        c.submit(rq.SetMode(Mode.SIM_TO_REAL))
        c.submit(rq.EnableMovement(confirmed=True))
        assert wait_for(lambda: c.connection == Connection.ARMED)
        c.set_requested({**HW_POSE, "elbow_flex": 120.0})
        time.sleep(1.0)
    finally:
        w.stop()
    sends = [call[1]["elbow_flex"] for call in robot_holder[0].calls if call[0] == "send"]
    assert 3 <= len(sends) <= 7  # ~5 issued ticks in 1 s with 200 ms writes, not 20: no backlog
    upper = c.hw_bounds.limits["elbow_flex"].upper
    assert all(v in (HW_POSE["elbow_flex"], upper) for v in sends)  # only current targets, never queued ones
    assert sends[-1] == upper  # clamped to the calibrated bound, forwarded directly
    assert c.overruns > 0
    assert c.tick_hz() < 10.0  # achieved rate is reported honestly


def test_coalescing_under_fast_ui_updates() -> None:
    c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, FakeRobot)
    w, _ = start(c)
    stop = threading.Event()

    def spam() -> None:
        v = 0.0
        while not stop.is_set():
            v = (v + 0.5) % 90
            c.set_requested({**SIM_INITIAL, "elbow_flex": v})

    t = threading.Thread(target=spam)
    t.start()
    try:
        time.sleep(0.5)
    finally:
        stop.set()
        t.join()
        w.stop()
    assert c.tick_seq <= 13  # thousands of UI updates, ~10 ticks: no backlog


def test_worker_stop_is_prompt() -> None:
    c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, FakeRobot)
    w, _ = start(c, hz=1.0)
    t0 = time.monotonic()
    w.stop()
    assert not w.is_alive() and time.monotonic() - t0 < 0.5


@pytest.mark.parametrize("hz", [20.0])
def test_snapshots_are_published_per_tick(hz: float) -> None:
    c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, FakeRobot)
    w, snaps = start(c, hz)
    try:
        time.sleep(0.3)
    finally:
        w.stop()
    assert len(snaps) >= 4
    assert all(isinstance(s, Snapshot) for s in snaps)
    seqs = [s.seq for s in snaps]
    assert seqs == sorted(seqs)


def test_behavior_playback_with_real_thread_and_stall(calibration_file: Path, tmp_path: Path) -> None:
    """M2 (ADR-013): wall-clock run through the real worker; a 350 ms write stall delays the timeline
    (reported), the behavior completes, returns to rest and holds. One send per issued tick."""
    from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
    from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
    from so101_companion.behaviors.service import BehaviorService

    robot = FakeRobot(initial_pose=dict(HW_POSE))
    c = ControllerCoordinator(AppConfig(), sim_bounds(), SIM_INITIAL, lambda: robot)
    service = BehaviorService(c, BehaviorRepository(tmp_path))
    w, _ = start(c)
    try:
        c.submit(rq.Connect("/dev/fake", str(calibration_file), confirmed=True))
        c.submit(rq.RecordRestPose(confirmed=True))
        c.submit(rq.SetMode(Mode.SIM_TO_REAL))
        assert wait_for(lambda: c.rest_pose is not None and c.mode == Mode.SIM_TO_REAL)
        time.sleep(0.15)
        fp = c.snapshot().extra["calibration_fingerprint"]
        samples = tuple(
            Sample(round(i * 0.05, 6), actual={**HW_POSE, "elbow_flex": HW_POSE["elbow_flex"] + 0.5 * i})
            for i in range(11)
        )
        b = new_behavior("w", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=fp)
        prepared = service.prepare_preview(b, PlaybackSettings(), c.snapshot())
        assert prepared.provisional_reason is None
        prepared.session.step(prepared.session.duration + 1)
        service.play(b, PlaybackSettings(), prepared.session.approval(), confirmed=True, enable_torque=False)
        assert wait_for(lambda: c.connection == Connection.PLAYING_BEHAVIOR)
        robot.faults.write_latency_s = 0.35
        time.sleep(0.1)
        robot.faults.write_latency_s = 0.0
        assert wait_for(lambda: c.connection == Connection.HOLDING, timeout=10.0)
    finally:
        w.stop()
    done = c.events.last("behavior_completed")
    assert done is not None and done["timeline_delay_ms"] >= 300.0
    assert c.events.last("rest_reached") is not None

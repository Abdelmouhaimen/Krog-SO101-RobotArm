"""ADR-018 on-demand simulation video: 15 (no frames when inactive), 16 (≤5 FPS), 17 (the last client's
disconnect releases everything), 18 (streaming does not slow the control loop).

Most tests use a counting fake renderer (no GL). The ``render``-marked test uses the real MuJoCo model
and EGL offscreen renderer through the same producer.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from api_helpers import AUTH, LiveStack, wait_until
from so101_companion.api.runner import ApiServer
from so101_companion.api.video import MAX_STREAM_FPS, SimulationVideo
from so101_companion.config import ApiSection
from so101_companion.domain.state import Connection

POSE = {
    "shoulder_pan": 0.0,
    "shoulder_lift": 0.0,
    "elbow_flex": 0.0,
    "wrist_flex": 0.0,
    "wrist_roll": 0.0,
    "gripper": 50.0,
}


class CountingRenderer:
    created = 0
    closed = 0
    threads: set[str] = set()  # noqa: RUF012 - shared test counter

    def __init__(self, work_s: float = 0.0) -> None:
        type(self).created += 1
        self.work_s = work_s
        self.frames = 0

    def render(self, pose: Mapping[str, float]) -> np.ndarray:
        type(self).threads.add(threading.current_thread().name)
        if self.work_s:
            time.sleep(self.work_s)
        self.frames += 1
        img = np.zeros((120, 160, 3), dtype=np.uint8)
        img[:, : min(159, self.frames)] = 200
        return img

    def close(self) -> None:
        type(self).closed += 1


@pytest.fixture(autouse=True)
def _reset() -> None:
    CountingRenderer.created = CountingRenderer.closed = 0
    CountingRenderer.threads = set()


def make_video(work_s: float = 0.0, **kw: Any) -> SimulationVideo:
    return SimulationVideo(lambda: CountingRenderer(work_s), lambda: POSE, **kw)


def test_15_no_frames_and_no_thread_without_subscribers() -> None:
    v = make_video()
    time.sleep(0.4)
    assert v.frames_produced == 0 and not v.running and v.threads_started == 0
    assert CountingRenderer.created == 0  # no renderer / GL context exists while nobody watches


def test_16_stream_is_capped_at_5_fps_and_serves_the_newest_frame() -> None:
    v = make_video(max_fps=30.0)  # a higher request is clamped to the hard cap
    assert v.period == pytest.approx(1.0 / MAX_STREAM_FPS)
    v.subscribe()
    try:
        got, last, t0 = [], 0, time.monotonic()
        while time.monotonic() - t0 < 2.0:
            f = v.wait_frame(last, 1.0)
            assert f is not None
            got.append(f)
            last = f.seq
        span = got[-1].t_mono - got[0].t_mono
        fps = (len(got) - 1) / span
        assert fps <= MAX_STREAM_FPS + 0.3, fps
        assert all(b.t_mono - a.t_mono >= 0.18 for a, b in itertools.pairwise(got))
        assert got[0].jpeg[:2] == b"\xff\xd8"  # JPEG
        time.sleep(0.9)  # a slow client skips frames: it gets the NEWEST one, never a backlog
        newest = v.wait_frame(last, 1.0)
        assert newest is not None and newest.seq >= last + 3
    finally:
        v.unsubscribe()
        v.close()


def test_17_last_unsubscribe_releases_the_producer_and_renderer() -> None:
    v = make_video()
    v.subscribe()
    v.subscribe()  # two clients share one producer
    assert wait_until(lambda: v.frames_produced > 0, 3.0)
    assert v.threads_started == 1 and CountingRenderer.created == 1
    v.unsubscribe()
    time.sleep(0.4)
    assert v.running  # one client left
    v.unsubscribe()
    assert wait_until(lambda: not v.running, 2.0)
    assert CountingRenderer.closed == 1 and v.renderers_open == 0
    n = v.frames_produced
    time.sleep(0.5)
    assert v.frames_produced == n  # nothing renders after the last client left
    v.subscribe()  # a new client starts a fresh producer
    assert wait_until(lambda: v.frames_produced > n, 3.0) and v.threads_started == 2
    v.unsubscribe()
    assert wait_until(lambda: not v.running, 2.0) and CountingRenderer.closed == 2


def test_17_mjpeg_over_real_http_and_disconnect_releases(tmp_path: Path, calibration_file: Path) -> None:
    video = make_video()
    stack = LiveStack(tmp_path, calibration_file, video=video)
    server = ApiServer(stack.app, ApiSection(port=0))
    server.start()
    try:
        assert (
            stack.client.get("/api/v1/simulation/frame", headers=AUTH).headers["content-type"] == "image/jpeg"
        )
        assert wait_until(lambda: not video.running, 2.0)  # the single frame did not leave it running
        with (
            httpx.Client(base_url=server.url, headers=AUTH, timeout=10.0) as c,
            c.stream("GET", "/api/v1/simulation/stream.mjpeg") as r,
        ):
            assert r.status_code == 200 and "multipart/x-mixed-replace" in r.headers["content-type"]
            buf, t0 = b"", time.monotonic()
            for chunk in r.iter_bytes():
                buf += chunk
                if buf.count(b"\xff\xd8") >= 3 or time.monotonic() - t0 > 5:
                    break
            assert buf.count(b"--so101frame") >= 3
            assert video.clients == 1 and video.running
        # the client closed the connection: the producer stops and the renderer is closed
        assert wait_until(lambda: video.clients == 0 and not video.running, 5.0)
        assert CountingRenderer.closed == CountingRenderer.created
    finally:
        server.stop()
        stack.close()


def test_18_streaming_does_not_slow_the_control_loop(tmp_path: Path, calibration_file: Path) -> None:
    video = make_video(work_s=0.12)  # a slow renderer (120 ms per frame), on its own thread
    stack = LiveStack(tmp_path, calibration_file, video=video)
    try:
        loop = stack.behavior(reps=None)
        assert stack.client.post(f"/api/v1/behaviors/{loop.id}/play", headers=AUTH).status_code == 202

        def measure(seconds: float) -> tuple[int, int]:
            seq0, over0 = stack.c.tick_seq, stack.c.overruns
            time.sleep(seconds)
            return stack.c.tick_seq - seq0, stack.c.overruns - over0

        base_ticks, base_over = measure(1.5)
        video.subscribe()
        try:
            stream_ticks, stream_over = measure(1.5)
            assert video.frames_produced >= 4
        finally:
            video.unsubscribe()
        assert stack.snap().connection == Connection.PLAYING_BEHAVIOR
        assert stream_ticks >= 0.9 * base_ticks and stream_ticks >= 27  # ~20 Hz either way
        assert stream_over <= base_over + 1
        assert CountingRenderer.threads == {"so101-sim-video"}  # never on the controller thread
    finally:
        stack.close()


@pytest.mark.render
def test_real_mujoco_frames_follow_the_published_pose(tmp_path: Path, calibration_file: Path) -> None:
    """Optional (-m render): the real model + EGL renderer through the same on-demand producer."""
    from so101_companion.api.video import MujocoFrameRenderer
    from so101_companion.config import AppConfig
    from so101_companion.simulation import model_assets
    from so101_companion.simulation.mapping import mapping_from_config
    from so101_companion.simulation.mujoco_pose import PoseScene

    scene_path = model_assets.default_scene_path()
    mapping = mapping_from_config(
        PoseScene.from_scene_path(scene_path).model_ranges_rad(), AppConfig().simulation
    )
    pose = dict(POSE)
    video = SimulationVideo(lambda: MujocoFrameRenderer(scene_path, mapping, 320, 240), lambda: pose)
    video.subscribe()
    try:
        a = video.wait_frame(0, 15.0)
        assert a is not None and a.jpeg[:2] == b"\xff\xd8"
        pose["shoulder_pan"] = 60.0
        pose["elbow_flex"] = -40.0
        b = video.wait_frame(a.seq + 1, 5.0)
        assert b is not None and b.jpeg != a.jpeg
    finally:
        video.unsubscribe()
    assert wait_until(lambda: not video.running, 5.0) and video.renderers_open == 0


def test_example_client_against_a_live_server(tmp_path: Path, calibration_file: Path) -> None:
    """examples/api_client.py (the future Milestone 3 starting point) works against the real server."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "api_client", Path(__file__).resolve().parents[2] / "examples" / "api_client.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stack = LiveStack(tmp_path, calibration_file, video=make_video())
    server = ApiServer(stack.app, ApiSection(port=0))
    server.start()
    try:
        loop, pose = stack.behavior(reps=None), stack.pose()
        c = mod.SO101Client(server.url, token=AUTH["Authorization"].split()[1])
        assert c.status()["ready"]["play_behavior"]["ok"]
        assert [b["id"] for b in c.behaviors()] == [loop.id] and c.poses()[0]["id"] == pose.id
        first = c.play(loop.id, repeat="until_stopped", getting_into_position="fast")
        assert first["state"] == "running"
        time.sleep(0.5)
        second = c.go_pose(pose.id, "fast")
        assert second["replaced"] == first["id"] and c.command(first["id"])["state"] == "superseded"
        assert c.wait(second["id"], timeout=10)["state"] == "completed"
        assert c.frame()[:2] == b"\xff\xd8"
        with pytest.raises(mod.ApiError) as err:
            c.pause()  # nothing to pause: the controller refuses, the client sees 409 + reason
        assert err.value.status == 409 and err.value.body["detail"]
        bad = mod.SO101Client(server.url, token="wrong")
        with pytest.raises(mod.ApiError) as err:
            bad.status()
        assert err.value.status == 401
    finally:
        server.stop()
        stack.close()

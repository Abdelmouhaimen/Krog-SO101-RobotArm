"""On-demand simulation video for the HTTP API (ADR-018).

``SimulationVideo`` renders the MuJoCo visualization of the controller's latest PUBLISHED displayed pose
into a single newest-frame slot, on its own producer thread:

* No subscriber -> no thread, no renderer, no frames (nothing runs in the background).
* The first subscriber starts the thread. The thread builds its OWN scene and offscreen GL renderer
  (a GL context belongs to the thread that created it; the Studio's renderer stays on the Qt thread).
* At most ``max_fps`` frames per second (hard cap 5). Each client waits for a frame NEWER than the
  last one it got, so a slow client skips frames: there is no queue and no growing delay.
* The last unsubscribe stops the thread, which closes the renderer and frees the GL context.

Nothing here touches the controller except reading the published snapshot, so rendering and JPEG
encoding never sit on the control path. The source is a small protocol (``subscribe`` / ``wait_frame``
/ ``unsubscribe``), so an RTSP adapter can be added later as one more subscriber.
"""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

MAX_STREAM_FPS = 5.0


class FrameRenderer(Protocol):
    def render(self, pose: Mapping[str, float]) -> np.ndarray: ...  # RGB uint8 (H, W, 3)

    def close(self) -> None: ...


RendererFactory = Callable[[], FrameRenderer]
PoseSource = Callable[[], Mapping[str, float] | None]


@dataclass(frozen=True)
class Frame:
    seq: int
    jpeg: bytes
    t_mono: float
    render_ms: float


class VideoUnavailable(RuntimeError):
    pass


def encode_jpeg(rgb: np.ndarray, quality: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb[:, :, :3])).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class SimulationVideo:
    def __init__(
        self,
        renderer_factory: RendererFactory,
        pose_source: PoseSource,
        *,
        max_fps: float = MAX_STREAM_FPS,
        jpeg_quality: int = 80,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = renderer_factory
        self._pose_source = pose_source
        self.period = 1.0 / min(max_fps, MAX_STREAM_FPS)
        self.jpeg_quality = jpeg_quality
        self.clock = clock
        self._cond = threading.Condition()
        self._clients = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame: Frame | None = None
        self._seq = 0
        self.error: str | None = None
        self.frames_produced = 0
        self.threads_started = 0
        self.renderers_open = 0

    # ------------------------------------------------------------------ subscribers (any thread)
    @property
    def clients(self) -> int:
        with self._cond:
            return self._clients

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def subscribe(self) -> None:
        with self._cond:
            self._clients += 1
            if self._thread is None or not self._thread.is_alive() or self._stop.is_set():
                # a previous producer that is still shutting down has already been told to stop;
                # wait for it so two GL renderers never coexist for this source
                old = self._thread
                if old is not None:
                    self._cond.release()
                    try:
                        old.join(timeout=5.0)
                    finally:
                        self._cond.acquire()
                self._stop = threading.Event()
                self._frame = None  # never serve a frame from an earlier session
                self.error = None
                self._thread = threading.Thread(
                    target=self._run, args=(self._stop,), name="so101-sim-video", daemon=True
                )
                self.threads_started += 1
                self._thread.start()

    def unsubscribe(self) -> None:
        with self._cond:
            self._clients = max(0, self._clients - 1)
            if self._clients == 0:
                self._stop.set()  # the producer closes its renderer and exits
                self._cond.notify_all()

    def wait_frame(self, after_seq: int, timeout: float) -> Frame | None:
        """The newest frame with ``seq > after_seq`` (blocks up to ``timeout``), or None."""
        deadline = self.clock() + timeout
        with self._cond:
            while True:
                if self.error is not None:
                    raise VideoUnavailable(self.error)
                f = self._frame
                if f is not None and f.seq > after_seq:
                    return f
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def close(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._clients = 0
            self._stop.set()
            self._cond.notify_all()
            t = self._thread
        if t is not None:
            t.join(timeout)

    def stats(self) -> dict[str, Any]:
        return {
            "clients": self.clients,
            "running": self.running,
            "frames_produced": self.frames_produced,
            "max_fps": round(1.0 / self.period, 3),
            "error": self.error,
        }

    # ------------------------------------------------------------------ producer thread
    def _run(self, stop: threading.Event) -> None:
        renderer: FrameRenderer | None = None
        try:
            renderer = self._factory()
            self.renderers_open += 1
            while not stop.is_set():
                pose = self._pose_source()
                if pose is not None:
                    t0 = time.perf_counter()
                    rgb = renderer.render(pose)
                    jpeg = encode_jpeg(rgb, self.jpeg_quality)
                    ms = (time.perf_counter() - t0) * 1000.0
                    with self._cond:
                        self._seq += 1
                        self._frame = Frame(self._seq, jpeg, self.clock(), round(ms, 2))
                        self.frames_produced += 1
                        self._cond.notify_all()
                # a full period AFTER each frame: published frames are always >= 1/max_fps apart, and an
                # overrunning render never causes a catch-up burst
                stop.wait(self.period)
        except Exception as exc:
            with self._cond:
                self.error = f"simulation video unavailable: {type(exc).__name__}: {exc}"
                self._cond.notify_all()
        finally:
            if renderer is not None:
                try:
                    renderer.close()
                finally:
                    self.renderers_open -= 1


class MujocoFrameRenderer:
    """Renders a LOGICAL pose with the Studio's model, mapping and camera framing (its own scene and GL
    context; create it on the thread that renders)."""

    def __init__(self, scene_path: Any, mapping: Any, width: int, height: int) -> None:
        from so101_companion.simulation.mujoco_pose import PoseScene
        from so101_companion.simulation.render import OffscreenRenderer

        self.scene = PoseScene.from_scene_path(scene_path)
        self.mapping = mapping
        self.renderer = OffscreenRenderer(self.scene.model, width=width, height=height)
        cam = self.renderer.camera
        cam.distance = float(cam.distance) * 1.3  # same studio framing as the window viewport

    def render(self, pose: Mapping[str, float]) -> np.ndarray:
        self.scene.set_logical_pose(pose, self.mapping)
        return self.renderer.render(self.scene.data)

    def close(self) -> None:
        self.renderer.close()

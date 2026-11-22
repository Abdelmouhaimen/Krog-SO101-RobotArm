"""Embedded 3D viewport: paints offscreen MuJoCo frames inside the main window.

Rendering happens on the Qt GUI thread from a QTimer (ADR-004 preferred approach). A renderer
failure is shown as an explicit error INSIDE this widget; there is no fallback to a detached viewer.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget


class CameraLike(Protocol):
    azimuth: float
    elevation: float
    distance: float


class FrameSource(Protocol):
    camera: CameraLike

    def render(self, data: Any) -> np.ndarray: ...
    def info(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


RendererFactory = Callable[[], FrameSource]


class RenderStats:
    def __init__(self, window_s: float = 2.0) -> None:
        self.window_s = window_s
        self.frame_times: deque[float] = deque()
        self.durations_ms: deque[float] = deque(maxlen=600)
        self.frames_total = 0

    def add(self, t: float, duration_ms: float) -> None:
        self.frames_total += 1
        self.frame_times.append(t)
        self.durations_ms.append(duration_ms)
        while self.frame_times and t - self.frame_times[0] > self.window_s:
            self.frame_times.popleft()

    def fps(self) -> float:
        if len(self.frame_times) < 2:
            return 0.0
        span = self.frame_times[-1] - self.frame_times[0]
        return (len(self.frame_times) - 1) / span if span > 0 else 0.0

    def summary(self) -> dict[str, float]:
        d = np.array(self.durations_ms) if self.durations_ms else np.zeros(1)
        return {
            "fps": round(self.fps(), 2),
            "render_ms_p50": round(float(np.percentile(d, 50)), 2),
            "render_ms_p95": round(float(np.percentile(d, 95)), 2),
            "frames_total": float(self.frames_total),
        }


class MujocoViewport(QWidget):
    frame_rendered = Signal()
    render_failed = Signal(str)

    OVERLAY = (
        "3D view · kinematic pose display (not a physics or torque simulation) · drag to orbit, wheel to zoom"
    )

    def __init__(
        self,
        data: Any,
        renderer_factory: RendererFactory,
        target_fps: float = 30.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("mujocoViewport")
        self.setMinimumSize(480, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._data = data
        self._image: QImage | None = None
        self._last_frame: np.ndarray | None = None
        self._drag_pos: QPointF | None = None
        self.error: str | None = None
        self.badge: tuple[str, str] | None = None  # (text, color) drawn as a pill, top-left
        self.stats = RenderStats()
        self.renderer: FrameSource | None = None
        try:
            self.renderer = renderer_factory()
        except Exception as exc:
            self._set_error(f"{type(exc).__name__}: {exc}")
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(max(1, round(1000.0 / target_fps)))
        self._timer.timeout.connect(self.render_once)
        if self.renderer is not None:
            self._timer.start()

    # -- rendering -------------------------------------------------------------------------
    def render_once(self) -> bool:
        if self.renderer is None or self.error is not None:
            return False
        t0 = time.perf_counter()
        try:
            frame = self.renderer.render(self._data)
        except Exception as exc:
            self._set_error(f"{type(exc).__name__}: {exc}")
            return False
        t1 = time.perf_counter()
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            self._set_error(f"Renderer returned unexpected frame {frame.shape} {frame.dtype}")
            return False
        frame = np.ascontiguousarray(frame)
        h, w, _ = frame.shape
        self._image = QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self._last_frame = frame
        self.stats.add(t1, (t1 - t0) * 1000.0)
        self.update()
        self.frame_rendered.emit()
        return True

    def _set_error(self, message: str) -> None:
        self.error = message
        if hasattr(self, "_timer"):
            self._timer.stop()
        self.update()
        self.render_failed.emit(message)

    def set_badge(self, text: str | None, color: str = "#22c55e") -> None:
        new = None if text is None else (text, color)
        if new != self.badge:
            self.badge = new
            self.update()

    def last_frame(self) -> np.ndarray | None:
        return self._last_frame

    def last_image(self) -> QImage | None:
        return self._image

    def renderer_info(self) -> dict[str, Any]:
        return self.renderer.info() if self.renderer is not None else {}

    def shutdown(self) -> None:
        self._timer.stop()
        if self.renderer is not None:
            self.renderer.close()
            self.renderer = None

    # -- painting --------------------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(18, 22, 28))
        if self.error is not None:
            painter.setPen(QColor(255, 110, 110))
            painter.drawText(
                self.rect().adjusted(16, 16, -16, -16),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "3D RENDER BACKEND FAILED — no detached viewer will be opened.\n\n"
                f"{self.error}\n\nTry another backend, e.g. --gl-backend glfw, and see "
                "milestone-1/runbooks/UBUNTU_SETUP.md.",
            )
            painter.end()
            return
        if self._image is not None:
            img_w, img_h = self._image.width(), self._image.height()
            scale = max(self.width() / img_w, self.height() / img_h)  # fill the viewport (crop edges)
            tw, th = img_w * scale, img_h * scale
            target = QRectF((self.width() - tw) / 2, (self.height() - th) / 2, tw, th)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(target, self._image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.badge is not None:
            text, color = self.badge
            font = painter.font()
            font.setPointSizeF(max(9.0, font.pointSizeF() + 1.5))
            font.setBold(True)
            painter.setFont(font)
            fm = painter.fontMetrics()
            pill = QRectF(16, 16, fm.horizontalAdvance(text) + 32, fm.height() + 16)
            painter.setPen(QColor(color))
            painter.setBrush(QColor(10, 18, 24, 215))
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            painter.setPen(QColor(235, 240, 245))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
            font.setBold(False)
            font.setPointSizeF(max(8.0, font.pointSizeF() - 2.5))
            painter.setFont(font)
        painter.setPen(QColor(140, 152, 165))
        painter.drawText(14, self.height() - 12, self.OVERLAY)
        painter.end()

    # -- camera interaction (view only; never touches robot state) -------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.position()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is None or self.renderer is None:
            return
        delta = event.position() - self._drag_pos
        self._drag_pos = event.position()
        cam = self.renderer.camera
        cam.azimuth -= 0.3 * delta.x()
        cam.elevation = float(np.clip(cam.elevation - 0.3 * delta.y(), -89.0, 89.0))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self.renderer is None:
            return
        steps = event.angleDelta().y() / 120.0
        cam = self.renderer.camera
        cam.distance = float(np.clip(cam.distance * (0.9**steps), 0.05, 20.0))

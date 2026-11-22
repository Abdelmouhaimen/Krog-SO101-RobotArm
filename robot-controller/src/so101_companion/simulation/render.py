"""Offscreen MuJoCo rendering to RGB arrays, for display INSIDE the single Qt window.

The GL context is created by ``mujoco.Renderer`` on the constructing thread and must only be
used from that thread (the Qt GUI thread in this app). MuJoCo's native detached viewer window is
never used (single-window contract, ADR-004/ADR-005).
"""

from __future__ import annotations

import os
import threading
from typing import Any

import mujoco
import numpy as np

DEFAULT_SIZE = (640, 480)  # equals the model's default offscreen buffer; no in-memory change needed


class RenderBackendError(RuntimeError):
    pass


class OffscreenRenderer:
    def __init__(self, model: mujoco.MjModel, width: int = DEFAULT_SIZE[0], height: int = DEFAULT_SIZE[1]):
        self.model = model
        self.width = width
        self.height = height
        self.backend = os.environ.get("MUJOCO_GL", "<unset>")
        self._thread = threading.get_ident()
        vis = model.vis.global_
        if width > vis.offwidth or height > vis.offheight:
            # In-memory visual buffer size only; the official XML files stay byte-identical (ADR-005).
            vis.offwidth = max(vis.offwidth, width)
            vis.offheight = max(vis.offheight, height)
        try:
            self._renderer = mujoco.Renderer(model, height=height, width=width)
        except Exception as exc:
            raise RenderBackendError(
                f"MuJoCo offscreen renderer could not start with MUJOCO_GL={self.backend}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self.camera = mujoco.MjvCamera()
        mujoco.mjv_defaultFreeCamera(model, self.camera)
        # Use the official scene's own <global azimuth/elevation> viewpoint.
        self.camera.azimuth = float(vis.azimuth)
        self.camera.elevation = float(vis.elevation)
        self._gl_info: dict[str, str] | None = None

    def _check_thread(self) -> None:
        if threading.get_ident() != self._thread:
            raise RenderBackendError("OffscreenRenderer used from a thread that does not own its GL context")

    def render(self, data: mujoco.MjData) -> np.ndarray:
        self._check_thread()
        try:
            self._renderer.update_scene(data, camera=self.camera)
            frame: np.ndarray = self._renderer.render()
        except Exception as exc:
            raise RenderBackendError(f"MuJoCo render failed: {type(exc).__name__}: {exc}") from exc
        if self._gl_info is None:
            self._gl_info = _query_gl_info()
        return frame

    def info(self) -> dict[str, Any]:
        return {
            "mujoco_gl": self.backend,
            "mujoco_version": mujoco.__version__,
            "render_size": f"{self.width}x{self.height}",
            **(self._gl_info or {}),
        }

    def close(self) -> None:
        renderer = getattr(self, "_renderer", None)
        if renderer is not None:
            renderer.close()
            self._renderer = None


def _query_gl_info() -> dict[str, str]:
    """Best effort GL vendor/renderer strings while MuJoCo's context is current."""
    try:
        from OpenGL import GL

        return {
            "gl_vendor": GL.glGetString(GL.GL_VENDOR).decode(errors="replace"),
            "gl_renderer": GL.glGetString(GL.GL_RENDERER).decode(errors="replace"),
            "gl_version": GL.glGetString(GL.GL_VERSION).decode(errors="replace"),
        }
    except Exception as exc:
        return {"gl_renderer": f"unavailable ({type(exc).__name__})"}

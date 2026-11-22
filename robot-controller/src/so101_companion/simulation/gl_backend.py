"""Explicit selection of MuJoCo's offscreen OpenGL backend.

MuJoCo's Python bindings read ``MUJOCO_GL`` when ``mujoco`` is first imported, so the backend
must be chosen before that import. P0 measured on the target (Ubuntu 24.04, Wayland, Intel Mesa):
``egl`` and ``glfw`` work, ``osmesa`` fails (libOSMesa not installed). ``egl`` is the default
because it needs no window-system object at all (see decisions/ADR-005).
"""

from __future__ import annotations

import os
import sys

SUPPORTED_BACKENDS = ("egl", "glfw", "osmesa")
DEFAULT_BACKEND = "egl"


class BackendSelectionError(RuntimeError):
    pass


def resolve_backend(requested: str | None) -> str:
    """CLI choice > pre-set ``MUJOCO_GL`` environment variable > default ``egl``."""
    backend = (requested or os.environ.get("MUJOCO_GL") or DEFAULT_BACKEND).lower()
    if backend not in SUPPORTED_BACKENDS:
        raise BackendSelectionError(
            f"Unsupported MuJoCo GL backend {backend!r}; choose one of {', '.join(SUPPORTED_BACKENDS)}"
        )
    return backend


def select_backend(requested: str | None = None) -> str:
    """Set ``MUJOCO_GL`` (and the matching ``PYOPENGL_PLATFORM``) before ``mujoco`` is imported."""
    backend = resolve_backend(requested)
    if "mujoco" in sys.modules:
        current = os.environ.get("MUJOCO_GL", "").lower()
        if current != backend:
            raise BackendSelectionError(
                f"mujoco was already imported with MUJOCO_GL={current or '<unset>'}; "
                f"cannot switch to {backend!r} in this process"
            )
        return backend
    os.environ["MUJOCO_GL"] = backend
    if backend in ("egl", "osmesa"):
        os.environ["PYOPENGL_PLATFORM"] = backend
    return backend

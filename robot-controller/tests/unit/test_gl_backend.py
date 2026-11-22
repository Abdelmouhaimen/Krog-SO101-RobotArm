from __future__ import annotations

import pytest

from so101_companion.simulation import gl_backend


def test_resolve_prefers_cli_then_env_then_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MUJOCO_GL", raising=False)
    assert gl_backend.resolve_backend(None) == "egl"
    monkeypatch.setenv("MUJOCO_GL", "glfw")
    assert gl_backend.resolve_backend(None) == "glfw"
    assert gl_backend.resolve_backend("osmesa") == "osmesa"


def test_unknown_backend_rejected() -> None:
    with pytest.raises(gl_backend.BackendSelectionError):
        gl_backend.resolve_backend("vulkan")


def test_cannot_switch_backend_after_mujoco_import() -> None:
    import mujoco  # noqa: F401  (conftest selected egl before this import)

    assert gl_backend.select_backend("egl") == "egl"
    with pytest.raises(gl_backend.BackendSelectionError):
        gl_backend.select_backend("glfw")

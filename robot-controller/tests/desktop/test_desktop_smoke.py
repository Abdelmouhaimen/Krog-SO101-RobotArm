"""Optional, manual: launches the REAL app window on the current desktop session for a few seconds.

Run: python -m pytest -m desktop -s
Requires an active X11/Wayland session. Opens no device (the app installs the device guard).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.desktop


@pytest.mark.parametrize("backend", ["egl", "glfw"])
def test_single_window_desktop_smoke(tmp_path: Path, backend: str, scene_path: Path) -> None:
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("no desktop session")
    env = {
        k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "MUJOCO_GL", "PYOPENGL_PLATFORM")
    }
    out = tmp_path / backend
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "so101_companion",
            "--gl-backend",
            backend,
            "--smoke-seconds",
            "5",
            "--artifacts-dir",
            str(out),
        ],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(proc.stdout, proc.stderr)
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["passed"], metrics["checks"]
    assert metrics["environment"]["qt_platform"] in ("wayland", "xcb")
    assert proc.returncode == 0

"""T-033 / A17 / SR-06: no hardware access on import, launch, default mode or CI."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "so101_companion"
ADAPTER = "devices/lerobot_so101.py"

MODULE_LEVEL_DRIVER_IMPORT = re.compile(r"^(import|from)\s+(serial|lerobot|scservo_sdk)\b", re.M)
ANY_DRIVER_IMPORT = re.compile(r"^\s*(import|from)\s+(serial|lerobot|scservo_sdk)\b", re.M)
FORBIDDEN_SOURCE_PATTERNS = {
    "detached MuJoCo viewer": re.compile(r"mujoco\.viewer|launch_passive|viewer\.launch"),
    "serial enumeration / hard-coded port": re.compile(r"list_ports|glob\(['\"]/dev|/dev/tty(USB|ACM)\d"),
}


def _sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name != "device_guard.py")


@pytest.mark.parametrize("label", sorted(FORBIDDEN_SOURCE_PATTERNS))
def test_source_has_no_forbidden_pattern(label: str) -> None:
    pattern = FORBIDDEN_SOURCE_PATTERNS[label]
    hits = [str(p.relative_to(SRC)) for p in _sources() if pattern.search(p.read_text())]
    assert hits == [], f"{label} found in {hits}"


def test_driver_imports_only_lazily_inside_the_adapter() -> None:
    module_level = [
        str(p.relative_to(SRC)) for p in _sources() if MODULE_LEVEL_DRIVER_IMPORT.search(p.read_text())
    ]
    assert module_level == []
    anywhere = [str(p.relative_to(SRC)) for p in _sources() if ANY_DRIVER_IMPORT.search(p.read_text())]
    assert anywhere == [ADAPTER]


def test_no_automatic_calibration_calls_anywhere() -> None:
    """SR-08: AST check of real calls (not docstrings): no .calibrate(), every robot/bus .connect()
    passes calibrate=False, and only the adapter connects a robot at all (Qt signal .connect ignored)."""
    import ast

    connect_sites = []
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "calibrate", f"{path}: calls .calibrate()"
                receiver = ast.unparse(node.func.value)
                if node.func.attr == "connect" and ("robot" in receiver or "bus" in receiver):
                    kw = {k.arg: k.value for k in node.keywords}
                    assert isinstance(kw.get("calibrate"), ast.Constant) and kw["calibrate"].value is False, (
                        path
                    )
                    connect_sites.append(str(path.relative_to(SRC)))
    assert connect_sites == [ADAPTER]


def test_importing_every_module_pulls_in_no_hardware_driver() -> None:
    """Fresh interpreter: import all package modules, then inspect sys.modules."""
    modules = sorted(
        ("so101_companion." + ".".join(p.relative_to(SRC).with_suffix("").parts)).removesuffix(".__init__")
        for p in SRC.rglob("*.py")
        if p.name != "__main__.py"
    )
    code = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM']='offscreen'\n"
        "from so101_companion.simulation import gl_backend; gl_backend.select_backend('egl')\n"
        f"for m in {modules!r}: __import__(m)\n"
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "('serial','lerobot','scservo_sdk','feetech','dynamixel_sdk','usb','torch')]\n"
        "print('BAD', bad)\n"
        "print('VIEWER', 'mujoco.viewer' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "BAD []" in out
    assert "VIEWER False" in out


def test_hardware_start_modes_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    from so101_companion.app import EXIT_SETUP_ERROR, main

    for mode in ("real-to-sim", "sim-to-real"):
        assert main(["--mode", mode]) == EXIT_SETUP_ERROR
    assert "always starts in SIM_ONLY" in capsys.readouterr().err


def test_mujoco_backend_import_failure_is_an_explicit_setup_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from so101_companion.app import EXIT_SETUP_ERROR, main

    if not (ROOT / "vendor").exists():
        pytest.skip("official model not fetched")
    monkeypatch.setitem(sys.modules, "so101_companion.simulation.mujoco_pose", None)
    assert main(["--smoke-seconds", "0.1"]) == EXIT_SETUP_ERROR
    assert "MuJoCo could not initialise" in capsys.readouterr().err


def test_bad_config_is_a_setup_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from so101_companion.app import EXIT_SETUP_ERROR, main

    bad = tmp_path / "bad.toml"
    bad.write_text("[app]\nhardware_autoconnect = true\n")
    assert main(["--config", str(bad)]) == EXIT_SETUP_ERROR
    assert "not supported" in capsys.readouterr().err


def test_app_launch_in_subprocess_never_touches_devices_or_drivers() -> None:
    """Full SIM_ONLY launch (offscreen) for 1.5 s with sliders moving: no driver import, no device access."""
    if not (ROOT / "vendor").exists():
        pytest.skip("official model not fetched")
    code = (
        "import os, sys, json\n"
        "os.environ['QT_QPA_PLATFORM']='offscreen'\n"
        "from so101_companion.app import main\n"
        "import tempfile\n"
        "d = tempfile.mkdtemp()\n"
        "rc = main(['--smoke-seconds','1.5','--artifacts-dir', d])\n"
        "m = json.load(open(os.path.join(d,'metrics.json')))\n"
        "bad=[x for x in sys.modules if x.split('.')[0] in ('serial','lerobot','scservo_sdk','torch')]\n"
        "print('RESULT', json.dumps({'rc': rc, 'checks': m['checks'], 'bad': bad}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=120
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT "))
    import json

    result = json.loads(line.removeprefix("RESULT "))
    assert result["bad"] == []
    assert result["checks"]["no_device_access"] is True
    assert result["checks"]["still_sim_only_disconnected"] is True
    assert result["checks"]["exactly_one_visible_top_level_window"] is True

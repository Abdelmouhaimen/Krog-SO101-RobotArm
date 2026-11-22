"""T-036..T-038 / FR-16 / A17: commissioning scripts exist, are opt-in only and never run headless."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "commissioning"
ENTRY = sorted(SCRIPTS.glob("c0*.py"))
MOTION = {
    "c03_one_joint.py",
    "c04_gripper.py",
    "c05_limits_and_clipping.py",
    "c06_full_sync_and_timing.py",
    "c08_hold_torque_fault.py",
}


def test_ordered_procedure_set_is_complete() -> None:
    assert [p.name for p in ENTRY] == [
        "c01_connect_and_read.py",
        "c02_mirror_gui.py",
        "c03_one_joint.py",
        "c04_gripper.py",
        "c05_limits_and_clipping.py",
        "c06_full_sync_and_timing.py",
        "c07_record_rest_pose.py",
        "c08_hold_torque_fault.py",
    ]


@pytest.mark.parametrize("script", ENTRY, ids=lambda p: p.name)
def test_help_works_without_hardware(script: Path) -> None:
    out = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=60, check=False
    )
    assert out.returncode == 0 and "--operator-present" in out.stdout


@pytest.mark.parametrize("script", ENTRY, ids=lambda p: p.name)
def test_refuses_non_interactive_run_even_with_opt_in(script: Path) -> None:
    extra = (
        ["--joint", "elbow_flex"] if script.name in ("c03_one_joint.py", "c05_limits_and_clipping.py") else []
    )
    out = subprocess.run(
        [sys.executable, str(script), "--operator-present", "--port", "/dev/ttyUSB-never", *extra],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 3, out.stderr
    assert "REFUSED" in out.stderr and "interactively" in out.stderr
    assert "Calibration OK" not in out.stdout  # stopped before any setup


@pytest.mark.parametrize("script", ENTRY, ids=lambda p: p.name)
def test_operator_gate_precedes_any_session(script: Path) -> None:
    tree = ast.parse(script.read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [ast.unparse(n.func) for n in ast.walk(main) if isinstance(n, ast.Call)]
    assert "require_operator" in calls
    first_gate = min(
        n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "require_operator"
    )
    sessions = [
        n.lineno for n in ast.walk(main) if isinstance(n, ast.Call) and ast.unparse(n.func) == "Session"
    ]
    launches = [
        n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "subprocess.run"
    ]
    assert all(first_gate < ln for ln in sessions + launches)


@pytest.mark.parametrize("script", [p for p in ENTRY if p.name in MOTION], ids=lambda p: p.name)
def test_motion_scripts_require_typed_move_confirmation(script: Path) -> None:
    text = script.read_text()
    assert "arm(s)" in text  # _motion.arm(): SIM_TO_REAL + typed "MOVE" + EnableMovement(confirmed=True)
    motion_helper = (SCRIPTS / "_motion.py").read_text()
    assert 'confirm_word("Keep clear of the arm.", "MOVE")' in motion_helper


def test_require_operator_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    monkeypatch.syspath_prepend(str(SCRIPTS))
    import _common

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(SystemExit) as exc:
        _common.require_operator(argparse.Namespace(operator_present=False))
    assert exc.value.code == 3
    _common.require_operator(argparse.Namespace(operator_present=True))  # passes


def test_rest_pose_script_is_read_only() -> None:
    text = (SCRIPTS / "c07_record_rest_pose.py").read_text()
    assert "arm(s)" not in text and "EnableMovement" not in text and ".send(" not in text
    assert "CANDIDATE" in text and "T-039" in text


def test_scripts_never_auto_select_a_port() -> None:
    text = (SCRIPTS / "_common.py").read_text()
    assert "nothing is selected automatically" in text
    assert "ttyUSB0" not in text and "ttyACM0" not in text

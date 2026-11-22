"""The guard must block serial access BEFORE it happens. Paths below are deliberately nonexistent
names that only match the serial patterns, so even a broken guard cannot touch a real device."""

from __future__ import annotations

import contextlib
import os

import pytest

from so101_companion.diagnostics import device_guard
from so101_companion.diagnostics.device_guard import DeviceAccessViolation

FAKE_SERIAL_PATHS = [
    "/dev/ttyUSB-so101-guard-selftest",
    "/dev/ttyACM-so101-guard-selftest",
    "/dev/serial/by-id/usb-so101-guard-selftest",
]


def test_guard_is_active_in_test_session() -> None:
    assert device_guard.is_active()


@pytest.mark.parametrize("path", FAKE_SERIAL_PATHS)
def test_builtin_open_of_serial_path_is_blocked(path: str, expect_device_violation: None) -> None:
    with pytest.raises(DeviceAccessViolation):
        open(path, "rb")  # noqa: SIM115
    assert any(path in v for v in device_guard.violations())


@pytest.mark.parametrize("path", FAKE_SERIAL_PATHS)
def test_os_open_of_serial_path_is_blocked(path: str, expect_device_violation: None) -> None:
    with pytest.raises(DeviceAccessViolation):
        os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)


@pytest.mark.parametrize("path", ["/dev", "/dev/", "/dev/serial/by-id"])
def test_device_directory_enumeration_is_blocked(path: str, expect_device_violation: None) -> None:
    with pytest.raises(DeviceAccessViolation):
        os.listdir(path)
    with pytest.raises(DeviceAccessViolation):
        os.scandir(path)


IMPORT_PROBE = """
import importlib, sys
from so101_companion.diagnostics import device_guard
from so101_companion.diagnostics.device_guard import DeviceAccessViolation
device_guard.install()
results = {}
for how, target in %r:
    try:
        if how == "module":
            importlib.import_module(target)
        else:
            exec(target, {})
        results[target] = "IMPORTED"
    except DeviceAccessViolation:
        results[target] = "BLOCKED"
    except ImportError:
        results[target] = "NOT_INSTALLED"
print(results)
"""


def test_driver_imports_are_blocked_in_a_fresh_interpreter() -> None:
    """Fresh interpreter: an import hook cannot intercept modules already cached in sys.modules
    (e.g. by the LeRobot contract tests), so this self-test must not run in-process."""
    import ast
    import subprocess
    import sys

    probes = [
        ("module", "serial"),
        ("module", "scservo_sdk"),
        ("module", "lerobot.robots.so_follower"),
        ("stmt", "import serial"),
        ("stmt", "from serial.tools import list_ports"),
        ("stmt", "import lerobot"),
    ]
    out = subprocess.run(
        [sys.executable, "-c", IMPORT_PROBE % (probes,)], capture_output=True, text=True, check=True
    ).stdout
    results = ast.literal_eval(out.strip().splitlines()[-1])
    assert set(results.values()) == {"BLOCKED"}, results


def test_ordinary_files_and_non_serial_dev_nodes_are_allowed(tmp_path: os.PathLike[str]) -> None:
    p = os.path.join(tmp_path, "x.txt")
    with open(p, "w") as fh:
        fh.write("ok")
    with open("/dev/null", "rb") as fh:
        fh.read(1)
    assert device_guard.violations() == []


def test_violation_is_recorded_even_if_exception_is_swallowed(expect_device_violation: None) -> None:
    with contextlib.suppress(Exception):
        open(FAKE_SERIAL_PATHS[0], "rb")  # noqa: SIM115
    assert len(device_guard.violations()) == 1

"""Development-time guard that detects and blocks serial-device access from Python code.

Used by the test suite (always) and by the app until the operator confirms a hardware connection.
It relies on CPython audit hooks (PEP 578): ``open``/``os.open`` of serial device nodes and
directory listing of ``/dev`` (port enumeration/probing) are recorded and, while enabled, rejected
with :class:`DeviceAccessViolation` *before* the operation happens. A ``sys.meta_path`` blocker
does the same for imports of hardware driver packages (tests forbid them by default; the app
allows the lazy LeRobot import because device opens remain blocked until Connect is confirmed).

Limitations (deliberately documented, this is NOT a security boundary):
* native code that calls ``open(2)`` directly (C extensions, ctypes) bypasses audit hooks;
* audit hooks cannot be removed once installed; ``set_enabled(False)`` only stops enforcement.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import re
import sys
import threading
import types
from collections.abc import Sequence
from typing import Any

SERIAL_PATH_RE = re.compile(r"^/dev/(tty(USB|ACM|S|AMA|THS|XRUSB)|serial(/|$)|cu\.)")
PROBE_DIR_RE = re.compile(r"^/dev(/serial(/.*)?)?/?$")
DEFAULT_FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {"serial", "scservo_sdk", "lerobot", "feetech", "dynamixel_sdk", "pyftdi", "usb"}
)


class DeviceAccessViolation(RuntimeError):
    """Raised when guarded code attempts serial/device access."""


_lock = threading.Lock()
_installed = False
_enabled = False
_forbidden_modules: frozenset[str] = DEFAULT_FORBIDDEN_MODULES
_violations: list[str] = []


def _as_path(raw: Any) -> str | None:
    if isinstance(raw, int) or raw is None:
        return None
    try:
        path = os.fsdecode(raw)
    except TypeError:
        return None
    return os.path.abspath(path)


def _is_serial_path(path: str) -> bool:
    if SERIAL_PATH_RE.match(path):
        return True
    # Symlinks such as /dev/serial/by-id/* resolve to /dev/ttyUSB*; realpath does not open files.
    if path.startswith("/dev/"):
        return bool(SERIAL_PATH_RE.match(os.path.realpath(path)))
    return False


def _record(message: str) -> None:
    with _lock:
        _violations.append(message)
    raise DeviceAccessViolation(message)


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if not _enabled:
        return
    if event == "open":
        path = _as_path(args[0]) if args else None
        if path is not None and _is_serial_path(path):
            _record(f"serial device open blocked: {path}")
    elif event in ("os.listdir", "os.scandir"):
        path = _as_path(args[0]) if args else None
        if path is not None and PROBE_DIR_RE.match(path):
            _record(f"device directory enumeration blocked: {path}")


class _ForbiddenImportBlocker(importlib.abc.MetaPathFinder):
    """First entry on ``sys.meta_path``. Catches ``import x`` AND ``importlib.import_module('x')``
    (the latter does not emit the ``import`` audit event). Already-imported modules are not
    re-checked, which is why tests also inspect ``sys.modules`` in a fresh interpreter."""

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: types.ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if _enabled and fullname.split(".", 1)[0] in _forbidden_modules:
            _record(f"hardware driver import blocked: {fullname}")
        return None


_blocker = _ForbiddenImportBlocker()


def install(*, forbidden_modules: frozenset[str] | None = None) -> None:
    """Install (once) and enable the guard."""
    global _installed, _enabled, _forbidden_modules
    with _lock:
        if forbidden_modules is not None:
            _forbidden_modules = frozenset(forbidden_modules)
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True
        if _blocker not in sys.meta_path:
            sys.meta_path.insert(0, _blocker)
        _enabled = True


def set_forbidden_modules(modules: frozenset[str]) -> frozenset[str]:
    """Replace the forbidden-import set; returns the previous one. Device opens stay blocked."""
    global _forbidden_modules
    with _lock:
        previous, _forbidden_modules = _forbidden_modules, frozenset(modules)
    return previous


def set_enabled(enabled: bool) -> None:
    global _enabled
    with _lock:
        _enabled = enabled


def is_active() -> bool:
    return _installed and _enabled


def violations() -> list[str]:
    with _lock:
        return list(_violations)


def reset_violations() -> None:
    with _lock:
        _violations.clear()

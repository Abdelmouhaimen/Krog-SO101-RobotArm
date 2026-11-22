"""Shared helpers for the USER-RUN Gate B commissioning scripts. NEVER run by CI or by an agent.

Every script:
* refuses to start unless stdin is an interactive terminal AND ``--operator-present`` is given;
* prints test id, preconditions, what will happen, expected result and failure instructions;
* requires typed confirmation words before connecting, moving or removing torque;
* uses exactly the application's controller + safety pipeline + LeRobot adapter (no shortcuts),
  (fixed M1 policy: calibrated bounds only, targets forwarded directly through LeRobot);
* on Ctrl-C or any error during motion requests a best-effort Emergency Hold, then asks the
  operator what to do; it never reconnects or resumes automatically;
* writes a redacted JSON report + event log to ``commissioning-reports/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "commissioning-reports"
EXIT_REFUSED = 3


def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument(
        "--operator-present",
        action="store_true",
        help="REQUIRED: a human operator is at the desk-clamped, supported arm",
    )
    p.add_argument(
        "--port", default="", help="servo bus device, e.g. /dev/serial/by-id/usb-... (asked if empty)"
    )
    p.add_argument("--calibration", default="", help="EXISTING LeRobot calibration json (asked if empty)")
    p.add_argument("--config", type=Path, default=None, help="app config TOML (default: built-in defaults)")
    return p


def refuse(msg: str) -> None:
    print(f"REFUSED: {msg}", file=sys.stderr)
    raise SystemExit(EXIT_REFUSED)


def require_operator(args: argparse.Namespace) -> None:
    if not sys.stdin.isatty():
        refuse(
            "this hardware procedure must be run interactively by the operator in a terminal "
            "(it is never run by CI, scripts or coding agents)"
        )
    if not args.operator_present:
        refuse("pass --operator-present to confirm a human is at the clamped, supported arm")


def banner(
    test_id: str,
    title: str,
    *,
    preconditions: list[str],
    actions: list[str],
    expected: list[str],
    on_failure: list[str],
) -> None:
    line = "=" * 78
    print(f"\n{line}\n{test_id} — {title}\n{line}")
    for head, items in (
        ("PRECONDITIONS", preconditions),
        ("THIS SCRIPT WILL", actions),
        ("EXPECTED RESULT", expected),
        ("IF SOMETHING LOOKS WRONG", on_failure),
    ):
        print(f"\n{head}:")
        for item in items:
            print(f"  • {item}")
    print(f"\n{line}")


def confirm_word(prompt: str, word: str) -> bool:
    answer = input(f"\n{prompt}\nType {word!r} to continue, anything else to abort: ").strip()
    return answer == word


def ask_yes_no(question: str) -> bool:
    while True:
        a = input(f"{question} [y/n]: ").strip().lower()
        if a in ("y", "yes", "n", "no"):
            return a.startswith("y")


def choose_port(given: str) -> str:
    if given:
        return given
    by_id = Path("/dev/serial/by-id")
    candidates = sorted(str(p) for p in by_id.iterdir()) if by_id.is_dir() else []  # explicit operator run
    print("\nSerial devices found under /dev/serial/by-id (nothing is selected automatically):")
    for i, c in enumerate(candidates):
        print(f"  [{i}] {c}")
    if not candidates:
        print("  (none) — type the device path manually")
    raw = input("Enter the index or the full device path of the SO-101 FOLLOWER servo bus: ").strip()
    if raw.isdigit() and int(raw) < len(candidates):
        return candidates[int(raw)]
    if not raw.startswith("/dev/"):
        refuse("no valid port selected")
    return raw


def choose_calibration(given: str) -> str:
    from so101_companion.devices.calibration import default_calibration_hint

    if given:
        return given
    hint = default_calibration_hint()
    files = sorted(hint.glob("*.json")) if hint.is_dir() else []
    print(f"\nExisting LeRobot calibration files in {hint} (none is selected automatically):")
    for i, f in enumerate(files):
        print(f"  [{i}] {f.name}")
    raw = input("Enter the index or full path of the EXISTING calibration file: ").strip()
    if raw.isdigit() and int(raw) < len(files):
        return str(files[int(raw)])
    return raw


class Session:
    """Headless controller session using the application's coordinator and real LeRobot adapter."""

    def __init__(self, args: argparse.Namespace, test_id: str) -> None:
        from so101_companion.app import real_robot_factory
        from so101_companion.config import load_config
        from so101_companion.control.coordinator import ControllerCoordinator
        from so101_companion.devices.calibration import calibrated_bounds, load_calibration
        from so101_companion.safety import policy
        from so101_companion.telemetry.events import EventLog

        self.test_id = test_id
        self.cfg = load_config(args.config)
        self.port = choose_port(args.port)
        self.calibration = choose_calibration(args.calibration)
        cal = load_calibration(self.calibration)  # raises before any hardware access
        self.bounds = calibrated_bounds(cal, policy.ANGULAR_MARGIN_DEG)
        print(f"\nCalibration OK: id={cal.robot_id}. Allowed ranges (calibrated, 5° margin):")
        for name, (lo, hi) in self.bounds.as_tuples().items():
            print(f"  {name:14s} {lo:+8.1f} … {hi:+8.1f}")
        self.events = EventLog(self.cfg.app.event_ring_size)
        initial = dict.fromkeys(self.bounds.limits, 0.0)
        self.c = ControllerCoordinator(
            self.cfg, self.bounds, initial, real_robot_factory(self.cfg), events=self.events
        )
        self.period = 1.0 / self.cfg.app.command_hz
        self.results: dict[str, Any] = {
            "test_id": test_id,
            "started": datetime.now().isoformat(timespec="seconds"),
        }

    # -- requests ------------------------------------------------------------------------------
    def request(self, req: Any) -> Any:
        self.c.submit(req)
        self.c.process_requests()
        snap = self.c.snapshot()
        print(
            f"  -> {type(req).__name__}: {snap.connection.value} | torque {snap.torque.value} | "
            f"{snap.message}"
        )
        return snap

    def connect(self) -> bool:
        from so101_companion.control import requests as rq
        from so101_companion.ui.dialogs import CONNECT_WARNING

        print("\n" + CONNECT_WARNING.format(port=self.port, cal=self.calibration))
        if not confirm_word("Support the arm now.", "CONNECT"):
            print("Aborted before connecting. Nothing was touched.")
            return False
        snap = self.request(rq.Connect(self.port, self.calibration, confirmed=True))
        self.results["connect"] = {
            "connection": snap.connection.value,
            "calibration_valid": snap.calibration_valid,
            "driver": dict(snap.extra.get("robot", {})),
        }
        return snap.connection.value == "CONNECTED_READ_ONLY"

    def tick_for(self, seconds: float, on_tick: Callable[[Any], bool | None] | None = None) -> Any:
        """Run controller ticks at command_hz. ``on_tick`` may return True to stop early.
        Ctrl-C -> best-effort Emergency Hold."""
        end = time.monotonic() + seconds
        next_t = time.monotonic()
        snap = self.c.snapshot()
        try:
            while time.monotonic() < end:
                now = time.monotonic()
                if now < next_t:
                    time.sleep(next_t - now)
                self.c.tick()
                next_t += self.period
                if time.monotonic() > next_t:  # overrun: skip, never catch up
                    next_t = time.monotonic() + self.period
                snap = self.c.snapshot()
                if snap.connection.value == "FAULT_LATCHED":
                    print(f"\n!!! FAULT LATCHED: {snap.fault.code} — {snap.fault.details}")
                    return snap
                if on_tick and on_tick(snap):
                    return snap
        except KeyboardInterrupt:
            print("\nCtrl-C: requesting best-effort EMERGENCY HOLD")
            self.emergency_hold()
            snap = self.c.snapshot()
        return snap

    def emergency_hold(self) -> None:
        from so101_companion.control import requests as rq
        from so101_companion.domain.state import Action

        if Action.EMERGENCY_HOLD in self.c.available_actions():
            self.request(rq.EmergencyHold())

    def set_target(self, **values: float) -> None:
        snap = self.c.snapshot()
        base = (
            dict(snap.requested.values)
            if snap.requested.values is not None
            else dict(snap.actual.values or {})
        )
        base.update(values)
        self.c.set_requested(base)

    @staticmethod
    def row(snap: Any, joint: str) -> str:
        def g(sv: Any) -> str:
            v = sv.get(joint)
            return "   N/A " if v is None else f"{v:+7.2f}"

        return (
            f"tick {snap.seq:5d} {joint}: requested {g(snap.requested)} staged {g(snap.staged)} "
            f"sent {g(snap.sent)} actual {g(snap.actual)} {' '.join(snap.clamp_reasons)}"
        )

    # -- shutdown & report ---------------------------------------------------------------------
    def finish(self) -> Path:
        from so101_companion.control import requests as rq
        from so101_companion.domain.state import Action

        if self.c.robot is not None and Action.DISCONNECT in self.c.available_actions():
            dis = self.c.robot.disable_torque_on_disconnect
            print(
                "\nThe script will now DISCONNECT. "
                + (
                    "disable_torque_on_disconnect=True: MOTORS WILL GO LIMP — support the arm."
                    if dis
                    else "Torque is left as is (disable_torque_on_disconnect=False)."
                )
            )
            if confirm_word("Ready to disconnect?", "DISCONNECT"):
                self.request(rq.Disconnect(confirmed=True))
            else:
                print(
                    "Not disconnecting now. NOTE: when this process exits, LeRobot's destructor may still "
                    "disconnect (and disable torque). Keep supporting the arm."
                )
                input("Press Enter when the arm is supported to exit the script...")
                self.request(rq.Disconnect(confirmed=True))
        return self.write_report()

    def write_report(self) -> Path:
        from so101_companion.telemetry.events import redact

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        path = REPORT_DIR / f"{stamp}_{self.test_id}.json"
        self.results["finished"] = datetime.now().isoformat(timespec="seconds")
        self.results["port"] = self.port
        self.results["calibration"] = self.calibration
        path.write_text(json.dumps(redact(self.results), indent=2, default=str) + "\n")
        self.events.export_jsonl(path.with_suffix(".events.jsonl"))
        print(f"\nReport written: {path}\nEvent log:      {path.with_suffix('.events.jsonl')}")
        print("Copy the outcome into templates/HARDWARE_TEST_REPORT.md (PASS/FAIL/NOT RUN).")
        return path


def guarded_main(fn: Callable[[], int]) -> None:
    try:
        raise SystemExit(fn())
    except KeyboardInterrupt:
        print("\nInterrupted before any motion. Nothing further was sent.")
        raise SystemExit(130) from None

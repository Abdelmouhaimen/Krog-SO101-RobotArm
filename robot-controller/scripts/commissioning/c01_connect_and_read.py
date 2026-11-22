#!/usr/bin/env python3
"""B01 — Connect and read all six joint positions. NO motion commands are sent. Operator-run only."""

from __future__ import annotations

from _common import Session, ask_yes_no, banner, base_parser, guarded_main, require_operator


def main() -> int:
    args = base_parser(__doc__ or "").parse_args()
    require_operator(args)
    banner(
        "B01",
        "Connect + read-only observation",
        preconditions=[
            "SO-101 follower clamped to the desk, powered, USB connected, nothing within reach of the arm",
            "An EXISTING LeRobot calibration file for THIS arm (M1 never calibrates automatically)",
            "Your hand ready to support the arm: LeRobot configure() toggles torque off/on while connecting",
        ],
        actions=[
            "validate the calibration file (no hardware access)",
            "after you type CONNECT: SOFollower.connect(calibrate=False), then compare the motors' "
            "calibration registers with the file (read-only)",
            "switch to REAL_TO_SIM and read positions at 20 Hz for 5 s — zero position commands",
            "disconnect after you type DISCONNECT",
        ],
        expected=[
            "CONNECTED_READ_ONLY, calibration VALID (registers match the file)",
            "six finite values: arm joints in degrees near 0 at mid-range, gripper 0..100",
            "values change when the arm is moved by hand ONLY after Torque Off (not tested here)",
            "command count stays 0",
        ],
        on_failure=[
            "CALIBRATION_MISMATCH: wrong file/id or arm re-calibrated — stop; recalibrate with LeRobot's own "
            "supervised tool outside this app, then retry",
            "connect failed: check port (/dev/serial/by-id/...), power, cable; nothing was moved",
            "unexpected motion during connect: cut power, note it in the report",
        ],
    )
    s = Session(args, "B01")
    if not s.connect():
        s.write_report()
        return 1
    from so101_companion.control import requests as rq
    from so101_companion.domain.state import Mode

    s.request(rq.SetMode(Mode.REAL_TO_SIM))
    samples: list[dict[str, float]] = []

    def collect(snap: object) -> None:
        values = snap.actual.values
        if values is not None:
            samples.append(dict(values))

    snap = s.tick_for(5.0, collect)
    print(
        f"\nRead {len(samples)} observations, achieved {snap.tick_hz_actual:.1f} Hz, commands sent: "
        f"{snap.command_seq}"
    )
    if samples:
        last = samples[-1]
        for name, value in last.items():
            print(f"  {name:14s} {value:+8.2f}{' %' if name == 'gripper' else '°'}")
    s.results.update(
        samples=len(samples),
        last_reading=samples[-1] if samples else None,
        achieved_hz=round(snap.tick_hz_actual, 2),
        commands_sent=snap.command_seq,
        final_state=snap.connection.value,
        operator_values_plausible=ask_yes_no("Do the six values look plausible for the arm's current pose?"),
    )
    s.results["pass"] = bool(samples) and snap.command_seq == 0 and s.results["operator_values_plausible"]
    s.finish()
    return 0 if s.results["pass"] else 1


if __name__ == "__main__":
    guarded_main(main)

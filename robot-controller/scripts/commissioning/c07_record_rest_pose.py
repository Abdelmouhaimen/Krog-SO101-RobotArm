#!/usr/bin/env python3
"""B08 preparation (T-039, first half) — RECORD a measured physical rest pose. READ-ONLY.

This script only reads and writes a CANDIDATE report file that the application does not read.
Since 2026-11-07 the application itself records the rest pose ("Record rest pose…") and implements
Normal Stop (ADR-011); use this script only for an independent, read-only capture.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime

from _common import REPORT_DIR, Session, ask_yes_no, banner, base_parser, guarded_main, require_operator


def main() -> int:
    args = base_parser(__doc__ or "").parse_args()
    require_operator(args)
    banner(
        "B08-prep",
        "Record the measured physical rest pose (read-only)",
        preconditions=["B01–B06 passed", "you know where the arm should rest (e.g. folded, supported)"],
        actions=[
            "connect, REAL_TO_SIM (read-only)",
            "optionally Torque Off (you support the arm) so you can place it by hand, then Enable torque "
            "to hold it there",
            "average 40 readings and write commissioning-reports/rest_pose.candidate.<time>.json",
        ],
        expected=["six stable values (std < 0.5)", "NO motion commands"],
        on_failure=["unstable readings: arm not resting — re-place it and repeat"],
    )
    s = Session(args, "B08_rest_capture")
    if not s.connect():
        s.finish()
        return 1
    from so101_companion.control import requests as rq
    from so101_companion.domain.state import Mode

    s.request(rq.SetMode(Mode.REAL_TO_SIM))
    if ask_yes_no("Turn torque OFF to place the arm by hand? (You must support it!)"):
        s.request(rq.TorqueOff(confirmed=True))
        input("Place the arm in its rest pose and keep supporting it. Press Enter to continue...")
        if ask_yes_no("Enable torque to hold this pose (goal := present position first)?"):
            s.request(rq.EnableTorque(confirmed=True))
    samples: list[dict[str, float]] = []
    s.tick_for(2.0, lambda snap: samples.append(dict(snap.actual.values)) if snap.actual.values else None)
    if len(samples) < 10:
        print("Not enough readings.")
        s.finish()
        return 1
    pose = {k: statistics.fmean(x[k] for x in samples) for k in samples[0]}
    spread = {k: statistics.pstdev(x[k] for x in samples) for k in samples[0]}
    outside = s.bounds.outside(pose)
    record = {
        "status": "CANDIDATE — not used by the application (the app records its own rest pose, ADR-011)",
        "recorded": datetime.now().isoformat(timespec="seconds"),
        "units": "arm: LeRobot calibrated degrees; gripper: LeRobot 0..100",
        "calibration_id": s.c.robot.calibration.robot_id if s.c.robot and s.c.robot.calibration else None,
        "pose": pose,
        "stddev": spread,
        "joints_inside_5deg_margin": outside,
        "samples": len(samples),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"rest_pose.candidate.{datetime.now():%Y%m%dT%H%M%S}.json"
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    print(
        f"\nSaved {out}. To use a rest pose for Normal Stop, record it in the application "
        "(Record rest pose…, ADR-011)."
    )
    s.results["rest_pose_file"] = str(out)
    s.results["pass"] = all(v < 0.5 for v in spread.values())
    s.finish()
    return 0 if s.results["pass"] else 1


if __name__ == "__main__":
    guarded_main(main)

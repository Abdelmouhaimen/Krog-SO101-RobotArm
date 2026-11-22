#!/usr/bin/env python3
"""B02 (and B05 visual part) — guided REAL_TO_SIM mirroring check in the integrated window.

Launches the normal application (it starts in SIM_ONLY and connects nothing by itself) with the
port / calibration fields pre-filled, prints the checklist, then records the operator's findings.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from _common import (
    ROOT,
    ask_yes_no,
    banner,
    base_parser,
    choose_calibration,
    choose_port,
    guarded_main,
    require_operator,
)

CHECKLIST = """
IN THE WINDOW (nothing happens until you click):
  1. Click 'Connect…', read the warning, support the arm, confirm.
  2. Select REAL_TO_SIM. The sliders become display-only; ACTUAL column updates; SENT stays N/A.
  3. Support the arm with one hand, click 'Torque OFF…' and confirm. Gently move ONE joint at a time.
     Check the 3D model moves the SAME joint in the SAME direction by a similar amount.
     Order: shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, then squeeze/open the gripper.
  4. Note any joint that moves the opposite way (sign) or sits at a constant offset at the same pose.
  5. Hold the arm in a safe pose, click 'Enable torque…' (holds the present pose).
  6. Optional: 'Export log…' to commissioning-reports/.
  7. Click 'Disconnect…' (support the arm: torque is disabled on disconnect by default), close the window.
"""


def main() -> int:
    args = base_parser(__doc__ or "").parse_args()
    require_operator(args)
    banner(
        "B02",
        "Real-to-sim mirror (visual)",
        preconditions=["B01 passed", "arm clamped; hand ready to support it while torque is off"],
        actions=[
            "launch the integrated window with fields pre-filled",
            "record your observations afterwards",
        ],
        expected=[
            "the MuJoCo model follows the MEASURED arm pose for all six joints",
            "no position command is ever sent in REAL_TO_SIM (SENT column stays N/A, cmd #0)",
        ],
        on_failure=[
            "opposite direction or constant offset: record joint, sign and offset; adjust ONLY "
            "[simulation] joint_sign / joint_offset_deg / logical_gripper_0_maps_to in your config "
            "(official model files stay unchanged), then repeat B02",
            "FAULT banner: note code, disconnect, check cable/power",
        ],
    )
    port, cal = choose_port(args.port), choose_calibration(args.calibration)
    cfg_path = Path(tempfile.mkdtemp(prefix="so101-b02-")) / "b02.toml"
    base = (args.config.read_text() + "\n") if args.config else ""
    if "[hardware]" in base:
        print("NOTE: your --config has a [hardware] section; enter port/calibration in the window manually.")
        cfg_text = base
    else:
        cfg_text = base + f'[hardware]\nport = "{port}"\ncalibration_path = "{cal}"\n'
    cfg_path.write_text(cfg_text)
    print(CHECKLIST)
    input("Press Enter to open the application window...")
    subprocess.run(
        [sys.executable, "-m", "so101_companion", "--config", str(cfg_path)], cwd=ROOT, check=False
    )

    from _common import REPORT_DIR

    findings = {
        "model_follows_all_joints": ask_yes_no("Did the 3D model follow all six joints?"),
        "no_commands_in_mirror": ask_yes_no("Did SENT stay N/A / cmd #0 while in REAL_TO_SIM?"),
        "notes_sign_offset": input(
            "Joints with wrong direction / offset (e.g. 'wrist_roll sign', or 'none'): "
        ),
        "gripper_direction": input("Gripper: when you OPENED it, did the logical value go UP or DOWN? "),
    }
    import json
    from datetime import datetime

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{datetime.now():%Y%m%dT%H%M%S}_B02.json"
    out.write_text(json.dumps({"test_id": "B02", **findings}, indent=2) + "\n")
    print(f"Report written: {out}")
    return 0 if findings["model_follows_all_joints"] and findings["no_commands_in_mirror"] else 1


if __name__ == "__main__":
    guarded_main(main)

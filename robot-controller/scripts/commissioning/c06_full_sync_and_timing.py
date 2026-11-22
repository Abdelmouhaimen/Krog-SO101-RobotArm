#!/usr/bin/env python3
"""B05/B09 — full sync timing on the real bus, then the guided GUI sim-to-real check. Operator-run only.

Phase 1 (headless, no motion): 10 s REAL_TO_SIM read loop, then 10 s ARMED with the target held at
the measured pose (send_action of the present pose -> no motion), measuring achieved controller Hz,
cycle time and skipped deadlines. Phase 2: open the integrated window for the visual SIM_TO_REAL check.
"""

from __future__ import annotations

import statistics
import subprocess
import sys

from _common import ROOT, Session, ask_yes_no, banner, base_parser, guarded_main, require_operator
from _motion import arm

GUI_CHECKLIST = """
IN THE WINDOW: Connect… -> SIM_TO_REAL -> Enable movement… Move sliders SLOWLY, one joint at a time,
then two joints together. Confirm: the arm follows; STAGED = requested clamped to the calibrated
range; SENT == STAGED (forwarded directly); the 3D model shows the MEASURED pose.
Press EMERGENCY HOLD once while moving: motion stops, state HOLDING.
Disarm, Disconnect… (support the arm), close.
"""


def measure(s: Session, seconds: float) -> dict[str, float]:
    cycles: list[float] = []

    def on_tick(snap: object) -> None:
        cycles.append(snap.cycle_ms)

    snap = s.tick_for(seconds, on_tick)
    return {
        "achieved_hz": round(snap.tick_hz_actual, 2),
        "cycle_ms_median": round(statistics.median(cycles), 2) if cycles else -1.0,
        "cycle_ms_max": round(max(cycles), 2) if cycles else -1.0,
        "skipped_deadlines": float(snap.overruns),
        "ticks": float(len(cycles)),
    }


def main() -> int:
    args = base_parser(__doc__ or "").parse_args()
    require_operator(args)
    banner(
        "B09 + B05",
        "Bus timing at 20 Hz, then full sim-to-real sync in the window",
        preconditions=["B01–B04 passed", "arm clear of obstacles"],
        actions=[
            "read-only timing 10 s",
            "armed, holding the measured pose, timing 10 s (no motion intended)",
            "open the integrated window for the guided check",
        ],
        expected=[
            "achieved Hz and cycle times recorded honestly (20 Hz is a target, not a guarantee)",
            "no motion during the armed timing phase",
        ],
        on_failure=["Ctrl-C = best-effort hold", "any motion in phase 1: FAIL, stop and report"],
    )
    s = Session(args, "B09_B05")
    if not s.connect():
        s.finish()
        return 1
    from so101_companion.control import requests as rq
    from so101_companion.domain.state import Mode

    s.request(rq.SetMode(Mode.REAL_TO_SIM))
    s.results["read_only"] = measure(s, 10.0)
    if arm(s):
        s.results["armed_hold_pose"] = measure(s, 10.0)
        s.results["moved_during_armed_phase"] = ask_yes_no("Did the arm move during the armed timing phase?")
    print(f"\nTiming: {s.results}")
    s.finish()
    print(GUI_CHECKLIST)
    if ask_yes_no("Open the integrated window now for the visual sync check?"):
        subprocess.run([sys.executable, "-m", "so101_companion"], cwd=ROOT, check=False)
        print("Record the GUI result manually in templates/HARDWARE_TEST_REPORT.md (B05).")
    return 0


if __name__ == "__main__":
    guarded_main(main)

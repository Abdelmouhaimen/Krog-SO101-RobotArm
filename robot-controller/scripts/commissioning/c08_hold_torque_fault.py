#!/usr/bin/env python3
"""B06/B07 — Emergency Hold, Torque Off / Enable torque, and a SAFE manual USB-fault rehearsal.
Normal Stop-to-Rest is NOT tested: it does not exist until T-039. Operator-run only."""

from __future__ import annotations

import time

from _common import Session, ask_yes_no, banner, base_parser, confirm_word, guarded_main, require_operator
from _motion import arm


def main() -> int:
    p = base_parser(__doc__ or "")
    p.add_argument("--skip-fault-rehearsal", action="store_true")
    args = p.parse_args()
    require_operator(args)
    banner(
        "B07 + B06",
        "Emergency Hold, Torque Off, fault latch",
        preconditions=["B03 passed", "you can support the whole arm with one hand", "USB cable reachable"],
        actions=[
            "small elbow motion, then EMERGENCY HOLD mid-motion",
            "TORQUE OFF (typed confirmation; support the arm), check mirror while you move it by hand",
            "Enable torque (holds the present pose)",
            "fault rehearsal in READ-ONLY mirror: YOU unplug the USB cable when asked; nothing is moving",
        ],
        expected=[
            "HOLD: state HOLDING, no further sends; the arm stops near where it was (best effort)",
            "TORQUE OFF: arm limp (supported); REAL_TO_SIM still mirrors",
            "USB unplug: FAULT_LATCHED within a fraction of a second, torque UNKNOWN, no reconnect attempts; "
            "after replugging nothing resumes by itself; recovery = Disconnect + new connection (B01)",
            "Normal Stop-to-Rest: NOT AVAILABLE (by design until T-039)",
        ],
        on_failure=["arm falls unexpectedly / does not stop: cut power, FAIL, report"],
    )
    s = Session(args, "B07_B06")
    if not s.connect() or not arm(s):
        s.finish()
        return 1
    from so101_companion.control import requests as rq
    from so101_companion.domain.state import Mode

    start = s.c.snapshot().actual.get("elbow_flex")
    assert start is not None
    lim = s.bounds.limits["elbow_flex"]
    target = start + 20.0 if start + 20.0 <= lim.upper else start - 20.0
    s.set_target(elbow_flex=target)
    s.tick_for(0.25)  # mid-motion
    s.emergency_hold()
    sends_before = s.c.command_seq
    s.tick_for(1.0)
    hold_ok = s.c.snapshot().connection.value == "HOLDING" and s.c.command_seq == sends_before
    s.results["hold"] = {
        "state_holding_no_more_sends": hold_ok,
        "operator_arm_stopped": ask_yes_no("Did the arm stop promptly after HOLD?"),
    }

    s.request(rq.SetMode(Mode.REAL_TO_SIM))
    print("\nTORQUE OFF next: all motors stop holding. SUPPORT THE ARM NOW.")
    if confirm_word("Arm supported?", "TORQUE OFF"):
        s.request(rq.TorqueOff(confirmed=True))
        print("Move the elbow gently by hand for 3 s; values below should follow.")
        s.tick_for(3.0, lambda snap: print(Session.row(snap, "elbow_flex")) if snap.seq % 10 == 0 else None)
        s.results["torque_off"] = {
            "limp": ask_yes_no("Was the arm limp (you were holding it)?"),
            "mirror_followed": ask_yes_no("Did the elbow value follow your hand?"),
        }
        input("Hold the arm in a safe pose and press Enter to ENABLE TORQUE (holds the present pose)...")
        s.request(rq.EnableTorque(confirmed=True))
        s.results["enable_torque"] = {"held_without_jump": ask_yes_no("Did it hold there without jumping?")}

    if not args.skip_fault_rehearsal and ask_yes_no("Run the USB-unplug fault rehearsal now (read-only)?"):
        s.request(rq.SetMode(Mode.REAL_TO_SIM))
        print(
            "\nUNPLUG the USB cable of the servo bus NOW (motors keep their last state). "
            "Waiting up to 30 s..."
        )
        snap = s.tick_for(30.0, lambda sn: sn.connection.value == "FAULT_LATCHED")
        latched = snap.connection.value == "FAULT_LATCHED"
        reads_after = s.c.last_success_read_at
        time.sleep(1.0)
        s.tick_for(2.0)  # nothing must happen: no bus calls while latched
        s.results["fault"] = {
            "latched": latched,
            "code": snap.fault.code if snap.fault else None,
            "torque_reported": snap.torque.value,
            "no_reads_after_latch": s.c.last_success_read_at == reads_after,
        }
        print("Plug the cable back in. The app must NOT recover by itself.")
        input("Press Enter after replugging...")
        s.tick_for(2.0)
        s.results["fault"]["still_latched_after_replug"] = s.c.snapshot().connection.value == "FAULT_LATCHED"
    s.results["normal_stop_to_rest"] = "NOT RUN — feature intentionally absent until T-039"
    s.finish()
    return 0


if __name__ == "__main__":
    guarded_main(main)

#!/usr/bin/env python3
"""B03 — small movement of ONE arm joint at a time, through the full safety pipeline. Operator-run only."""

from __future__ import annotations

from _common import Session, ask_yes_no, banner, base_parser, guarded_main, refuse, require_operator
from _motion import arm, move_joint

ARM = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")
MAX_DELTA = 15.0


def main() -> int:
    p = base_parser(__doc__ or "")
    p.add_argument("--joint", choices=ARM, required=True)
    p.add_argument("--delta", type=float, default=5.0, help=f"degrees, |delta| <= {MAX_DELTA} (default 5)")
    args = p.parse_args()
    require_operator(args)
    if not 0 < abs(args.delta) <= MAX_DELTA:
        refuse(f"--delta must be within ±{MAX_DELTA}° and non-zero")
    banner(
        "B03",
        f"One-joint small move: {args.joint} by {args.delta:+.1f}°",
        preconditions=[
            "B01 and B02 passed",
            f"clear space around {args.joint} for ±{abs(args.delta) + 5:.0f}°",
        ],
        actions=[
            "connect (CONNECT), enter SIM_TO_REAL, enable movement (MOVE) — sliders start at measured pose",
            f"request measured {args.joint} {args.delta:+.1f}° (inside calibrated range −5° margin)",
            "print requested / staged / sent / actual every tick (fixed policy: forwarded directly)",
            "ask you to confirm the physical direction, then return to the start pose and HOLD",
        ],
        expected=[
            "smooth small motion of ONLY that joint; arrival within ±1.5° in a few seconds",
            "sent == staged == requested (within the calibrated range); actual converges on it",
            "the 3D model (in the GUI, B02) and the arm agree on direction",
        ],
        on_failure=[
            "Ctrl-C = best-effort Emergency Hold; cut power if motion is unsafe",
            "wrong direction: record it (joint_sign) — do NOT continue with other joints",
            "no arrival / FAULT: note the printed telemetry; check power and load",
        ],
    )
    s = Session(args, f"B03_{args.joint}")
    if not s.connect() or not arm(s):
        s.finish()
        return 1
    start = s.c.snapshot().actual.get(args.joint)
    assert start is not None
    lo, hi = s.bounds.limits[args.joint].lower, s.bounds.limits[args.joint].upper
    target = start + args.delta
    if not lo <= target <= hi:
        print(
            f"Target {target:+.1f}° is outside the allowed range [{lo:+.1f}, {hi:+.1f}] — choose the "
            "other direction or a smaller delta. Holding and disconnecting."
        )
        s.emergency_hold()
        s.finish()
        return 1
    out = move_joint(s, args.joint, target, tolerance=1.5)
    s.results["move"] = out
    s.results["operator_direction_ok"] = ask_yes_no(
        f"Did {args.joint} physically move in the direction you expected for {args.delta:+.1f}°?"
    )
    if out["state"] == "ARMED":
        s.results["return"] = move_joint(s, args.joint, start, tolerance=1.5, verbose=False)
    s.emergency_hold()
    s.results["pass"] = bool(out["arrived"] and s.results["operator_direction_ok"])
    print(
        f"\nRESULT: {'PASS' if s.results['pass'] else 'FAIL'}  (arrived={out['arrived']}, "
        f"max staged step={out['max_step_staged']:.2f}°)"
    )
    s.finish()
    return 0 if s.results["pass"] else 1


if __name__ == "__main__":
    guarded_main(main)

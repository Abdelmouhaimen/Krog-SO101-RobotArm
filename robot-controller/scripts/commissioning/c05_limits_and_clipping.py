#!/usr/bin/env python3
"""B04 — calibrated limit (5° margin) and clipping validation for one arm joint. Operator-run only.

Request a target 20° BEYOND the allowed limit; the controller must stage exactly the limit
(calibrated range − 5°) and the arm must stop there, short of the mechanical stop. Under the fixed
M1 policy (ADR-010) the clamped target is sent directly (no step limiter, no LeRobot cap), so the
joint travels to the limit at servo speed: start this test with the joint near the limit.
"""

from __future__ import annotations

from _common import Session, ask_yes_no, banner, base_parser, confirm_word, guarded_main, require_operator
from _motion import arm, move_joint

ARM = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll")


def main() -> int:
    p = base_parser(__doc__ or "")
    p.add_argument("--joint", choices=ARM, required=True)
    p.add_argument("--side", choices=("upper", "lower"), default="upper")
    args = p.parse_args()
    require_operator(args)
    banner(
        "B04",
        f"Limits & clipping: {args.joint} toward its {args.side} limit",
        preconditions=["B03 passed for this joint", "full clearance over the joint's whole range"],
        actions=[
            "connect, SIM_TO_REAL, enable movement",
            f"request 20° beyond the {args.side} allowed limit; watch staged clip at the limit",
            "return to the start pose and hold",
        ],
        expected=[
            "staged stops exactly at the allowed limit (range_min/max reason printed)",
            "the arm stops ~5° before its calibrated end of travel, never against the stop",
        ],
        on_failure=[
            "Ctrl-C = best-effort Emergency Hold",
            "the arm hits a mechanical stop: FAIL — calibration does not match the arm; recalibrate",
        ],
    )
    s = Session(args, f"B04_{args.joint}_{args.side}")
    if not s.connect() or not arm(s):
        s.finish()
        return 1
    lim = s.bounds.limits[args.joint]
    limit = lim.upper if args.side == "upper" else lim.lower
    beyond = limit + (20.0 if args.side == "upper" else -20.0)
    start = s.c.snapshot().actual.get(args.joint)
    assert start is not None
    print(f"\nAllowed {args.side} limit: {limit:+.2f}° ({lim.source}); requesting {beyond:+.2f}°")
    if not confirm_word("The joint will travel to its limit.", "MOVE"):
        s.emergency_hold()
        s.finish()
        return 1
    out = move_joint(s, args.joint, beyond, tolerance=2.0, timeout_s=15.0)
    staged_at_limit = out["final_staged"] is not None and abs(out["final_staged"] - limit) < 1e-6
    clipped = any(r.startswith(f"{args.joint}:range_") for t in out["trace"] for r in t["reasons"])
    s.results.update(
        limit=limit,
        move=out,
        staged_at_limit=staged_at_limit,
        range_clip_reported=clipped,
        operator_stopped_before_mechanical_stop=ask_yes_no(
            "Did the joint stop clearly BEFORE its mechanical end stop?"
        ),
    )
    move_joint(s, args.joint, start, tolerance=2.0, timeout_s=15.0, verbose=False)
    s.emergency_hold()
    s.results["pass"] = bool(
        staged_at_limit and clipped and s.results["operator_stopped_before_mechanical_stop"]
    )
    print(f"\nRESULT: {'PASS' if s.results['pass'] else 'FAIL'}")
    s.finish()
    return 0 if s.results["pass"] else 1


if __name__ == "__main__":
    guarded_main(main)

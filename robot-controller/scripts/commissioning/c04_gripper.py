#!/usr/bin/env python3
"""B05 (matrix: gripper 0-100 mapping) — small gripper steps and direction check. Operator-run only."""

from __future__ import annotations

from _common import Session, ask_yes_no, banner, base_parser, guarded_main, require_operator
from _motion import arm, move_joint


def main() -> int:
    p = base_parser(__doc__ or "")
    p.add_argument(
        "--step", type=float, default=15.0, help="gripper units per test move (default 15, max 30)"
    )
    args = p.parse_args()
    require_operator(args)
    step = max(5.0, min(30.0, args.step))
    banner(
        "B05",
        "Gripper 0..100 control and direction",
        preconditions=[
            "B03 passed for at least one joint",
            "NOTHING between the gripper jaws, fingers clear",
        ],
        actions=[
            "connect, SIM_TO_REAL, enable movement",
            f"move the gripper by +{step:g} units, then back, staying inside 0..100",
            "ask whether +units physically OPENED the gripper (LeRobot convention: 0=closed, 100=open)",
        ],
        expected=[
            "gripper moves to the target; sent == staged (fixed policy: targets forwarded directly)",
            "LeRobot's own gripper current/torque limits stay in force (written by configure())",
        ],
        on_failure=[
            "Ctrl-C = best-effort Emergency Hold",
            'if +units CLOSED the gripper: set [simulation] logical_gripper_0_maps_to = "model_max" so the '
            "3D view matches (the official model files stay unchanged); report it",
            "stalling / heating: stop, Torque Off with the arm supported",
        ],
    )
    s = Session(args, "B05_gripper")
    if not s.connect() or not arm(s):
        s.finish()
        return 1
    start = s.c.snapshot().actual.get("gripper")
    assert start is not None
    target = start + step if start + step <= 100.0 else start - step
    out = move_joint(s, "gripper", target, tolerance=3.0)
    sent_equals_staged = all(
        t["sent"] is None or t["staged"] is None or abs(t["sent"] - t["staged"]) < 1e-6 for t in out["trace"]
    )
    opened = ask_yes_no(f"Did the gripper OPEN when the value went {'up' if target > start else 'down'}?")
    increasing_opens = opened if target > start else not opened
    s.results.update(
        move=out,
        sent_equals_staged=sent_equals_staged,
        increasing_value_opens=increasing_opens,
        recommended_logical_gripper_0_maps_to="model_min" if increasing_opens else "model_max",
    )
    s.results["return"] = move_joint(s, "gripper", start, tolerance=3.0, verbose=False)
    s.emergency_hold()
    s.results["pass"] = bool(out["arrived"] and sent_equals_staged)
    print(
        f"\nRESULT: {'PASS' if s.results['pass'] else 'FAIL'}; increasing value opens: {increasing_opens} -> "
        f"logical_gripper_0_maps_to = {s.results['recommended_logical_gripper_0_maps_to']!r}"
    )
    s.finish()
    return 0 if s.results["pass"] else 1


if __name__ == "__main__":
    guarded_main(main)

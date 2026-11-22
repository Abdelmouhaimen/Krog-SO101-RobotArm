"""Runaway detection for ARMED hardware motion (ADR-009). Pure function, no I/O.

Added after the 2026-11-07 hardware incident: with goal == present, the wrist_roll servo accelerated
AWAY from its goal (0.1 -> 2.3 -> 5.7 -> 9 deg/tick) and spun ~337 deg. LeRobot's measured-relative
cap re-centres on the moving present position every tick, so it followed the runaway; the 10-unit
step limit only shapes targets. Neither can detect a joint that does not obey its goal.

A joint is a runaway when, over the last control interval, it
  1. moved >= ``step`` (deg or gripper units),
  2. AWAY from BOTH the goal it was executing (sent in the previous tick) and the goal just sent,
  3. ACCELERATING: the previous interval also moved that way, no faster
     (an overshoot/coast after a fast slider reversal decelerates - observed on the shoulder), and
  4. ends >= ``error`` from both goals.
"""

from __future__ import annotations

from collections.abc import Mapping


def detect_runaway(
    prev_actual: Mapping[str, float],
    actual: Mapping[str, float],
    prev_goal: Mapping[str, float],
    new_goal: Mapping[str, float],
    last_moves: Mapping[str, float] | None,
    *,
    step: float,
    error: float,
) -> list[str]:
    bad: list[str] = []
    for name, after in actual.items():
        before = prev_actual[name]
        moved = after - before
        if abs(moved) < step:
            continue
        goals = (prev_goal[name], new_goal[name])
        away_from_all = all((g - before) == 0.0 or moved * (g - before) < 0 for g in goals)
        prev_move = last_moves.get(name, 0.0) if last_moves else 0.0
        accelerating = prev_move * moved > 0 and abs(moved) >= abs(prev_move)
        if away_from_all and accelerating and min(abs(g - after) for g in goals) >= error:
            bad.append(name)
    return bad

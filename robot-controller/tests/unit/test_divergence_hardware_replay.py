"""Tracking-divergence DIAGNOSTIC (detector of ADR-009; diagnostic-only per ADR-010) replayed against
REAL SO-101 traces recorded on 2026-11-07 (20 Hz, LeRobot 0.4.4).

* wrist_runaway: goal == present on Enable movement; wrist_roll accelerated away and spun ~337 deg
  (recorded before the guard existed). The guard must trip early, within ~10 deg of travel.
* operator_session: a real operator session (base swept across its full range, shoulder raised and
  swept fast with reversals). The first guard version falsely tripped on its last tick; the guard
  must never trip on it.
"""

from __future__ import annotations

import json
from pathlib import Path

from so101_companion.safety import policy
from so101_companion.safety.divergence import detect_runaway

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def replay(name: str) -> list[tuple[int, list[str], dict[str, float]]]:
    ticks = json.loads((FIX / name).read_text())["ticks"]
    trips = []
    last_moves = None
    for k in range(1, len(ticks)):
        prev, cur = ticks[k - 1], ticks[k]
        bad = detect_runaway(
            prev["actual"],
            cur["actual"],
            prev["sent"],
            cur["sent"],
            last_moves,
            step=policy.DIVERGENCE_STEP,
            error=policy.DIVERGENCE_ERROR,
        )
        last_moves = {j: cur["actual"][j] - prev["actual"][j] for j in cur["actual"]}
        if bad:
            trips.append((cur["seq"], bad, cur["actual"]))
    return trips


def test_real_wrist_runaway_is_caught_early() -> None:
    ticks = json.loads((FIX / "hw_20261107_wrist_runaway.json").read_text())["ticks"]
    start = ticks[0]["actual"]["wrist_roll"]
    trips = replay("hw_20261107_wrist_runaway.json")
    assert trips, "guard did not detect the recorded runaway"
    seq, joints, actual = trips[0]
    assert joints == ["wrist_roll"]
    assert seq - ticks[0]["seq"] <= 4  # within 4 ticks (200 ms) of Enable movement
    assert abs(actual["wrist_roll"] - start) <= 10.0  # vs ~337 deg actually travelled


def test_real_operator_session_never_trips() -> None:
    assert replay("hw_20261107_operator_session.json") == []

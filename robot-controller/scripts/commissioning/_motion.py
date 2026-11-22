"""Shared small-motion helper for c03/c04/c05: arm, move one joint to a target through the FULL
application safety pipeline, wait for arrival or timeout, report per-tick telemetry."""

from __future__ import annotations

import itertools
from typing import Any

from _common import Session, confirm_word


def arm(s: Session) -> bool:
    from so101_companion.control import requests as rq
    from so101_companion.domain.state import Mode
    from so101_companion.ui.dialogs import ENABLE_WARNING

    s.request(rq.SetMode(Mode.SIM_TO_REAL))
    s.tick_for(0.3)
    print("\n" + ENABLE_WARNING)
    if not confirm_word("Keep clear of the arm.", "MOVE"):
        return False
    snap = s.request(rq.EnableMovement(confirmed=True))
    return snap.connection.value == "ARMED"


def move_joint(
    s: Session, joint: str, target: float, *, tolerance: float, timeout_s: float = 6.0, verbose: bool = True
) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []
    s.set_target(**{joint: target})

    def on_tick(snap: Any) -> bool:
        trace.append(
            {k: getattr(snap, k).get(joint) for k in ("requested", "staged", "sent", "actual")}
            | {"reasons": list(snap.clamp_reasons)}
        )
        if verbose:
            print(Session.row(snap, joint))
        actual = snap.actual.get(joint)
        return (
            actual is not None
            and abs(actual - snap.staged.get(joint)) <= tolerance
            and snap.staged.get(joint) == snap.requested.get(joint)
        )

    snap = s.tick_for(timeout_s, on_tick)
    final = snap.actual.get(joint)
    return {
        "joint": joint,
        "target": target,
        "final_actual": final,
        "final_staged": snap.staged.get(joint),
        "arrived": final is not None and abs(final - target) <= tolerance,
        "state": snap.connection.value,
        "max_step_staged": max(
            (
                abs(b["staged"] - a["staged"])
                for a, b in itertools.pairwise(trace)
                if a["staged"] is not None and b["staged"] is not None
            ),
            default=0.0,
        ),
        "trace": trace,
    }

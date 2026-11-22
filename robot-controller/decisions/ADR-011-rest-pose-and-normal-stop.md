# ADR-011 — Recorded physical rest pose and Normal Stop (T-039)

**Status:** Accepted 2026-11-07. Parameters decided by the owner; implemented and tested offline.
**Real execution 2026-11-07 (owner-reviewed):** recorded the rest pose (3 joints clamped); two STOP
returns ran smoothly at 30°/s and held. Both reported `rest_not_reached` because elbow_flex settled
3.4° from its target (tolerance ±3°), from gravity load with LeRobot's P=16 at 5 V. The owner
accepted the behaviour; the tolerance is unchanged pending an explicit decision.

## Behaviour

| | |
|---|---|
| **Record rest pose…** | Confirmed action, available when connected with a valid calibration. Reads a **fresh measured pose**; joints inside the 5° margin are **clamped to the allowed bound** (owner decision; the clamped joints are reported). Saved to `config/rest_pose.<robot_id>.json` (gitignored), keyed by the LeRobot calibration id. A pose is never used with a different calibration, and is reloaded on connect only if it lies inside the current calibrated bounds. Nothing moves. |
| **STOP (Normal Stop)** | Available only in SIM_TO_REAL + ARMED + torque ON + recorded pose. Plans a **straight joint-space trajectory** from a fresh measurement to the rest pose, at **30°/s (gripper 30 units/s)**, all joints arriving together (minimum 0.5 s). State `RETURNING_TO_REST`: each tick sends the interpolated target through the normal path (calibrated bounds, direct LeRobot send). Sliders have no authority. |
| Arrival | All arm joints within **±3°** and the gripper within **±5 units** after the trajectory ends → `HOLDING` at the rest pose, **torque stays on**, event `rest_reached`. |
| Timeout | Not arrived within **trajectory duration + 3 s** → hold at the measured pose (goal := present), `HOLDING`, event `rest_not_reached` with per-joint errors. Not a fault. |
| Abort | Emergency Hold → `HOLDING` at the measured pose. Torque Off → limp. Disarm or a mode change → read-only. Communication faults latch as usual. |
| Distinctness | STOP (blue), EMERGENCY HOLD (red) and Torque OFF are separate controls with separate semantics. |

## Why a trajectory and not a limiter

ADR-010 removed all generic limiters; smoothness is the caller's job. The return to rest is such a
caller: a fixed, timed trajectory owned by this one feature, not a global restriction. Its constants
live in `safety/policy.py` and are not configurable.

## Tests

- `tests/unit/test_rest_pose.py`: store round trip and identity, corrupt or foreign files,
  synchronized 30°/s trajectory, minimum duration, tolerances.
- `tests/integration/test_controller_hardware.py`:
  - record + clamp + persist;
  - return + arrival + hold (per-tick step ≤ 1.5°, sliders ignored);
  - timeout;
  - Emergency Hold and Torque Off abort;
  - reload only for the same calibration;
  - STOP unavailable without a recorded pose.
- `tests/gui/test_main_window.py`: record → STOP button → HOLDING.

# ADR-009 — wrist_roll runaway incident (2026-11-07) and the tracking-divergence detector

**Status:** Accepted 2026-11-07. The detector's *automatic reaction* was **withdrawn by ADR-010**.
It now runs as a diagnostic only.

## Incident (observed on the real SO-101, LeRobot 0.4.4, 20 Hz)

- **Before the incident:** in a REAL_TO_SIM session with torque OFF, the operator twisted the wrist
  by hand through its encoder seam many times (11 crossings of ±180° in the log).
- **Trigger:** later, *Enable movement* sent `goal == present` for all six joints (wrist_roll −100.88°).
- **What happened:** the wrist_roll servo **accelerated away from that goal** (0.1 → 2.3 → 5.7 →
  9 → 10 °/tick). It spun about **337°** in about 2.5 s, then stopped 23° short. Other joints did
  not move.
- **Why nothing stopped it:** at that time LeRobot's `max_relative_target` (20°) was active.
  It re-centred on the moving present position every tick, so it *followed* the runaway. The
  10-unit step limiter only shapes targets, which were constant. Emergency Hold, sent manually
  afterwards, held.
- **Diagnosis (read-only register dump afterwards):** all registers match the calibration file:
  Homing_Offset −669, limits 0..4095, mode 0, Status 0, 27–29 °C, 5.1–5.2 V supply. The most
  consistent explanation is that the servo kept position across turns after the hand rotations.
  The motion was not reproducible: re-arming afterwards in the same pose produced 0.09° maximum
  deviation over 62 ticks. **Root cause not proven.**
- **Operator guidance:** avoid hand-twisting wrist_roll more than half a turn while torque is off.

## Detector (`safety/divergence.py`, pure function)

A joint is flagged when, over one control interval, it:
1. moved ≥ 4° (or 4 gripper units),
2. **away from both** the goal it was executing (sent in the previous tick) and the goal just sent,
3. **while accelerating** in that direction (the previous interval moved the same way, no faster),
4. and ends ≥ 6° from both goals.

Rules 2–3 come from a second hardware observation: during a fast slider reversal, the shoulder
coasted about 6° in its old direction for one tick. The first detector version tripped falsely
on that.

**Validation on real recorded data** (`tests/fixtures/hw_20261107_*.json`,
`tests/unit/test_divergence_hardware_replay.py`):
- The wrist runaway is flagged **3 ticks (150 ms) after Enable, after 8° of travel**.
- The full operator session (base swept across its range, fast shoulder reversals) is **never**
  flagged.

## Reaction

- **Original decision (this ADR):** latch `TRACKING_DIVERGENCE` and disable torque on the
  offending joint only.
- **Superseded the same day by ADR-010:** the owner chose a fixed policy without automatic torque
  actions. The detector now only emits a `tracking_divergence` event and shows an orange UI
  warning. Stopping is a manual Emergency Hold / Torque Off decision.

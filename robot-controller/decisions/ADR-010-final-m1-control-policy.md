# ADR-010 — FINAL Milestone 1 control and safety policy (fixed, not configurable)

**Status:** Accepted 2026-11-07 by the project owner after supervised hardware commissioning.
**Supersedes:**
- ADR-002's 10-units-per-tick step limiter and its `max_relative_target = {20° ×5, 100}`.
- spec FR-07 and the cap part of FR-08.
- ADR-007 §1's interaction with the step limiter (the bounds ratchet itself is kept).
- ADR-009's automatic reaction.

**Implemented in:** `src/so101_companion/safety/policy.py` (single source of truth).

## Policy

| Aspect | Fixed behaviour |
|---|---|
| Position path | requested → finite six-joint validation → **calibrated bounds** (5° margin on the five arm joints; gripper 0..100) → sent **directly** through LeRobot `send_action` |
| Step limiter | **none** (the SO-101-VR-Control 10-unit/tick limiter is removed) |
| LeRobot cap | `max_relative_target=None` (no measured-relative clipping; `send_action` does no extra present-position read) |
| Servo settings | LeRobot `configure()` defaults unchanged (mode, PID 16/0/32, gripper protections 500/250/25) |
| Speed | limited only by the servos (and power supply); movement is never instantaneous |
| Communication faults | latched; no command until manual, confirmed recovery; nothing resumes automatically |
| Emergency Hold / Torque Off | manual, separate operator actions (unchanged) |
| Tracking divergence | **diagnostic only**: `tracking_divergence` event + orange UI warning; no fault, no torque change |
| Disconnect | LeRobot default `disable_torque_on_disconnect=True` (fixed); the UI warns first |
| wrist_roll usable range | **±163°** (after the owner-approved re-centring, ADR-012), intersected with the calibrated bounds for hardware and the slider/simulation bounds. LeRobot calibrates wrist_roll as a full turn, so the calibration alone gave ±175°. Valid for the current calibration (motor 5 Homing_Offset 381); re-measure after any recalibration. |
| Bounds ratchet | kept: a joint already inside the 5° margin is never pushed further out and never pulled in just because movement was enabled |
| Configurability | **none**: a `[safety]` section or `hardware.disable_torque_on_disconnect` in a config file is rejected |

## Consequences (accepted by the owner)

- **Every target goes straight to the servos.** A large slider drag or a large consecutive request
  moves the joint at full servo speed. Smoothness and timing are the caller's responsibility; the
  M2 Behavior Engine will generate timed trajectories.
- **A servo runaway like the one in ADR-009 is not stopped automatically.** It is detected and
  displayed, and the operator must press Emergency Hold or Torque Off.
- **Large real-arm movements need their own supervised physical test** before routine use.
  Large movements are tested in simulation first (`test_large_jump_reaches_target_in_one_tick_in_simulation`,
  `test_consecutive_large_requests_are_forwarded_tick_by_tick`).
- **The normal Stop → return-to-rest motion still needs a smooth, timed return** (M1 Definition of
  Done item 8). It must be implemented as an explicit trajectory, not by re-adding a limiter.

## Validation

- **Replaced tests:** the old ones expected 10°/tick staging or the 20° relative clip.
- **New tests cover:**
  - direct forwarding: sim, fake hardware and real LeRobot code via the driver contract (`sync_write`
    only, no extra read);
  - consecutive large requests;
  - calibrated-bounds clamping;
  - distinct requested/staged/sent/actual stages;
  - the fixed-policy constants, and rejection of safety configuration;
  - diagnostic-only divergence;
  - unchanged fault latching.

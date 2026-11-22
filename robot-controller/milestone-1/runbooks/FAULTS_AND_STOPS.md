# Fault handling, stopping and authority — operator contract

| Situation | Application behavior | What is *not* guaranteed |
|---|---|---|
| Offline/Sim paused | Stop advancing simulated staged target; maintain last pose. | Not a physical emergency stop. |
| Healthy Emergency Hold while real arm enabled | Stop processing new slider requests; read current positions; request hold at measured joint values subject to existing LeRobot safety; show HOLDING if send succeeds. | Zero latency or instant physical halt. |
| USB read/write fails | Clear or freeze requested motion queue, latch FAULT, stop new sends, stale display, require manual recovery; don't blindly torque-toggle. | Motor power/torque status, instant stop. |
| User selects Torque Off | Warn about falling arm, require explicit confirmation, call only verified driver torque-control operation if link available. | Safe supported gravity-free pose. |
| User selects voluntary Disconnect | Warn that LeRobot can disable torque on disconnect; stop sends, close in controlled sequence. | Preservation of torque after bus is closed. |
| User closes GUI while motors engaged | Demand explicit safe shutdown choice; disclose torque/disconnect effects; log outcome. | Hardware holds pose if power/torque is removed. |
| User wants normal Stop-to-Rest | Feature unavailable in Gate A until real measured pose saved and later human-authorized implementation. | Any imaginary safe home position. |

**Fault recovery steps:** freeze target progression; inspect and support arm; check physical power/USB/supply; explicitly acknowledge fault; reinitialize/validate calibration and physical surroundings if needed; stay read-only until operator separately arms movement. **No auto reconnect, no auto resume of old queued gesture.**

Emergency Hold and Torque Off buttons must not imply safety certification or a physical emergency stop. Physical emergency hardware and manual supervision are outside the software's guarantee.

## As implemented in M1 code (2026-11-06; offline-tested, hardware behaviour NOT yet verified)

| Situation | Implementation | Test evidence |
|---|---|---|
| Sim pause | `Pause`/`Resume` (SIM_ONLY only) freeze staged progression | `test_pause_freezes_progression_and_resume_continues` |
| Emergency Hold | observe → `send(measured)` → `HOLDING`; intent discarded; no further sends until explicit re-enable; failure → `HOLD_FAILED` fault; also attemptable while `FAULT_LATCHED` (fault stays latched) | `test_emergency_hold_healthy`, `test_emergency_hold_on_dead_bus_reports_failure` |
| USB read/write/telemetry failure | `FAULT_LATCHED`, intent cleared, **no further bus calls**, ACTUAL marked STALE, torque `UNKNOWN`, no reconnect/resume; a later good read does not clear it | `test_read_failure_latches_and_freezes_everything`, `test_write_failure_keeps_previous_sent_and_never_fabricates_actual` |
| Torque Off | typed/dialog confirmation → `bus.disable_torque(num_retry=5)` → `TORQUE_OFF`; failure → fault, torque `UNKNOWN` | `test_torque_off_only_after_confirmation`, driver contract tests |
| Enable torque | goal := present, then `bus.enable_torque()` → `CONNECTED_READ_ONLY` (ADR-007 §5) | `test_enable_torque_holds_present_pose` |
| Manual recovery | confirmed; one fresh read (+ calibration re-check); → read-only; torque stays `UNKNOWN` until Torque Off → Enable torque | `test_manual_recovery_requires_confirmation_and_fresh_read` |
| Disconnect / quit | confirmation stating `disable_torque_on_disconnect`; quit while connected asks first; torque shown OFF/UNKNOWN truthfully | `test_disconnect_reports_torque_consequence`, `test_disconnect_and_quit_warn_about_torque` |
| Stop-to-Rest | **absent** (no action, button or request type) | `test_no_rest_pose_or_stop_to_rest_anywhere`, `test_no_stop_to_rest_control` |
| Tracking divergence (joint accelerating away from its goal, ADR-009) | **Diagnostic only** (ADR-010): `tracking_divergence` event + orange UI warning; no fault, no torque change, sending continues | `test_runaway_is_reported_as_diagnostic_only`, `test_divergence_hardware_replay.py` (real traces) |

**Final M1 control policy (ADR-010, 2026-11-07):** targets are clamped to the calibrated bounds and sent
directly (no step limiter, `max_relative_target=None`). Nothing limits speed except the servos, so the
operator stops misbehaviour manually with EMERGENCY HOLD or Torque OFF.

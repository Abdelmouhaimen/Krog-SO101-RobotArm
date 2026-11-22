# ADR-007 — Defensive checks added beyond LeRobot / SO-101-VR-Control, and state-machine details

> **2026-11-07:** the "inherited" step limiter and `max_relative_target` rows below were removed by ADR-010 (fixed final policy). All other items in this ADR remain in force.

**Status:** Accepted by implementing agent 2026-11-06 (P2–P4). Each item is either required by the
approved spec or essential for correct operation; none adds a new control algorithm, measured-position
limiter, torque/PID/voltage control, ground protection, IK or VR.

## Inherited unchanged (not re-implemented)

| Mechanism | Owner |
|---|---|
| Measured-relative cap 20° ×5 / 100 gripper | LeRobot `max_relative_target` (native dict) |
| Gripper torque/current/overload protections | LeRobot `configure()` |
| Servo position control, PID | STS3215 firmware via LeRobot |
| 10 units per issued tick step limit (all six joints) | SO-101-VR-Control math, adapted as a pure function (`safety/step_limit.py`) |
| Calibrated range ± 5° | SO-101-VR-Control formula, verified against LeRobot normalization (ADR-006) |

## Additions, purpose and difference from the sources

1. **No-outward-ratchet range clamp** (`JointBounds.clamp(hold_outside=staged)`).
   *Problem:* a real rest pose usually sits against mechanical stops, i.e. inside the 5° margin. Plain
   clamping (what VR-Control does) would make the arm move up to 5° the moment movement is enabled,
   without any slider input. *Rule:* if a joint is already outside its allowed range, the range is widened
   just enough to include where it is: it may stay or move inward, never further outward. Once inside,
   the normal range applies. *Difference:* VR-Control clamps unconditionally.
2. **Calibration register check after connect** (`SOFollower.is_calibrated`, read-only). LeRobot 0.4.4
   ignores a mismatch when `calibrate=False`, so normalization would silently use wrong ranges. Mismatch →
   `FAULT_LATCHED/CALIBRATION_MISMATCH`, no motion, no automatic calibration.
3. **Calibration file schema validation** before any driver import (six motors, ids 1–6, integer fields,
   0 ≤ min < max ≤ 4095, |homing_offset| ≤ 2047, span ≥ 20 ticks). VR-Control falls back to URDF limits.
4. **Enable movement starts from the measured pose** (requested = staged = actual), so arming causes no
   jump. VR-Control starts from its last commanded angles.
5. **Enable torque = goal := present, then torque on** (`bus.sync_read` → `bus.sync_write(Goal_Position)`
   → `bus.enable_torque()`): prevents a snap to a stale goal after the arm was moved by hand with torque
   off. Uses only verified bus APIs; physical behaviour is Gate B (B07).
6. **No implicit torque writes after a fault.** The adapter keeps abandoned LeRobot objects referenced
   (`_PARKED`) so `Robot.__del__` cannot fire a torque-disable mid-session; a failed connect closes the
   port with `bus.disconnect(disable_torque=False)`. *Limitation:* at interpreter exit LeRobot's destructor
   may still run `disconnect()`; the scripts/UI tell the operator to support the arm.
7. **Torque state tri-state.** After any comm fault torque is `UNKNOWN`; `Enable movement` requires a
   known `ON`, so after recovery the operator must do Torque Off → Enable torque.
8. **Recovery semantics.** Manual, confirmed, performs exactly one read (plus a calibration-register
   re-check for `CALIBRATION_MISMATCH`). It never reconnects, never re-arms, never changes torque. After a
   USB unplug the port handle is dead, so recovery fails and the documented path is Disconnect + Connect.
9. **No bus activity while `FAULT_LATCHED`** (no polling) except explicit operator actions: Emergency
   Hold attempt, Torque Off, Recover, Disconnect.
10. **Emergency Hold also offered while `FAULT_LATCHED`** (best effort; a failure is reported as
    `HOLD_FAILED`; a success does not clear the fault).
11. **SIM_ONLY never polls a connected robot.** Hardware data flows only in REAL_TO_SIM / SIM_TO_REAL.
12. **LeRobot version allow-list** (`0.4.4`): an unverified driver version is refused before connecting.
13. **Device guard** (`diagnostics/device_guard.py`): blocks serial opens and `/dev` enumeration until
    the operator confirms Connect; the confirmed connect path disables it for the process. It is a
    development safety net, not a security boundary.
14. **Config rejects** `hardware_autoconnect/autoenable=true` and `mode_on_start != SIM_ONLY`; the CLI
    refuses `--mode real-to-sim|sim-to-real`.
15. **`--fake-hardware` demo mode** (FakeRobot, generated demo calibration, port `/dev/FAKE-so101-demo`,
    window title says FAKE) for offline rehearsal. The fake *emulates* LeRobot's cap only so offline tests
    can show `sent ≠ staged`; it is never used with real hardware.
16. **Port must be an explicit `/dev/...` path**; no enumeration in the GUI; commissioning scripts list
    `/dev/serial/by-id` only when the operator runs them and never pick one automatically.

## State machine (as implemented)

`mode ∈ {SIM_ONLY, REAL_TO_SIM, SIM_TO_REAL}` × `connection ∈ {DISCONNECTED, CONNECTING,
CONNECTED_READ_ONLY, ARMED, HOLDING, FAULT_LATCHED, TORQUE_OFF}`. Every operator action is permitted only
if it is in `available_actions()` (the UI enables buttons from the same set; tests assert both).
Leaving SIM_TO_REAL disarms. Connecting never changes the mode. Disconnect returns to SIM_ONLY.

# M1 specification — Robot ↔ MuJoCo foundation

**Status:** APPROVED by user interview rounds 1–4, 2026-11-06. **Implementation:** NOT STARTED.

## Problem and desired outcome

Build an SO-101 robot companion's low-level foundation independent of game/agent/gestures. It should be possible to interactively position six logical joints in a visual simulator, show the physical robot's measured pose in the same simulation after hardware arrives, and send safe joint-position commands from the same UI to the real robot. Give a coding agent reliable offline tests and a later human-operated hardware commissioning procedure.

## Personas / scenarios

- **Developer without hardware** launches one integrated window, moves six sliders, sees the official MuJoCo model follow, inspects constraint diagnostics and simulated positions, and runs offline tests with no USB.
- **Operator connecting real arm later** confirms that LeRobot setup may temporarily disable torque, loads existing calibration, sees actual positions without sending motion commands, then explicitly enables movement.
- **Operator executing real motion later** drags a slider, sees desired target, staged target after 10-unit step clamp, position actually returned by LeRobot after 20° per-arm measurement-relative clamp, current measured angle and visual mirror. A failed read/write latches error and stops new movement.

## Normative functional requirements

| ID | Requirement |
|---|---|
| FR-01 | One follower robot with exactly six logical joint names: `shoulder_pan`, `shoulder_lift`, `elbow_flex`, `wrist_flex`, `wrist_roll`, `gripper`. |
| FR-02 | Single desktop window must contain full 3D MuJoCo visual, six continuously manipulable joint sliders, numeric requested/staged/sent/actual fields and mode/connection/authority/fault indicators. |
| FR-03 | Ubuntu 24.04 installation and launch documented; program may run **SIM_ONLY** with no serial port, calibration file or hardware. |
| FR-04 | Official SO-101 `scene.xml` and `so101_new_calib.xml` used **unmodified**, with meshes resolved. Physical pose mapping adapter is allowed; no physical alignment wizard required. |
| FR-05 | Support `SIM_ONLY`, `REAL_TO_SIM` (read-only observation mirrored to viewer), and `SIM_TO_REAL` (UI target → safety → LeRobot → feedback/viewer). Modes mutually exclusive and intentionally entered. |
| FR-06 | Use LeRobot `SOFollower` and `send_action`/`get_observation`; all motor commands are *position targets* in degrees for arm and 0–100 for gripper. Do not send voltage, raw torque, current or PID output. |
| FR-07 | At nominal 20 Hz, apply 10 units max delta per **issued control tick** relative to previous staged target for all six joints. Display actual measured tick frequency. No catch-up bursts after delayed ticks. **[SUPERSEDED 2026-11-07 by ADR-010: no step limiter; targets clamped to calibrated bounds and sent directly.]** |
| FR-08 | Configure native LeRobot `max_relative_target` dictionary: 20.0 for five arm joints, 100.0 for gripper (effectively no additional measured-position cap within logical 0–100 range). Setting is configurable and validated. Return-value of `send_action` is the source of `SENT` values. **[Cap part SUPERSEDED 2026-11-07 by ADR-010: `max_relative_target=None`, not configurable; SENT still = `send_action` return value.]** |
| FR-09 | Enforce known legal ranges for all joints. For real arm, derive reliable angular bounds from existing verified LeRobot calibration, apply 5° safety margin to **five** angular joints; no hardware command if calibration absent/invalid. Gripper bound 0–100; no 5° gripper margin. Sim-only uses model ranges as provisional. |
| FR-10 | User can operate a slider while movement is enabled; slider changes are **coalesced** into scheduled commands instead of sending one motor write per UI event. |
| FR-11 | Connection startup and LeRobot `connect/configure` require distinct explicit user confirmation, warning about potential torque changes. Connecting alone is **not** authorization to send target positions; operator must separately arm/enable movement. |
| FR-12 | Diagnostic panel distinguishes requested, staged (after limits), returned sent (after LeRobot), actual (hardware or fake feedback), and simulated displayed values; stale/unavailable values marked clearly. Never replace failed measured feedback with target. |
| FR-13 | Communication failure latches `FAULT_LATCHED`, stops issuing new movements, reports error/time/status, preserves last-known torque state **without toggling it blindly**, disallows automatic reconnect or auto-resume; manual recovery with confirmation is required. |
| FR-14 | Provide **Emergency Hold** (best effort: send present-position target if feedback/bus healthy, otherwise latch failure) and **Torque Off** (separate explicit confirmation; hardware supports it if verified; may let robot fall). Explain neither equals physical emergency stop. |
| FR-15 | **No working rest-pose functionality until hardware is available.** No built-in/fake rest pose or exposed Stop-to-Rest action during Gate A. Later scope includes recording measured pose, safe transition, verified arrival and holding torque. |
| FR-16 | Progressive user-run scripts must exist for future `read-only diagnostic → one joint → gripper → mirror/control`, with explicit opt-in and no CI/live automatic execution. |
| FR-17 | Application logs structured events with monotonic timestamps, command sequence ID, mode, gate/clip reason, last successful read and sent timestamps, and errors; allow export of logs without secrets. |
| FR-18 | Codebase is a clean independent Python project; reuse relevant LeRobot/VR logic, not their VR dashboard, IK, ground protection or application entry point. |

## Safety / integrity requirements

| ID | Requirement |
|---|---|
| SR-01 | Hardware movement is impossible before connection **and** explicit movement enable **and** verified calibration. |
| SR-02 | Disallow NaN, infinity, unknown/missing/extra motor keys and incompatible units; fail closed before hardware write. |
| SR-03 | Enforce accurate controller state machine, session/mode changes, no second writer, fault latching and no auto restart. |
| SR-04 | If motor bus read/write times out/errors during actuation, command queue is cleared, no new requests sent, UI fault latched. Torque state described as unknown unless known; no guaranteed software stop under link failure. |
| SR-05 | A voluntary disconnect and application close show verified LeRobot disconnect/torque consequences. Do not silently toggle torque in an exception handler. |
| SR-06 | Sim-only cannot open serial ports and no tests trigger motor movement. Mock adapters fully cover fault and clip logic. |
| SR-07 | A safety check must operate in the same units/conventions as the data being checked; preserve 5° margin only on arm joint angles, not gripper normalized values. |
| SR-08 | The program must never automatically calibrate the real robot. Calling `SOFollower.connect(calibrate=False)` alone does not substitute calibration validity checks. |

## UX and performance criteria

| ID | Requirement |
|---|---|
| UX-01 | Exactly one primary top-level application window; no detached MuJoCo viewer; fully integrated 3D and controls. |
| UX-02 | Six sliders respond to drag and display units; cannot accidentally control real hardware in sim or read-only mode. |
| UX-03 | UI thread remains responsive during blocked serial operations using a serialized worker/queue; no concurrent calls to one servo bus. |
| UX-04 | A nominal 20 Hz command loop is targeted and real measured rate displayed; 20 Hz is not an unconditional hardware guarantee. |
| UX-05 | Visual refresh target ≥15 fps under normal supported desktop conditions, measured in P0; degrade visibly and log if backend slow. No false claim without benchmark. |
| UX-06 | Simulator displays actual measured physical position in mirror modes; requested target isn't confused with measured pose. |

## Nonfunctional constraints

- Python (version to be pinned after compatibility check), Ubuntu 24.04 desktop, MuJoCo, PySide6 (provisional GUI library), LeRobot as hardware driver.
- Prove Qt/MuJoCo context compatibility first. Use upstream official model/meshes without editing their assets. Record upstream SHA, licenses and installation.
- The visual preview may use kinematic `qpos`/`mj_forward` rather than high-fidelity dynamic simulation. Explicitly label as **pose preview**, not motor torque simulation.
- Separate GUI, controller, safety, LeRobot device, MuJoCo scene mapping and diagnostic logging in testable modules.
- No VR, IK, Cartesian targeting, ground-plane collision protection, physics-accurate torque/forces, normal rest pose (until later), behavior engine, Valorant streaming, agent/MCP, recording, physical calibration wizard, multi-arm.

## Delivery gates

- **Gate A / software offline (NOW):** All relevant FR/SR/UX implemented and tested via fake robot, non-hardware simulator, and native single-window GUI; rest and real physical movements explicitly unavailable/pending. See test matrix.
- **Gate B / hardware validation (LATER):** User approves and runs progressive checks; no coding assistant self-certifies physical success.

## Definition of done

No requirement may be marked done without a specific test/inspection result. Every confirmed and unconfirmed aspect must be visible in `IMPLEMENTATION_STATUS.md`. The complete Gate A is independent of the physical hardware, but Gate B can remain pending indefinitely without misreporting completion.

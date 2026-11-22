# Project context and scope continuity

## Vision
A physical, expressive SO-101 companion sits on a desk near the user's gaming setup. In the future an AI observes live Valorant gameplay, decides reactions and speaks. A behavior engine translates high-level reactions (celebrate, wave, talk via gripper, disappointment) into safe robot joint trajectories. Later, optional 3D monitor localization permits screen-aware spatial gestures. The AI never needs raw motor control.

## Milestone ordering (immutable without explicit user decision)
- **M1 — Robot ↔ MuJoCo foundation (NOW):** one clean, joint-space control interface and a single-window visual UI; sim-only, physical read mirror, physical execution; established LeRobot/VR-control safety mechanisms; no hardware test requirement for software delivery.
- **M2 — Behavior library (LATER):** manual demonstrations and recording of measured joint positions; edit, preview, safely replay; speech-like gripper animation and gestures; exact methods/timing/format require a separate interview.
- **M3 — Gameplay agent (LATER):** audio/video input, behavior-level function calls/MCP, speech and expressive physical reactions; user must be interviewed on architecture, autonomy, safety, latency and privacy.
- **Future:** spatial mapping of physical screen into robot 3D coordinates. No M1 UI or backend complexity dedicated to this.

## Interview decisions: Round 1
1. One follower arm.
2. OS initially Windows, **subsequently superseded by Ubuntu 24.04**.
3. User previously ran SO-101-VR-Control successfully with the physical arm.
4. New clean project, adapting relevant source communication/safety rather than forking all VR modules.
5. Interactive sliders and MuJoCo viewer.
6. Modes: simulation-only, real-to-sim, simulation-to-real.
7. Prefer visual pose/synchronization over realistic actuator physics (later clarified explicitly).
8. Same existing safety *approach* but make validation/fault handling robust.
9. Real arm will be clamped/fixed on desk.
10. Normal Stop intended to return to a physical saved rest pose; later clarified **defer functionality until hardware is present**.
11. Coding agent initially Codex; later **agent neutral** (Codex and Claude Code).
12. Additional: change LeRobot measured-position cap to configurable 20° default; **exclude all ground protection**.

## Interview decisions: Round 2
1. Native Python (now Ubuntu 24.04, not Windows).
2. On connection, no movement command until user explicitly enables it.
3. Dragging slider continuously commands targets while enabled.
4. All six joints incl. gripper.
5. 20° measured-position target cap for five arm joints, **no effective cap for gripper**; use native LeRobot per-motor dictionary with gripper limit 100.
6. Physical rest pose is captured once from actual measured arm position, **later only**.
7. Provide Emergency Hold plus a separate explicit Torque Off.
8. Existing LeRobot calibration required: no fallbacks for real actuation.
9. Additional: no invented actuator safeguards when built-in LeRobot/VR repo behavior suffices.

## Interview decisions: Round 3
1. Agent-neutral package for Claude Code / Codex.
2. Ubuntu 24.04 desktop.
3. Official MuJoCo SO-101 model unchanged; user visually evaluates fit, no mandatory physical alignment procedure.
4. Explicit confirmation before LeRobot connect/configuration, which can change torque/operating modes.
5. Following eventual real return-to-rest, retain torque/hold pose.
6. UI: 3D, six sliders, requested/sent/actual positions, connection status.
7. Progressive user-run hardware scripts after code is prepared: read diagnostics → one joint → gripper → full sync.
8. Hardware currently **not available**; Claude Code may have host tools in future, but code development is offline for now. Never assume sandboxing as a security control.

## Interview decisions: Round 4 (final)
1. ONE integrated window, not a detached MuJoCo viewer plus separate panel.
2. USB error → stop issuing movement commands, latch fault, preserve last known torque state where possible, require manual recovery; no auto-resume/reconnect.
3. **No rest-pose functionality until the physical arm is available**; no fake simulation rest pose and no placeholder normal return-to-rest as a working button.

## Important nuance: two different 10/20 limits
- VR source's ~10-unit-per-control-update **step limiter** bounds delta from prior software-commanded joint position. At nominal 20 Hz it permits up to ~200°/s commanded speed, NOT measured speed, and includes gripper units (10/100 per step). Application should not artificially accumulate missed steps.
- LeRobot's `max_relative_target` caps **distance from current measured position to the target sent to servo**. Ours defaults to 20 degrees for each arm joint, and 100 for the gripper (effectively no additional cap within its normalized range). It incurs a read, can lead to extra lag, and must be logged as a separate stage.

## M1 exclusions
NO VR, hand tracking, IK, Cartesian control, floor/ground protection, speech, gameplay streaming, behavior recording/editing, agent/MCP, trained policies, physics-accurate servo torque model, multi-arm setup, model editing, calibration wizard, automated real rest pose capture before the arm arrives, automatic recovery after loss of USB.

## References
- [VR source robot_interface.py](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py)
- [LeRobot SOFollower](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/so_follower.py)
- [SO-101 simulation README](https://github.com/TheRobotStudio/SO-ARM100/blob/main/Simulation/SO101/README.md)

# Engineering constitution — non-negotiable

**Safety before cleverness.** No application startup, GUI construction, import, unit test, rendering, or model load may open a live serial port or command a physical motor. Hardware connection is behind a separately confirmed user action. Sending commands is further gated by an explicit enable/arm action.

**Use proven low-level control.** LeRobot `SOFollower` owns hardware `Present_Position`/`Goal_Position` and servo configuration. Preserve its built-in clipping via `SOFollowerConfig.max_relative_target`. Port/adapt only small, reviewed, unit-tested joint-space 10-unit per-step logic and calibrated-range methodology from SO-101-VR-Control. No new ad-hoc PID, voltage, force, acceleration or torque implementation.

**Protect limits without introducing rejected features.** Enforce five calibrated-joint angular ranges with 5° margins and gripper 0–100. Real movement fails closed if calibration is unavailable/invalid, with no silent URDF-only fallback. No floor-height check, no ground-collision logic, no IK, no VR. Safety is not a license to add unwanted functionality.

**Explicit authority boundaries.** UI chooses goals; a single serialized controller loop validates and stages commands; hardware adapter sends through LeRobot; visual adapter renders. Rendering may never write to hardware. Simulated state is not mistaken for encoder feedback. Mode transitions are deliberate and logged.

**No invented success.** Distinguish requested (slider intent), pre-LeRobot staged (after joint/step constraints), returned sent target (after LeRobot measured cap), actual hardware observation, and simulated displayed joint pose. Make stale data visible; never substitute stale command for a failed read.

**Comms failures latch.** Any lost serial link, unsuccessful status read/write, unknown hardware state or invalid telemetry during actuation stops new target progression and latches `FAULT`; automatic reconnection/resumption forbidden. Torque state after a failure is **unknown** unless confirmed; preserving previous torque means **do not blindly toggle it**, not that torque can be guaranteed.

**Stop is not E-stop.** Emergency Hold is a best-effort measured-position hold if feedback and communications are healthy; Torque Off is explicitly user-confirmed and may permit the arm to fall. Software cannot guarantee stopping under power or communications loss. Never name these physical emergency-stop equivalents.

**Rest is gated by real hardware.** Do not implement/expose functional normal Stop-to-Rest until measured physical rest pose can be recorded and later arrival checked with a bounded, confirmed trajectory. M1 offline UI has pause/hold controls but no invented rest preset.

**Offline completion is honest.** Gate A proves software implementation and mock tests; Gate B is a separate user-operated real-hardware commissioning gate that cannot be green without user observations. The source repository and MuJoCo model are external dependencies; pin commits and verify current APIs before implementing.

**Single-window contract.** Embedded MuJoCo visualization and six joint sliders are in one application window. No fallback that silently starts a detached viewer. A failed graphics backend yields an explicit setup error or documented alternative renderer **inside** the window.

**Test-first, narrow increments.** Each P0–P6 task has behavior tests and exit criteria. Mock the robot for CI. Never tell an agent to operate physical hardware without current explicit human approval. Record version checks, decisions and uncertainty in `IMPLEMENTATION_STATUS.md`.

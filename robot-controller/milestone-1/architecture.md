# M1 technical architecture

## Architectural diagram

```text
┌────────────────────────────────────────────────────────────┐
│ Single PySide6 MAIN WINDOW                                │
│  MuJoCo RGB viewport | 6 sliders | mode/status | diagnostics│
└───────────────────────┬────────────────────────────────────┘
                        │ desired logical joint targets
                        ▼
┌────────────────────────────────────────────────────────────┐
│ ControllerCoordinator (single owner, 20 Hz nominal)        │
│ - Validate finite complete 6-joint frame                   │
│ - Intent + mode/authority check                             │
│ - Calibrated bounds (5° for arm, gripper 0..100)            │
│ - 10-unit per issued-tick VR-style software step limit      │
│ - Sequential command read/write, timestamp/sequence        │
│ - FAULT latch on bus failure                               │
└───────────────┬─────────────────────────┬───────────────────┘
                │                         │
         SIM_ONLY mode              HARDWARE mode
                ▼                         ▼
   ┌────────────────────┐    ┌──────────────────────────────┐
   │ MuJoCoPoseAdapter  │    │ LeRobotSOFollowerAdapter      │
   │ - model unchanged  │    │ - connect(calibrate=False)    │
   │ - joint/unit map   │    │ - get_observation             │
   │ - visual qpos      │    │ - send_action                 │
   │ - mj_forward/render│    │ - native max_relative_target  │
   └────────────────────┘    └──────────────┬───────────────┘
                                            │
                                    physical STS3215 bus
                                            │
                                         real arm
```

`REAL_TO_SIM`: only actual `get_observation` drives MuJoCo pose; no periodic `send_action`. `SIM_TO_REAL`: slider intent goes through safety/LeRobot; actual feedback drives visual pose and separate diagnostics. `SIM_ONLY`: simulator is the active display/command sink; no hardware module needs to be imported eagerly.

## Modules to implement

- `domain/joints.py`: stable enum/order, logical units, finite six-component `JointFrame`, basic bounds.
- `domain/state.py`: mode, connection/arm/fault authority, immutable telemetry snapshots, timestamp age.
- `safety/joint_bounds.py`: tested per-mode bounds adapter; 5° physical margin and normalized gripper range.
- `safety/step_limit.py`: 10 units per emitted control step from previous staged target; source-audited VR math.
- `devices/protocol.py`: `RobotPort` protocol: initialize (explicit), read, send, torque off, close + fake adapter.
- `devices/lerobot_so101.py`: encapsulates import/version checks, six-key map, native 20°/100 config, calibration; serialized sync API.
- `simulation/mujoco_pose.py`: official model load, qpos address discovery, named actuator/range, degree↔rad and gripper 0–100 map, forward/render, no asset patching.
- `control/coordinator.py`: tick scheduling, locks/worker, mode state machine, validated output, health and faults.
- `ui/main_window.py`: single PySide6 shell with embedded MuJoCo image, sliders, requested/staged/sent/actual values, status, safe commands.
- `app.py`: pure CLI bootstrap, default `SIM_ONLY`, no eager serial enumeration or connection.
- `tests/`: offline unit/fake-hardware/GUI/renderer smoke and manual real hardware scripts.

## Rendering policy — P0 decision gate

Preferred: Qt widget displays `mujoco.Renderer.update_scene()` + `.render()` pixels (QImage conversion to pixmap/paint), inside single main window. Hold rendering context in a single owned thread; if Qt GUI thread handles rendering, keep frames cheap and schedule via Qt timer. Do **not** call the renderer from a worker with a different GL current-thread context. Serial/LeRobot calls run on their own single worker and report immutable snapshots to Qt via signals/queued handlers. Do not misuse `mujoco.viewer.launch_passive()` since it opens its own window.

P0 tests X11/Wayland, Mesa/EGL/OSMesa where compatible. Record environment and a successful embedded frame or stop/raise a blocker before building downstream UI; avoid silently relaxing the one-window requirement. A native Qt OpenGL widget rendering MuJoCo directly may be explored if image-copy performance is inadequate, but do not assert it works before a test.

## 20 Hz command loop

- Use a monotonic clock; one scheduling owner; latest requested slider frame replaces older frames (coalescing).
- Each actually emitted controller tick increments the staged pose by at most 10 units/joint from *previous staged pose* toward requested pose; do not multiply 10 by number of skipped ticks to catch up.
- If mode is `SIM_ONLY`, update visual pose with staged values; if `REAL_TO_SIM`, ignore slider targets for motors; if `SIM_TO_REAL`, call LeRobot `send_action(staged)` only when authorized.
- LeRobot may read present pose for `max_relative_target`; returned `sent` values may differ from `staged`. Show all separately. `SENT` is *null/unavailable* before first actual send, never pretend it equals requested.
- Sequential order: acquire/verify observation if appropriate → derive stage → send once → record returned sent values → acquire observation for visual pose on a feasible schedule. Never make concurrent bus calls. If cycle overruns, skip planned ticks and report achieved Hz; no backlog of stale serial actions.
- GUI event rate and renderer frame rate are independent from the controller loop.

## Startup / state flow

```text
SIM_ONLY (default; zero ports opened)
       | explicit "Connect hardware" + warning confirmation
       v
CONNECTING → CONNECTED_READ_ONLY
                   | explicit "Enable movement" + valid calibration
                   v
                 ARMED ↔ HOLDING (healthy comms only)
                   | read/write fail
                   v
             FAULT_LATCHED --manual acknowledge/re-init--> CONNECTED_READ_ONLY

TORQUE_OFF (explicit, warned request) only through verified driver control.
```

Transition from `SIM_TO_REAL` to `REAL_TO_SIM` stops new motion targets. Mode changes from hardware authority must be explicit; closing/disconnecting warns about torque behavior. No real rest command in Gate A. A single `device` owns servo bus; no UI/diagnostic operation performs writes independently.

## Safety ordering

```
UI request → shape/finite/unit validation → per-joint allowed-range clamp
           → VR-style per-tick 10-unit stage limit
           → LeRobot send_action (native 20°/100 measured-relative cap)
           → sent-value return → actual observation → pose conversion/render
```

Do not add own secondary 20° measured cap: native LeRobot does it. For `REAL_TO_SIM`, the staged/sent fields should be marked `N/A` or last sent with explicit timestamps, not mislabel mirror reads as commands.

## Model / gripper mapping

- Load the official `Simulation/SO101/scene.xml`; it includes new calibration model XML and meshes. Find 6 named joints/actuators, check `MjModel.jnt_qposadr`, `jnt_range` / `actuator_ctrlrange` at runtime; fail if missing.
- Logical arm angles (°) → simulation joint hinge radians. Official model zero convention might differ from installed real calibration; do not invent offsets. Start with unit conversion and officially documented new-calibration zero convention; mark actual pose matching as later **visual validation**.
- Gripper logic 0=closed,100=open; provisional linear map to official gripper XML actuation range, with direction/config constant and tests. Verify visually which end is truly open. No misleading claim that the XML already implements LeRobot's normalized convention.
- For pose preview/mirroring, set `qpos` and `mj_forward`; optional `ctrl` setting in sim-only can be included for consistency, but must not be confused with physical actuator dynamics. Do not modify official model files.

## Graceful closure and torque caveat

A voluntary disconnect is distinct from a USB failure: verify LeRobot's `disable_torque_on_disconnect` setting and inform the operator before calling. On link failure, do not send an unverified torque command; record `torque=UNKNOWN` when appropriate. Emergency Hold may require a read and write and therefore is not reliable under comms loss. UI must say so.

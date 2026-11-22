# Source audit (public sources reviewed on 2026-11-06)

This is a repository-level audit, not a claim of pinning or running these projects. Links to `main` are **mutable**. First implementing agent must resolve commit SHAs, preserve copies/links to those revisions in `SOURCE_LOCK.md`, and ensure API compatibility with its installed versions. Do not treat the line numbers as immutable after upstream changes.

## A. LeRobot SO-101 follower — definitive motor command path

- [`so_follower.py`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/so_follower.py#L918-L955): `send_action(action)` extracts `*.pos` fields; if `config.max_relative_target is not None`, reads `Present_Position`, applies `ensure_safe_goal_position`, writes `Goal_Position` through `bus.sync_write`, and returns the **actually sent** goal positions. There is no per-tick Python gravity, torque or voltage controller here. A present-position bus read can reduce control FPS.
- [`config_so_follower.py`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/config_so_follower.py): `max_relative_target: float | dict[str,float] | None = None`, `use_degrees=True`, `disable_torque_on_disconnect=True`, PID coefficients and read-retry count. Dictionary is a supported native configuration.
- [`utils.py` `ensure_safe_goal_position`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/utils.py): if dictionary, its keys **must match** the exact motors in the goal/present pair. Each goal is clipped relative to its read *actual* position. For M1, send all six keys, with dict of 20.0 (five arm joints) + 100.0 (gripper); do not send partial action objects without checking key expectations.
- [`so_follower.py` `connect`/`configure`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/so_follower.py#L798-L875): `connect(calibrate: bool = True)` can calibrate if missing, then calls `configure`; `configure` enters `bus.torque_disabled()` and writes operating mode, PID and specific gripper protective limits. **Hardware connection is not a read-only operation**. M1 requires explicit human warning and calls `connect(calibrate=False)` only after validating calibration, subject to version verification. Do not assume `connect(calibrate=False)` alone guarantees an invariant torque state.
- [`so_follower.py` `_motors_ft`/bus`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/so_follower.py#L731-L750): five arm joints plus gripper. The gripper uses `MotorNormMode.RANGE_0_100`; angles use degree convention with `use_degrees=True`.
- [`so_follower.py` `get_observation`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/so_follower.py#L880-L915): uses `bus.sync_read('Present_Position')`, returns keys e.g. `shoulder_pan.pos` and `gripper.pos`.
- [`so_follower.py` disconnect](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/so_follower.py#L953-L966): bus disconnect may **disable torque** according to config. M1 must not promise torque preservation on failure if firmware/driver/link/power is no longer under control. On voluntary disconnect, show explicit warning about torque behavior before invoking the verified API.

### Exact LeRobot clipping example

```
measured shoulder_lift = 10°
request after VR-style step restriction = 42°
max_relative_target["shoulder_lift"] = 20°
actual target sent by LeRobot = 30°
```

This is a **position-error envelope**, not a 20°/tick velocity limit. The `100` gripper value disables *meaningful* measured-position clipping within the normalized 0–100 logical range; it does **not** disable gripper motor current/torque settings or the upstream 10-units-per-command step limiter.

## B. SO-101-VR-Control — reference, NOT the new application

- [`robot_interface.py`, `setup_robot_configs`](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py#L1670-L1703): instantiates LeRobot `SOFollowerRobotConfig` with `use_degrees=True`, `max_relative_target=10.0`, `disable_torque_on_disconnect=True`. M1 intentionally changes `10` to per-motor `20/100`.
- [`robot_interface.py`, calibration margin](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py#L1905-L1955): obtains calibration at hard-coded follower path; uses servo raw range min/max, converts relative to midpoint with `360/STS3215_RESOLUTION`, then **adds/subtracts 5 degrees**, intersecting URDF limits. It falls back if calibration is missing. M1 must **fail closed** on missing/malformed calibration for real mode, should not blindly reuse the midpoint math without verifying LeRobot's normalized coordinate transformation and sign/homing. See `CALIBRATION_NOTES.md`.
- [`robot_interface.py`, `clamp_joint_angles` and `update_arm_angles`](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py#L2082-L2173): clamps requested angles and then `delta = np.clip(clamped-prev, -10, 10)` for *all six* joints (gripper in 0–100 units), at nominal 20 Hz. This limits **commanded step** relative to previous staged/commanded state, not the actual motor velocity. Extract the joint-space mathematics only; leave out IK and wrist/VR-specific semantics.
- [`robot_interface.py`, `send_command`](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py#L2220-L2260): sends six `*.pos` values to `SOFollower.send_action()` after an interval gate. There is no direct torque command with each action.
- [`robot_interface.py`, actual feedback](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py#L2369-L2420): gets actual LeRobot observation; verify failure cases before adapting as original may fall back to previous requested/commanded angles — forbidden in M1 telemetry.
- [`robot_interface.py`, disengage](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py#L2190-L2218): requests return-to-initial and disables torque without asserting physical arrival. Do **not** copy this into M1. Physical rest pose and arrival verification are deferred.
- VR-specific ground/IK protection in source is rejected by user: **do not port**.

## C. Official SO-101 MuJoCo model

- [TheRobotStudio SO-101 simulation README](https://github.com/TheRobotStudio/SO-ARM100/blob/main/Simulation/SO101/README.md): `scene.xml` by default includes `so101_new_calib.xml` (new joint-zero convention); alternate `so101_old_calib.xml`. User wants official default model **unchanged**. README explicitly notes LeRobot gripper `0=closed`, `100=open` and that mapping is **not yet encoded** directly into the URDF/MJCF.
- [`scene.xml`](https://github.com/TheRobotStudio/SO-ARM100/blob/main/Simulation/SO101/scene.xml): default scene includes `so101_new_calib.xml`. Keep original files and asset hierarchy intact; load file by path, not copy only single XML without mesh directory.
- [`so101_new_calib.xml`, actuators](https://github.com/TheRobotStudio/SO-ARM100/blob/main/Simulation/SO101/so101_new_calib.xml#L782-L794): six `<position>` actuators with matching joint names (`shoulder_pan`, `shoulder_lift`, `elbow_flex`, `wrist_flex`, `wrist_roll`, `gripper`) and radian control ranges. Gripper's XML range was observed around `[-0.17453, 1.74533]` radians; **do not claim its endpoint orientation is verified by physical testing**. Implement an explicit *provisional and configurable* mapping 0–100 → range, compare with model visual on simulator, don't alter upstream model.

## D. MuJoCo rendering and single-window feasibility

- [MuJoCo Python bindings docs](https://mujoco.readthedocs.io/en/stable/python.html): `mujoco.GLContext`/renderers require a valid OpenGL context; an OpenGL context can only be current on one thread at a time. `mujoco.Renderer` can render pixels for a custom UI.
- [MuJoCo rendering docs](https://mujoco.readthedocs.io/en/latest/programming/rendering.html): Linux rendering supports X11/GLX, headless OSMesa, EGL; availability depends on system and drivers. **Do not assume** a Qt/GLFW embedded viewer can be launched by a particular backend without a proof-of-concept.
- M1's one-window requirement warrants a P0 render test: preferred PySide6 + MuJoCo offscreen `Renderer` to RGB frame, display as Qt image in same main window. Establish Linux GL context and thread ownership before architecture commitment. Use a single render owner thread, never share `Renderer` context across threads without controlled transfer. Native `mujoco.viewer.launch_passive()` in a separate window is NOT compliant.

## E. Spec-driven development workflow

- [GitHub Spec Kit](https://github.github.com/spec-kit/): explicit Specify → Plan → Tasks → Implement → Converge lifecycle and agent integrations, includes Claude/Codex.
- [Spec Kit integration CLI reference](https://github.github.com/spec-kit/integrations/cli.html): support agent-neutral workflow files, optionally install one or more integrations.
- Our package already provides approved spec/plan/tasks/tests; don't let auto-generated scaffolding replace them. Spec Kit is optional.

## F. Claims NOT verified

1. Exact current SHAs, releases, licensing constraints for vendoring the upstream MJCF/meshes.
2. Qt + MuJoCo renderer performance on the user's target Ubuntu GPU/session.
3. User's calibration file and motor orientation mapping.
4. Physical servo behavior, USB bus timing, actual attainable 20 Hz, torque changes on setup/disconnect.
5. Real gripper-to-model direction mapping, continuous joint behavior.

**Keep these as explicit implementation or later hardware-validation checkpoints.**

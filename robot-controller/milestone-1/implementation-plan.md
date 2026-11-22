# M1 implementation plan — ordered for AI coding agents

Status: **plan, no code executed**. Dependencies and phases can be refined by the agent after documenting evidence, but the approved requirements cannot be silently relaxed.

## P0 — Baseline, licensing, single-window feasibility (do first)

**Purpose:** avoid building a full application before proving native embedding and asset compatibility.

- Initialize clean Python `src/` project for Ubuntu 24.04 with pytest, static checks and optional GUI testing tools. Choose a compatible Python version after inspecting MuJoCo, LeRobot and PySide6 support. Record *tested* versions and lock files.
- Fetch exact source commits of Hugging Face LeRobot, SO-101-VR-Control, and TheRobotStudio SO-ARM100 simulation; document SHAs, relevant licenses, source locations, link back to code. Do not copy unrelated VR code.
- Load the **official default** `Simulation/SO101/scene.xml` with all assets. Verify exactly six named joints/position actuators and their ranges; exercise `mj_forward` and frame generation.
- **Crucial:** create a minimal PySide6 main window with embedded MuJoCo RGB view and six dummy sliders. Run an Ubuntu GL backend/render test; document X11/Wayland/environment and actual fps/latency. No detached native viewer. Confirm no serial devices opened.
- Stop P0 as a blocker if embedded renderer cannot be made reliable on target Linux. Choose alternative **still inside same window**, with explicit ADR and tested evidence.

**Exit:** one-window render smoke test, joint model loader test, source/version/license lock, running GUI without USB.

## P1 — Contracts, units, model mapping, fake robot

- Define joint order/names, units and `JointFrame` validation: exactly six finite floats. Degrees and normalized gripper must never be mistaken for radian/degree mix.
- Implement `SimulatorPort` with official MJCF unchanged: logical degrees → hinge radians; gripper 0–100 → official model range, provisional direction. Mark pose preview as kinematic (`qpos`, `mj_forward`). Assert named actuator/joint maps, not magic array positions.
- Build fake `RobotPort` that returns deterministic six-joint measurements, applies an *emulated* version of LeRobot's measured cap **for testing** (not runtime hardware replacement), simulates dropped/corrupt reads and writes, and records all API calls.
- Define immutable controller telemetry snapshots with requested/staged/sent/actual/displayed, ages and statuses.

**Exit:** unit tests for all six mappings, gripper endpoints, invalid frames and fake bus events. No real imports open serial ports.

## P2 — Safety + controller + authority state machine

- Read/adapt VR repo's `delta = clip(target-prev, -10, 10)` as a **small pure function**, with source link; apply all six at most once per actual emitted controller tick.
- Implement per-joint range clips: simulation uses declared MJCF ranges and 5° provisional angular margin; hardware bounds require validated existing calibration and 5° angular margin in same LeRobot logical degree units. Do not copy raw tick midpoint logic unverified.
- Keep hardware measured-relative limiter inside LeRobot's native configuration only; the fake may emulate for tests.
- Build monotonic ~20Hz controller, no catch-up bursts, latest-intent coalescing, correct modes, mutual exclusion, typed state transitions and explicit authorization.
- Fault latched on any live hardware observation/write error; freeze/clear command queue, prohibit auto reconnect/resume. Support Emergency Hold best-effort and separate confirmable Torque Off. **No rest pose feature in offline build.**

**Exit:** deterministic time-controlled tests for >10° target steps, measured cap, blocking state transitions, fault/no-resume; zero hardware commands in sim/read-only modes.

## P3 — Integrated UI and diagnostics

- Build one main window containing embedded 3D view, six live sliders with units, requested/staged/sent/actual/displayed values, actual command frequency and status/age/error indicators.
- GUI must be responsive if hardware worker stalls; send only coalesced intents to controller worker, never direct bus writes from widgets.
- Explicit mode switch and connect confirmation; connection/initialization warning about torque disable/change; another enable movement step before actual commands.
- Expose Pause/Resume in sim-only; Emergency Hold and Torque Off with correct semantics for hardware and fake; **no normal Stop-to-Rest button yet**.
- Show stale telemetry distinctly; logs available for export.

**Exit:** GUI tests for no serial at startup, sliders moving virtual model, no hardware authority leak and fault banner.

## P4 — LeRobot adapter (code now; test with fake/mocks only)

- Inspect **installed pinned** LeRobot follower config, action/observation APIs and calibration. Use official `SOFollowerRobotConfig` with `use_degrees=True` and full dict `20° x 5, 100 gripper`.
- Implement explicit connect confirmation and `connect(calibrate=False)` only after validated existing calibration; note `configure()` may disable torque temporarily. The operator must support the mounted arm.
- Map all six `.pos` keys, always send full action set to satisfy native dictionary key equality. Use `send_action()` return value as sent telemetry, `get_observation()` for actual only; never estimate actual from targets.
- Explicitly review `disable_torque_on_disconnect` and shutdown semantics; never promise torque can be preserved after link loss. Torque Off must call a **verified** LeRobot/bus API; if unavailable, disable button with explanation rather than fabricate behavior.
- No robot presence is needed to unit/integration-test via mocks. Do not attempt serial device discovery automatically on module import.

**Exit:** adapter mocked tests pass for correct config, call ordering, data mapping, clip response, calibration denial, exceptions and torque behavior; review upstream source license/version lock.

## P5 — Offline acceptance / Gate A

- Execute `pytest` (headless core), static lint/type checks, model-load test, and GUI smoke test on a graphical desktop or provided supported virtual/headless backend.
- Test distinct `SIM_ONLY`, fake-`REAL_TO_SIM`, fake-`SIM_TO_REAL`. Use a fault-injecting fake to validate state machine and UI.
- Measure viewer frame rate and controller tick rate; report achieved values, GL backend and any deviations. Verify *one window only*.
- Walk requirements in `tests/acceptance-matrix.md`, record evidence and test artifact IDs; mark Gate A done only if all offline blockers pass.
- Produce reproducible local setup and architecture documentation. No hardware tested or claimed.

**Exit:** Gate A checklist complete; `IMPLEMENTATION_STATUS.md` reflects code, environment and results.

## P6 — Prepare Gate B scripts; **do not run** until user has arm

- Provide separate user-invoked, explicit-confirmation steps/scripts: read/config diagnostic; calibration validation; one-joint small move with limits; gripper; full feedback mirror and command; Emergency Hold and separately Torque Off; fault injection/disconnect. Each prints what will happen before any movement and requires live confirmation.
- Prepare physical rest-pose recording/Stop-to-Rest **as a future hardware subphase** only after user provides actual measured pose and approves motion semantics. Do not pre-create pretend safe preset.
- Record operator observations/visual validation against official MuJoCo model; no requirement for automated alignment wizard.

**Exit:** runbooks ready; hardware verification status remains `NOT RUN` until user performs tests.

## Recommended directory after implementation

```text
src/so101_companion/
  app.py
  domain/{joints,state,telemetry}.py
  safety/{bounds,step_limit}.py
  control/{coordinator,clock}.py
  devices/{protocol,fake,lerobot_so101}.py
  simulation/{mujoco_pose,render}.py
  ui/{main_window,controls,viewport}.py
  logging/{events}.py
  assets/{upstream_model_manifest}.json
scripts/{diag_read_only,one_joint_test,gripper_test,mirror_test}.py
config/{default.toml,example.toml}
tests/{unit,integration,gui,smoke}/
```

This layout is a proposal. Favor clarity and testability; do not introduce message brokers, databases, web services, cloud/MCP processes or unnecessary async infrastructure into M1.

## Time / dependency rule

Each phase starts only after its prerequisites pass. No estimates or claims that code has been produced. Critical path: P0 renderer + model → P1 mapping → P2 state machine → P3 UI → P4 hardware adapter mocks → P5 Gate A → P6 hardware commissioning later.

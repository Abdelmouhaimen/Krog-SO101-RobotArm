# ADR-005 — P0 toolchain, embedded renderer backend and model asset handling

**Status:** Accepted by implementing agent 2026-11-06 (P0). Not a user decision; review welcome.
Implements the provisional strategy of ADR-004 with tested evidence. Approved scope/safety
decisions (ADR-001..004) are unchanged.

## Context

AGENTS.md requires every implementation choice that the user did not expressly approve to be
logged with alternatives and consequences. P0 had to choose a Python version, dependency pins,
an offscreen GL backend, a way to obtain the official model unmodified, and placeholder slider
units, before the controller and hardware adapter exist.

## Decisions

### D1 — Python 3.12 (CPython 3.12.3, Ubuntu 24.04 system interpreter), `.venv` via uv
- *Why:* the candidate LeRobot release v0.7.0 declares `requires-python >=3.12`; 3.12 is the
  Ubuntu 24.04 system Python; MuJoCo 3.17 and PySide6 6.12 ship 3.12 wheels.
- *Alternative:* Python 3.10 + LeRobot 0.4.4, which is the stack the user's existing
  SO-101-VR-Control venv uses and has run on the real arm. Not chosen for the new clean project
  because it would pin an older LeRobot API from the start. Kept open for P4 (see D2).
- *Consequence:* `requires-python = ">=3.12,<3.13"`; only 3.12 is tested.

### D2 — LeRobot not installed in P0; candidate pin v0.7.0 (`410c095e…`) recorded only
- LeRobot is a hardware dependency; AGENTS.md/UBUNTU_SETUP require API inspection before it
  becomes a dependency. P0 only recorded the SHA/license and grepped the SO follower API surface
  (six motors, `max_relative_target` dict with key-equality check, `use_degrees`,
  `configure()` under `torque_disabled()`, `disable_torque_on_disconnect`).
- *Risk:* v0.7.0 was released 2026-11-05 (one day before P0). P4 must re-evaluate v0.7.0 versus a
  more mature tag, and versus 0.4.4, which is known to work with the user's hardware.

### D3 — Embedded render path: MuJoCo `Renderer` offscreen → numpy RGB → `QImage` painted in a `QWidget`, on the Qt GUI thread, default backend `MUJOCO_GL=egl`
- *Tested on target* (Ubuntu 24.04.4, Wayland session, Qt QPA `wayland`, Intel Arc Mesa 25.2.8):
  - `egl`: works. Offscreen benchmark ≈ 63–96 fps at 640×480. In-app ≈ 30 fps, which is the
    target cap; render p50 14–21 ms.
  - `glfw`: works with similar numbers. MuJoCo creates a *hidden* GLFW context window, which is
    not a viewer and never visible; the smoke test counted 1 visible top-level window.
  - `osmesa`: fails (`libOSMesa` not installed); `import mujoco` itself raises. The app reports
    an explicit setup error (exit 2) and opens no viewer.
- *Why EGL default:* no window-system object at all, works under Wayland and headless, and lets
  the same code run in CI.
- *Alternatives not chosen:* `QOpenGLWidget` with MuJoCo `mjr_*` rendering directly into Qt's GL
  context. This avoids the pixel read-back, but shares context ownership with Qt and is
  untested. Revisit only if read-back cost becomes a problem in P3. A detached
  `mujoco.viewer` is prohibited (ADR-004) and is banned in the code via a ruff `banned-api` rule
  and a source-scan test.
- *Consequences:* the backend must be chosen before `import mujoco`
  (`simulation/gl_backend.py`); the CLI flag `--gl-backend` falls back to `$MUJOCO_GL` and then
  `egl`. Rendering costs ~14–21 ms of GUI-thread time per frame at 30 fps. That is acceptable for
  P0, but P3 must keep the controller and serial I/O off the GUI thread (already required) and
  may render only on change. The renderer refuses calls from a non-owning thread.

### D4 — Default render size 640×480 (the official model's default offscreen buffer)
- Avoids touching even the in-memory `model.vis.global_.offwidth/offheight`. A larger
  `--render-size` enlarges that buffer *in memory only*; the XML files remain byte-identical.
  The image is scaled to the widget preserving aspect ratio.

### D5 — Official model obtained by a pinned, hash-verified fetch, not committed
- `scripts/fetch_model_assets.py` does a blob-filtered sparse clone of
  `TheRobotStudio/SO-ARM100` at `a758567c…` and checks out only the 17 files listed in
  `src/so101_companion/assets/upstream_model_manifest.json`: scene, `so101_new_calib.xml`, the 13
  referenced STL meshes, the SO101 README and the root Apache-2.0 LICENSE. Every file is
  sha256-verified. The app refuses to start on any missing or modified file.
- *Alternative:* commit the files into this repo, which Apache-2.0 permits with LICENSE/NOTICE.
  Not chosen, to keep the pin explicit and the repo free of 3rd-party binaries. `vendor/` is
  gitignored.
- *Consequence:* first setup needs network + git. Tests that need the model skip with a
  clear reason when it is absent, or fail when `SO101_REQUIRE_ASSETS=1`.

### D6 — P0 sliders use raw official-model joint angles (degrees of the MJCF hinge), labeled as placeholders
- The logical-unit mapping (arm degrees per LeRobot calibration, gripper 0–100 with provisional
  direction) is P1 (T-007/T-008); inventing it in P0 would pre-empt that work. Slider ranges are the
  official `jnt_range`, initial value `qpos0`. No margins and no step limit in P0 (that is P2).
  The UI says this explicitly.

### D7 — Pose preview is kinematic only
- `qpos` + `mj_forward`; `mj_step` is never called; the overlay reads
  "POSE PREVIEW — kinematic qpos, not a physics/torque simulation" (spec nonfunctional constraint).

### D8 — Test/safety tooling
- `diagnostics/device_guard.py`: a CPython audit hook plus a `sys.meta_path` blocker. It rejects
  opens of serial device nodes (`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/serial/*`, …, including
  symlink resolution), enumeration of `/dev`, and imports of `serial`, `lerobot`,
  `scservo_sdk`, etc. It is installed for every pytest session (any violation fails the session)
  and by the P0 app itself. It is **not a security boundary**: native code can bypass it, as
  documented in the module.
- Pytest markers: default run = no GL, no display, no serial; `render` = optional GL smoke;
  `desktop` = optional, launches a visible window; `hardware` = reserved, never automatic.
- Lock file: `requirements/dev-linux-x86_64-py312.lock` (uv, `--generate-hashes`,
  platform `x86_64-manylinux_2_39` = glibc 2.39 of Ubuntu 24.04; PySide6 6.12 needs ≥2.34).

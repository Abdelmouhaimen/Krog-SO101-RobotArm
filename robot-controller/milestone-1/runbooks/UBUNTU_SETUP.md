# Ubuntu 24.04 developer setup

## M1 — verified install and launch commands (2026-11-06, Ubuntu 24.04.4, Wayland, Intel Mesa)

Run from the repository root (`robot-controller/`).

```bash
# 1. Environment (Python 3.12 = Ubuntu 24.04 system python; uv >= 0.12; git)
uv venv --python /usr/bin/python3.12 .venv

# 2a. Simulation + GUI + tests only (no robot driver)
uv pip sync --python .venv/bin/python requirements/dev-linux-x86_64-py312.lock
# 2b. ...or everything incl. the real-arm driver (LeRobot 0.4.4 + CPU torch, ~1 GB download)
uv pip sync --python .venv/bin/python --torch-backend cpu requirements/hardware-linux-x86_64-py312.lock
uv pip install --python .venv/bin/python --no-deps -e .

# 3. Official SO-101 model at the pinned commit (git + network; sha256-verified, unmodified)
.venv/bin/python scripts/fetch_model_assets.py

# 4. Offline verification (no display, no GL context, no serial port needed)
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts
.venv/bin/mypy
.venv/bin/python -m pytest                 # default suite (driver-contract tests auto-skip without 2b)
.venv/bin/python -m pytest -m render       # optional: real MuJoCo EGL offscreen render + full app offscreen

# 5. Run the application (ONE window; always starts in SIM_ONLY; never connects by itself)
.venv/bin/python -m so101_companion
.venv/bin/python -m so101_companion --config my.toml        # pre-fill port/calibration, mapping tweaks
.venv/bin/python -m so101_companion --fake-hardware         # rehearse connect/mirror/arm/fault with a FAKE robot
.venv/bin/python -m so101_companion --gl-backend glfw       # tested alternative to the default egl
# Headless self-check (no window on screen):
QT_QPA_PLATFORM=offscreen .venv/bin/python -m so101_companion --smoke-seconds 5 --artifacts-dir artifacts/smoke
# Optional, opens real windows for ~5 s each:  .venv/bin/python -m pytest -m desktop
```

Hardware commissioning (later, with the arm): see `COMMISSIONING_GUIDE.md`.

Troubleshooting observed on the target:
- `--gl-backend osmesa` fails because `libOSMesa` is not installed (`import mujoco` raises). The app
  exits with code 2 and an explicit message; no viewer is opened. Use `egl` (default) or `glfw`.
- Qt's `offscreen` platform (used by tests) prints `This plugin does not support propagateSizeHints()` — harmless.
- Lock files target `x86_64-manylinux_2_39` (glibc 2.39); PySide6 6.12 wheels require glibc ≥ 2.34.
- A pre-set `MUJOCO_GL` in your shell is used unless `--gl-backend` is given.
- Serial access needs the `dialout` group (only for real hardware).

---

*Original P0 brief (kept for traceability):*

**Current artifact contains specs, not an installed program.** The coding agent must select compatible, tested package versions and provide exact copy-paste commands after P0. Do not install unpinned LeRobot `main` as an implicit production dependency.

## Preflight

- Ubuntu 24.04 with desktop session and functional OpenGL stack, Python virtual environment, Git, C compiler/system libraries as needed.
- No robot, USB, calibration, audio, networked agent, MCP or external game stream required for Gate A.
- Check `python --version`, `uname -a`, `echo "$XDG_SESSION_TYPE"`, MuJoCo/PySide6/LeRobot versions and OpenGL capability. Record versions in `SOURCE_LOCK.md`.
- Use official `TheRobotStudio/SO-ARM100/Simulation/SO101/scene.xml` and matching official assets; preserve original structure and licenses. Pin revision.

## Recommended build sequence

1. Create project `.venv` and install a locked `pyproject.toml`/requirements with MuJoCo, PySide6, pytest, numpy, LeRobot hardware extra (only after API verification) and development tools.
2. Run unit tests that require no GL or serial.
3. Run a standalone model-load test, then single-window renderer smoke test on real desktop. If GL backend fails, record exact error and test supported alternatives; **must remain single-window**.
4. Run `--mode sim-only` (final CLI determined during implementation), confirm six sliders and render/loop metrics.
5. Run mocked hardware tests; verify not opening device even if a `/dev/ttyUSB*` accidentally exists.
6. Document how to install/run and troubleshoot X11/Wayland/EGL/OSMesa on actual user hardware. Do not rely on magic environment variables unless observed to work on target.

## Graphics notes

[MuJoCo Python](https://mujoco.readthedocs.io/en/stable/python.html) and [rendering documentation](https://mujoco.readthedocs.io/en/latest/programming/rendering.html) require valid GL context and single-thread ownership; EGL/OSMesa availability depends on drivers/OS. Avoid relying on separate `mujoco.viewer` because it violates one-window decision. The working P0 program, screenshots and test commands are the deliverable, not a theoretical assurance.

## Source/version lock template

Create `SOURCE_LOCK.md` during P0 containing:

```text
Ubuntu desktop/session:
Python and Python package versions:
Git SHAs of: kabilankb/SO-101-VR-Control, huggingface/lerobot, TheRobotStudio/SO-ARM100:
Model provenance, file list and license:
MuJoCo renderer backend and measured fps:
Current calibration/API behavior verified from exact pinned LeRobot source:
```

# Source and dependency lock

Filled during P0 and finalized in P4 on 2026-11-06 by the implementing agent. SHAs were resolved
with `git ls-remote` / pinned clones on that date. "Installed" means present in the project `.venv`
and exercised by tests. LeRobot is pinned to **0.4.4** after source inspection (ADR-006); it is an
optional `hardware` extra, installed from `requirements/hardware-linux-x86_64-py312.lock`.

| Dependency | Upstream URL | SHA / version | License checked | Status |
|---|---|---|---|---|
| LeRobot | https://github.com/huggingface/lerobot | **`lerobot[feetech]==0.4.4`** = tag `v0.4.4` = `8fff0fde7c79f23a93d845d1a50e985de01f8b8a` (installed wheel byte-identical to the tag for `robots/so_follower/*`, `motors/motors_bus.py`). Same version as the user's working SO-101-VR-Control venv. Evaluated and NOT chosen: `v0.7.0` = `410c095eacc0011d998a73383eb94abed2715af0` (2026-11-05). | Apache-2.0 (LICENSE) | **PINNED + contract-tested** (14 tests run real `SOFollower` code with a fake bus); physical behaviour NOT verified |
| SO-101-VR-Control | https://github.com/kabilankb/SO-101-VR-Control | `2270be2153149010b18d91cf77fd31d05e0afff2` (2026-05-28; equals upstream HEAD and the user's local checkout `../SO-101-VR-Control`) | MIT ("Copyright (c) 2025 Emil Rofors") | PINNED. Adapted (with attribution): the 10-unit step-limit arithmetic (`update_arm_angles`) and the calibration ±5° margin formula (`_tighten_limits_from_calibration`, verified against LeRobot normalization). Not used: IK, VR, URDF fallback, initial-position return, commanded-angle fallback for failed reads. |
| SO-ARM100 SO101 MJCF/assets | https://github.com/TheRobotStudio/SO-ARM100/tree/main/Simulation/SO101 | `a758567c3978dfeefe282ede0500085a48fe8f78` (2026-10-06) | Apache-2.0 (root LICENSE, fetched alongside) | PINNED + sha256-verified (17 files, `src/so101_companion/assets/upstream_model_manifest.json`) |
| MuJoCo | https://github.com/google-deepmind/mujoco | PyPI `mujoco==3.17.0` (tag `3.17.0` = `800a109a4bdd02cb60404d565db4ae16fb04a53b`) | Apache-2.0 (wheel metadata) | INSTALLED + TESTED |
| PySide6 | https://pypi.org/project/PySide6/ | `PySide6-Essentials==6.12.0`, `shiboken6==6.12.0` | LGPL-3.0-only OR GPL-2.0/3.0 (wheel metadata); LGPL obligations apply only on redistribution | INSTALLED + TESTED |
| numpy | https://numpy.org | `2.5.4` | BSD-3-Clause and bundled permissive licenses | INSTALLED + TESTED |
| feetech-servo-sdk / pyserial (via LeRobot) | https://pypi.org/project/feetech-servo-sdk/ | `1.0.0` / `3.5` | Unlicense / BSD (wheel metadata) | INSTALLED with hardware lock only |
| torch (via LeRobot, CPU build) | https://pytorch.org | `2.10.0+cpu` (same torch version as the VR venv) | BSD-3-Clause | INSTALLED with hardware lock only; never imported in SIM_ONLY |
| PyOpenGL (transitive via mujoco) | https://pypi.org/project/PyOpenGL/ | `3.1.10` | BSD-style | INSTALLED (used only for GL info strings) |
| Python Ubuntu 24.04 env | local | CPython 3.12.3 (`/usr/bin/python3.12`), uv 0.12.1 | n/a | TESTED |

Hashed locks: `requirements/dev-linux-x86_64-py312.lock` (23 packages: GUI/sim/tests) and
`requirements/hardware-linux-x86_64-py312.lock` (112 packages: + LeRobot 0.4.4, CPU torch; install with
`--torch-backend cpu`).

## Environment record (P0)

```text
Ubuntu desktop/session: Ubuntu 24.04.4 LTS, kernel 7.0.0-42-generic, Wayland desktop session
  (XDG_SESSION_TYPE=wayland, WAYLAND_DISPLAY + XWayland DISPLAY=:0); Qt QPA platform used: wayland
GPU/GL: Intel Arc (Mesa Intel(R) Graphics (ARL)), Mesa 25.2.8-0ubuntu0.24.04.2, OpenGL 4.6;
  libEGL present; libOSMesa NOT installed
Python and Python package versions: see table + lock file
Git SHAs: see table
Model provenance, file list and license: upstream_model_manifest.json (Apache-2.0, unmodified)
MuJoCo renderer backend and measured fps:
  egl  – offscreen benchmark 63–96 fps @640x480; in-app 30.3 fps (= 30 fps target), p50 14–21 ms
  glfw – offscreen 64 fps; in-app 30.3–30.4 fps, p50 13–18 ms (hidden GLFW context, 1 visible window)
  osmesa – FAILS (libOSMesa missing); app exits with explicit setup error, no viewer opened
Current calibration/API behavior verified from exact pinned LeRobot source (0.4.4, P4):
  motors shoulder_pan..wrist_roll = DEGREES ((raw-mid)*360/4095, unclamped), gripper = RANGE_0_100
  (clamped); calibration file HF_LEROBOT_CALIBRATION/robots/so_follower/<id>.json (draccus JSON:
  id, drive_mode, homing_offset, range_min, range_max); connect(calibrate=False) does not act on a
  register mismatch (app checks is_calibrated itself); configure() inside bus.torque_disabled() writes
  mode, PID 16/0/32 and gripper limits 500/250/25; send_action reads Present_Position, applies
  ensure_safe_goal_position (dict keys must equal action keys; scalar must be float) and returns the
  sent goal; disconnect() disables torque iff disable_torque_on_disconnect (default True).
  Details and evidence: decisions/ADR-006-lerobot-pin-and-joint-mapping.md.
```

Do not ship upstream meshes/assets under guessed license terms. Follow licenses and preserve
notices. Do not claim exact version compatibility until the P0/P4 tests pass.

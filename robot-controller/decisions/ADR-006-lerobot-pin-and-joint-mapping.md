# ADR-006 — LeRobot pin (0.4.4 on Python 3.12) and logical ↔ MuJoCo joint mapping

**Status:** Accepted by implementing agent 2026-11-06 (P1/P4). Supersedes ADR-005 D2 (LeRobot candidate).
Approved user decisions are unchanged.

## 1. LeRobot version

**Decision:** pin `lerobot[feetech]==0.4.4` (git tag `v0.4.4` = `8fff0fde7c79f23a93d845d1a50e985de01f8b8a`),
installed on the project's CPython 3.12 with CPU-only torch from `requirements/hardware-linux-x86_64-py312.lock`.
The adapter refuses any other installed version (`hardware.allowed_lerobot_versions`).

**Evidence (source inspection, not assumption):**

| Check | Result |
|---|---|
| Version used by SO-101-VR-Control `2270be2` on this user's machine | `lerobot 0.4.4` (its `.venv`, Python 3.10) — the stack that moved this user's arm |
| Module used by VR-Control | `lerobot.robots.so_follower.so_follower.SOFollower / SOFollowerRobotConfig` — present in 0.4.4 and 0.7.0 |
| `requires-python` | 0.4.4: `>=3.10` (3.12 OK); 0.7.0: `>=3.12` |
| Diff 0.4.4 → 0.7.0 in `so_follower.py`, `config_so_follower.py`, `robots/utils.py` | Same public API we use (`connect(calibrate)`, `get_observation`, `send_action` return value, dict `max_relative_target`, `disable_torque_on_disconnect`). 0.7.0 adds read retries, configurable PID, new calibration prompt helper. |
| Calibration file location | 0.4.4: `HF_LEROBOT_CALIBRATION/robots/so_follower/<id>.json` — identical to the path VR-Control reads (`.../robots/so_follower/left_follower.json`) |
| 0.7.0 numpy constraint | `numpy<2.3` (conflicts with the P0 lock); 0.4.4 resolves with numpy 2.5.4 |
| Installed wheel vs tag source | byte-identical for `robots/so_follower/*`, `motors/motors_bus.py` (`diff -r` against `v0.4.4`) |
| Reproducible install | `uv pip sync --torch-backend cpu requirements/hardware-linux-x86_64-py312.lock` → 112 packages, ~10 s warm cache; `torch 2.10.0+cpu` |

**Alternatives:** (a) v0.7.0 — newest, but released one day before this work, unproven with this
arm, numpy conflict; (b) Python 3.10 venv identical to VR-Control — rejected because 0.4.4 runs on 3.12
and the GUI stack was already proven on 3.12 in P0.

**Consequences:** torch (CPU) is a transitive dependency of the *hardware* extra only; the default app
and test suite run without LeRobot (verified in a dev-only venv: 216 passed, 14 contract tests skipped).
Upgrading LeRobot later requires re-running the driver-contract suite and extending the allow-list.

### Verified 0.4.4 behaviours the design relies on

* `ensure_safe_goal_position`: dict caps must have **exactly** the action's keys (`ValueError`
  otherwise) and a scalar cap must be a `float` (an `int` raises `TypeError`) → we always send all six
  keys with float caps `{arm: 20.0, gripper: 100.0}`.
* `SOFollower.connect(calibrate=False)`: `bus.connect()` (handshake) → evaluates `is_calibrated`
  (register reads) but **does not act on a mismatch** when `calibrate=False` → we call
  `robot.is_calibrated` ourselves and fail closed (`CALIBRATION_MISMATCH`, no motion).
* `configure()` runs inside `bus.torque_disabled()` and writes Operating_Mode, P=16/I=0/D=32 and the
  gripper protections `Max_Torque_Limit=500`, `Protection_Current=250`, `Overload_Torque=25` (kept as is).
* `disconnect()` → `bus.disconnect(disable_torque_on_disconnect)`; `Robot.__del__` also disconnects a
  still-open robot (see ADR-007 §6).
* Read/write failures raise `ConnectionError` (comm) or `RuntimeError` (servo status) — all mapped to
  typed `HardwareError`s → fault latch.
* DEGREES normalization: `(raw − mid)·360/4095`, `mid=(range_min+range_max)/2`, **not** clamped; the
  gripper RANGE_0_100 is clamped to 0..100 inside LeRobot.

## 2. Calibrated bounds

`bounds = [(range_min − mid)·360/4095 + 5°, (range_max − mid)·360/4095 − 5°]` for the five arm joints,
`[0, 100]` for the gripper. This equals SO-101-VR-Control's `_tighten_limits_from_calibration` formula,
which is therefore **verified against LeRobot's normalization**, not blindly copied. Unlike the source, a
missing/invalid file is a hard failure (no URDF fallback). We do **not** intersect with model ranges for
hardware: the model↔physical mapping (sign/offset) is unverified, so an intersection could produce wrong
limits. `wrist_roll` (full turn 0..4095) → ±175°.

## 3. Logical ↔ MuJoCo mapping (PROVISIONAL until Gate B)

* Arm: `model_deg = sign·logical_deg + offset_deg`, defaults `sign=+1`, `offset=0`. Both LeRobot DEGREES
  and the official `so101_new_calib.xml` ("virtual zero at the middle of its joint range",
  SO-ARM100 Simulation/SO101/README) put 0 at mid-range, so identity is the documented choice, not an
  invented offset. Rotation direction cannot be verified without the arm.
* Gripper: LeRobot 0..100 → official `jnt_range` [−10°, +100°] linearly, `0 → model_min`. An offscreen
  render of the official model shows `model_min` = jaws closed and `model_max` = wide open, matching
  LeRobot's documented 0=closed / 100=open. **Conflicting source:** VR-Control's config uses
  `open_angle: 0` / `closed_angle: 45` on the same `gripper.pos` value, which suggests that on some
  calibrations a *low* value is open. Physical direction is a Gate B check (c04 records it).
* Out-of-model-range values are clamped **for display only** and reported; model files are never edited.
* All three are config keys (`[simulation] joint_sign / joint_offset_deg / logical_gripper_0_maps_to`).

## What still needs the physical arm

Sign of each joint, any zero offset between the user's calibration and the model, gripper direction,
and whether 0.4.4's `configure()` torque cycle is benign on this arm (B01/B02/B05).

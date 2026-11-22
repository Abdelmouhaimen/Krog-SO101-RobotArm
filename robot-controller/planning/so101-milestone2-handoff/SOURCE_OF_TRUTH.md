# Source-of-truth order and integration audit

1. **Live user-approved M1 decisions and latest actual code/ADRs** in the user's project, including ADR-010 fixed direct-target policy and subsequent wrist/calibration/hardware fixes.
2. This package's **M2 interview decisions** (normative only for new behavior-engine features).
3. Actual pinned LeRobot 0.4.4, SO-101-VR-Control and official SO-101 MuJoCo sources as already incorporated in M1.
4. Original `so101-companion-specs.zip` — historical background only. Its M1 examples using 10°/tick and `max_relative_target=20` are **obsolete**.

## Verify, don't assume
- Locate M1 controller, mode/authority state machine, joint frame, current calibration bounds, wrist mapping, local rest-pose procedure, and current telemetry semantics.
- Inspect the current `config`, `IMPLEMENTATION_STATUS.md`, `COMMISSIONING_GUIDE.md`, acceptance matrix and ADR-009..012, not just old historical specifications.
- Verify the current code and tests, and record exact commit/hash, interpreter and dependency locks in implementation evidence.
- Do not overwrite local serial-port/calibration/rest-pose files or include private configuration in version control.
- The current app source tree is **not included** in this handoff; no claim of static review of its latest version is made.

## Hardware facts reported at M1 handoff (not independently reconfirmed here)
- Ubuntu 24.04, one SO-101 follower, LeRobot 0.4.4, native app with integrated MuJoCo, six joints, reported 20 Hz target.
- Wrist had unexplained ~337° rotation; later calibration changed, movement was warned-only under user-approved policy.
- Motor bus had occasional stalls up to ~350 ms; average under load ~16 Hz.
- Elbow had ~3.4° rest error versus existing ±3° tolerance; failure should be reported, not hidden.
- A 5 V supply once caused gripper input-voltage warning; old targets after torque-off/power-up remain a documented risk.
- M1 reports real-to-sim and sim-to-real functions verified; some stress/fault commissioning steps pending.

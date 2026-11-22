# Specification freeze review, 2026-11-06

## User choices checked across all four interview rounds

- [x] One arm, Ubuntu 24.04 (not earlier Windows), new clean codebase, physical arm not currently available.
- [x] Official unchanged MuJoCo model; user visually validates later, no mandatory alignment wizard.
- [x] ONE integrated window with six sliders and requested/staged/sent/actual telemetry.
- [x] Three operating modes with explicit hardware consent and movement enable.
- [x] Reuse existing LeRobot and VR-Control safety: five arm joint ±5° margins, 10 logical units per actually issued command step, LeRobot 20° measured-relative cap x five, 100 normalized gripper cap; gripper 0–100.
- [x] No new gravity compensation or raw voltage/torque controller.
- [x] Ground protection, VR and IK EXCLUDED; M2 and M3 not implemented.
- [x] USB error → latched fault, no automatic resume, preserve/mark unknown torque rather than toggling blindly.
- [x] Emergency Hold + explicit Torque Off with caveats.
- [x] **Normal Stop-to-Rest functionality deferred** until physical arm is present and real pose recorded; no fake sim rest pose.
- [x] Coding agent neutral (Claude Code or Codex), Gate A software vs Gate B physical.

## Unresolved implementation blockers (do not interpret as unanswered user questions)

- [x] P0: exact source/dependency pins and license checks. (SOURCE_LOCK.md, 2026-11-06)
- [x] P0: proof single-window MuJoCo rendering works on target Ubuntu desktop/graphics stack. (docs/evidence/p0)
- [x] P1: provisional gripper mapping visually verified in simulator (offscreen render, ADR-006); [ ] physical direction checked in Gate B.
- [x] P4: validate LeRobot-specific calibration transform and `connect/disconnect/torque_off` APIs for pinned version (source + contract tests, LeRobot 0.4.4; physical behaviour pending Gate B).
- [ ] Gate B: USB port, existing calibration, actual pose correspondence, physical rest pose.

No further user interview is necessary for M1 software Gate A. If P0 proves an unavoidable blocker, present the specific incompatibility and tested alternatives rather than silently relaxing the requirement.

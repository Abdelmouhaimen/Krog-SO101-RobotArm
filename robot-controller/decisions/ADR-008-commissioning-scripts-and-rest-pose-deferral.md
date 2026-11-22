# ADR-008 — Operator-run commissioning scripts; rest pose recording without Stop-to-Rest

**Status:** Accepted by implementing agent 2026-11-06 (P6).

## Decision

1. Eight ordered, operator-run scripts in `scripts/commissioning/` (c01 read-only … c08 hold/torque/fault).
   They reuse the application's controller, safety pipeline and LeRobot adapter. The one documented
   exception is the optional c05 `--test-native-cap` step: a single direct `send` of measured + 25°,
   still inside the calibrated range, behind its own typed confirmation. Its purpose is to observe
   LeRobot's native 20° cap on hardware.
2. Hard opt-in: an interactive TTY plus `--operator-present`, then typed words `CONNECT` / `MOVE` /
   `TORQUE OFF` / `CAP TEST` / `DISCONNECT`. CI, pipes and agents get exit code 3 before any setup (tested).
   Ctrl-C during motion → best-effort Emergency Hold.
3. **Rest pose:** the user asked both "do not implement or activate rest-pose functionality until the
   arm is available" and "prepare … record and test the physical rest pose". We implement only the
   **read-only recording** (c07 → `commissioning-reports/rest_pose.candidate.*.json`). The application
   does not read that file. Normal Stop-to-Rest motion (trajectory, speed, arrival tolerance, timeout,
   hold) stays unimplemented and is task T-039, to be specified and approved with the arm present. In
   c08, "normal Stop" is recorded as NOT RUN by design.
4. Reports: redacted JSON + event-log JSONL per run in `commissioning-reports/` (gitignored), to be
   transcribed into `templates/HARDWARE_TEST_REPORT.md`.

## Alternatives rejected

* A pre-built Stop-to-Rest with a placeholder pose: forbidden by Round 4 / FR-15.
* GUI-only commissioning: scripted steps give reproducible per-tick telemetry and pass/fail records;
  the GUI is still used for the visual checks (c02, c06).

# Gate B — LATER manual physical hardware commissioning

> **Implemented (2026-11-06):** the procedure below is now backed by operator-run scripts
> `scripts/commissioning/c01…c08`. Use the step-by-step **[COMMISSIONING_GUIDE.md](../../COMMISSIONING_GUIDE.md)**.
> Mapping: B1→c01, B2→c02, B3→c03, B4 (gripper)→c04, limits/clipping→c05, B5/B9 timing & sync→c06,
> B7 rest pose *recording only*→c07, B6 hold/torque/fault→c08. Stop-to-Rest stays deferred (T-039).

**Not now.** The user currently has no physical arm available. This is an operator-run procedure for when the desk-clamped SO-101 arrives. No script should auto-trigger it. Every stage starts with a confirmation prompt, printed port, intended action, expected effect and cancel option. Stop if a prior stage fails. Hardware should be clear of people, desk objects and cables; physically support it where torque may be disabled.

## Prerequisites

- Desk-mounted/secured SO-101, verified power supply and USB adapter.
- Local user selects serial device, ideally stable `/dev/serial/by-id/...`, and known LeRobot calibration identity/path. Confirm LeRobot source API version matches tested lock.
- **Never** auto-calibrate. If calibration is absent, stop and use a separate supervised LeRobot calibration procedure outside this M1 app.
- Before `connect()`: show warning: `SOFollower.configure()` may briefly disable torque and change motor mode/gains; support arm; confirm. `connect(calibrate=False)` is intended, but current pinned API must be inspected. Connection is not a read-only physical operation even though no motion action is issued.

## Manual testing order

**B1 — Initialize / observe**

Run diagnostic with an explicit `--enable-hardware` or interactive confirmation flag. Verify calibration, open port/configure, read six measured logical positions. Confirm no target commands were emitted. Show all readings and any errors. Physically support arm during torque state transitions.

**B2 — Real-to-sim mirror**

Launch integrated UI, select `REAL_TO_SIM`; arm manually moving should NOT be required if passive positions are changing, since motors may resist when torque is enabled. Use safely planned movements; visualize feedback and confirm joints correspond by eye. **No automated alignment wizard**. If mismatch, stop and adjust adapter mapping after review, not official model XML.

**B3 — Small one-joint movement**

Explicitly arm/enable. Pick a joint with safe clearance; allow only a small, operator-confirmed movement well inside valid calibrated limits. Command via slider at 20 Hz. Verify observed target, staged and sent positions, actual feedback and real movement. Stop if direction unexpected, clipping wrong, or power/temperature concern.

**B4 — Gripper**

With no object pinched, test small incremental 0–100 changes within soft safe bounds, then visually check 0=closed vs 100=open mapping. Confirm gripper current/torque protections left to LeRobot and no extra 20-degree gripper clip.

**B5 — End-to-end sync and performance**

User moves several small independent joints, confirms viewer follows *actual measured* pose; record achieved control Hz, bus read latency, render fps and any persistent tracking deviations. If 20 Hz not achieved, document result; don't claim compliance based on nominal timer setting.

**B6 — Hold, Torque Off and fault handling**

With arm physically supported and clear: test best-effort Emergency Hold; confirm it reads actual and stops progression. For Torque Off, explicitly warn arm may fall and separately confirm. To simulate USB failure, use a controlled safe rehearsal with support, only under user's explicit approval; verify latched UI fault and no auto recovery. Do not instruct an autonomous agent to physically unplug/power-cycle motors.

**B7 — Real rest pose (DEFERRED FEATURE)**

Only now, if user wants: safely position real arm, save **measured** six-joint pose and audit calibration/limit validity. Specify rest trajectory interpolation, speed limits, arrival tolerance, timeout/failure behavior and torque held after arrival. Implement and test the **normal Stop-to-Rest** feature in a separate approved later task. It is explicitly absent from Gate A.

## Record evidence for each physical step

Operator records test ID (`B01...B09`), date, serial port identifier **with secrets redacted**, calibration identity, actual observations, screenshots/logs, unexpected movement/torque state, PASS/FAIL and notes. A test not actually run stays NOT RUN.

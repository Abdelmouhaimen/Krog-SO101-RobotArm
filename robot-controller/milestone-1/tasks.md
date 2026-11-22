# M1 granular task list (agent execution checklist)

Each task: implement → test → record evidence and status in `IMPLEMENTATION_STATUS.md`. `blocked-hardware` is not a reason to skip unit tests. An agent should work **only the next unfinished task**, unless asked to do more.

## P0 — Base and renderer
- [x] T-001 Create Python project metadata, lint/type/test commands; Git baseline; no eager serial imports.
- [x] T-002 Lock source SHA + license for LeRobot, VR-Control, MuJoCo model; identify installed/tested library versions.
- [x] T-003 Download/reference official SO101 scene + required meshes without modification; assert six expected joints and six actuators.
- [x] T-004 Proof: single PySide6 window displays MuJoCo rendered model; no separate viewer spawned; log backend/fps.
- [x] T-005 Sim-only bootstrap with six mock sliders and no serial attempts; smoke test Linux desktop.

## P1 — Model contracts
- [x] T-006 Define six-name logical joint frame with finite values, exact key set, timestamps, sequence.
- [x] T-007 Implement MuJoCo named joint/actuator discovery and angle-to-radians adapter with tests.
- [x] T-008 Implement explicitly provisional gripper 0–100 mapping with endpoint/direction UI tests; no edits to official model.
- [x] T-009 Implement mock robot and deterministic failure injector.
- [x] T-010 Telemetry stages and freshness metadata; tests prove absent actual stays null.

## P2 — Safety and state machine
- [x] T-011 Implement five angular software bounds + 5° margin using *correct convention* and sim-only provisional model bounds; normalized gripper 0–100.
- [x] T-012 Reuse VR-style 10-unit **per issued tick** delta clip for six joints and test monotonic progression with skipped ticks. *(Implemented 2026-11-06; REMOVED 2026-11-07 by owner decision, ADR-010.)*
- [x] T-013 Implement one monotonic controller scheduler; coalesce new slider targets; benchmark fake 20 Hz, no backlog.
- [x] T-014 Implement three separate modes and explicit movement enable; assert no writes in mirror or sim-only.
- [x] T-015 Implement fault latch, freeze, manual recovery, no auto-reconnect/resume and stale actual; fake read/write failure tests.
- [x] T-016 Implement Emergency Hold best effort and explicit Torque Off request contract; test inability to hold on a disconnected bus.
- [x] T-017 Explicitly exclude rest, IK, VR, ground protection and physical commands in Gate A.

## P3 — User interface
- [x] T-018 Build single integrated main window with 3D, six live sliders, modes and connection state.
- [x] T-019 Show requested/staged/sent/actual/displayed separately with units, colors/text for clipped/stale states.
- [x] T-020 Model connect authorization prompt and separate arm/enable action; show torque-change warning and no auto-calibration.
- [x] T-021 Keep UI responsive during fake 1-second serial stalls and render/backend errors.
- [x] T-022 Add confirmation flow for Torque Off, best-effort Emergency Hold and offline Pause; omit Stop-to-Rest UI.
- [x] T-023 Add log viewer/export and live achieved tick Hz.

## P4 — LeRobot adapter
- [x] T-024 Pin/version-check actual installed LeRobot API and import six motor names.
- [x] T-025 Verify valid existing calibration preflight and reject missing/corrupt/unknown identity; no automatic calibration.
- [x] T-026 Confirm LeRobot native per-motor `max_relative_target` dict (20 x 5, 100 gripper) and full six-key commands. *(Superseded 2026-11-07, ADR-010: `max_relative_target=None`; full six-key commands kept.)*
- [x] T-027 Ensure returned send_action dict becomes `sent` (not requested), actual only from get_observation, with tests.
- [x] T-028 Verify bus torque-off API and disconnect behavior; warnings and mock tests, no automatic hardware call.
- [x] T-029 Implement error handling for read/write/connection failures and state transitions; no auto-retry after latched fault.

## P5 — Gate A quality
- [x] T-030 Run unit and integration tests for every FR/SR rule; fix failures.
- [x] T-031 Run native Ubuntu integrated window visual test; document GPU/session/backend/fps and one-window invariant. *(P0 Wayland desktop proof; final build verified offscreen with the real EGL renderer at the user's request not to open windows — re-run `pytest -m desktop` any time.)*
- [x] T-032 Run fake-hardware mirror and execution demo; compare telemetry stages.
- [x] T-033 Verify no serial connection attempted on import, launch, default mode, CI.
- [x] T-034 Validate source license acknowledgements and reproducible installs; finalize operations README.
- [x] T-035 Complete acceptance matrix with evidence and record Gate A result.

## P6 — Human-operated later
- [x] T-036 Produce read-only observation (after confirmed configuring connect) and port/calibration diagnostic script, operator-controlled.
- [x] T-037 Produce small one-joint script, then separate gripper, then mirror/control manual scripts; each starts in no-motion state.
- [x] T-038 Produce safe hardware fault/disconnect runbook and manual observations template; no automatic fault-injection on live servo.
- [ ] T-039 *(IMPLEMENTED 2026-11-07 — ADR-011: Record rest pose (clamped to bounds, persisted per calibration id), Normal Stop = 30°/s synchronized trajectory, ±3°/±5 arrival within duration+3 s, then HOLD with torque. Offline-tested; real execution 2026-11-07: smooth return + hold; elbow settled 3.4° vs ±3° tolerance.)* At user-approved hardware phase only: record measured rest pose, specify motion/arrival/timeouts, implement normal Stop-to-Rest and holding torque.
- [ ] T-040 *(Supervised commissioning done 2026-11-07 except B06 USB-unplug and the c04/c05 scripts; see IMPLEMENTATION_STATUS.md)* User runs and signs Gate B manual checklist; any failed safety check blocks live behavior development.

## Source-related task caveats

Do not add new software gripper measured-position protection beyond native LeRobot dict configured as 100. Do not add ground checks disguised as collision filters. Do not use fixed physical home pose from the original VR app; that violates R4. Do not represent `20° relative to measured` as `20°/tick`. The current implementation has no meaningful rest-pose function until later hardware phase.

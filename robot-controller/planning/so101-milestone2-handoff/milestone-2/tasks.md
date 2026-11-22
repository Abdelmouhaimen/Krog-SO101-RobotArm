# Implementation tasks — status 2026-11-07: P0–P6 done (Gate A), P7 not started

Owner resolutions D1–D6 (DECISIONS.md, Round 5) apply. Execution order (approved): P1 data → P2/P3 recording + trajectory → P5 controller integration → P4 UI → P6 acceptance → P7 physical (separate authorization).

## P0
- [x] M2-T001 Capture current repo SHA, dependencies, test baseline, uncommitted changes, hardware gate.
- [x] M2-T002 Audit current M1 controller/rest/wrist/torque state and map spec to existing interfaces.
- [x] M2-T003 Record design ADR and source/version assumptions.

- [x] M2-T031 (D4) Stall-aware return-to-rest with regression tests.
- [x] M2-T032 (D5) Calibration fingerprint for rest pose: load checks, explicit migration with backup, re-record.

## P1
- [x] M2-T004 Define validated behavior data schema and provenance rules.
- [x] M2-T005 Implement sample quality validator with >250ms gap rejection.
- [x] M2-T006 Implement atomic behavior repository, IDs, edits/revisions and migration tests.

## P2
- [x] M2-T007 Hand-guided six-joint recording with torque-off precondition and no writes.
- [x] M2-T008 Simulation slider recorder with genuine timestamps.
- [x] M2-T009 Editable simulation keyframes and basic scrubbing.
- [x] M2-T010 Invalid recording user feedback and recovery/re-record path.

## P3
- [x] M2-T011 Linear sampling and trim without mutating source.
- [x] M2-T012 Playback speeds 0.5x..2x; finite and indefinite repeats.
- [x] M2-T013 Smooth distance-based entry and loop transitions, including gripper.
- [x] M2-T014 Stall policy: no catch-up/skip even with 350ms serial delay.
- [x] M2-T015 Wrist seam/regression tests and calibration-bound derived clamp audit.

## P4
- [x] M2-T016 Integrate library/record/timeline/preview into existing one-window UI.
- [x] M2-T017 Show raw versus effective keyframe clamps and invalidate stale preview.
- [x] M2-T018 Show playhead/loop count/clock delay and full M1 telemetry.

## P5
- [x] M2-T019 Exclusive player authority through current M1 coordinator.
- [x] M2-T020 Explicit per-run real playback preview + confirmation with optional arm enabling.
- [x] M2-T021 Fresh actual pose/servo-goal initialization before movement.
- [x] M2-T022 Normal complete/interrupt delegates to M1 saved rest, honors failure.
- [x] M2-T023 Emergency Hold, Torque Off, comms faults override playback, no auto-resume.

## P6
- [x] M2-T024 Headless unit, integration, fault and offscreen tests against acceptance matrix.
- [x] M2-T025 Update ADRs, docs, task statuses, source locks and runbook.
- [x] M2-T026 Evidence-based Gate A signoff; list Gate B unresolved items.

## P7 — Requires user-controlled real hardware
- [ ] M2-T027 Supervised real torque-off recording and calibrated readback validation.
- [ ] M2-T028 Supervised sim-preview then real playback and tracking comparison.
- [ ] M2-T029 Supervised stop/rest, loops, gripper and error recovery verification.
- [ ] M2-T030 Record hardware evidence and Gate B signoff / blocked findings.

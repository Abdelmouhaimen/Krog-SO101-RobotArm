# M2 implementation plan — gated, spec-driven

**This document is a plan, not a claim that implementation has happened.** Stay on current user's repo/branch; no source code included here.

## P0 — Inspect and baseline (offline; no motor actions)
- Read M1 `AGENTS.md`/`CLAUDE.md`, actual code, current ADR-010, status and commissioning procedures. Confirm current git commit, passing test baseline and clean/dirty state; preserve private calibration and rest files.
- Verify actual M1 coordinator interface, timing semantics, rest logic and wrist mapping; document deviations from assumptions. Write an M2 ADR/design mapping.
- Do not silently add a separate device controller or modify M1 fixed safety policy.

## P1 — Pure domain and files
- Define schema, sample time validator (>250ms), stable IDs, full-six-joint validators and metadata/fingerprints.
- Implement atomic local repository, immutable captures and derived edit revisions, friendly load errors and revision migration tests.

## P2 — Recording and sim authoring
- Real hand-guidance recorder pulls only measured samples while torque OFF; no sends. Invalid recording alert with gap positions.
- Sim slider capture and keyframe editor; 3D preview remains disconnected from robot.
- Test with deterministic fake clock and fake bus including 350ms stalls.

## P3 — Trajectory sampler / timing
- Implement linear source interpolation, trim, speed 0.5–2, finite/infinite repeats, smooth entry/loop bridges, no-skip stall clock, predicted duration.
- Integrate canonical recorded-vs-commanded channels, wrist-continuity behavior and default full-six-joint target stream.

## P4 — UI integration
- One window: behavior list, record/edit/trim/scrub/speed/repeats/status/play controls without replacing existing M1 sliders/viewer.
- Preview invalidation on any effective-path change; before/after visible clamp; support single active behavior.

## P5 — Physical playback gate (implementation only until approved)
- Connect player exclusively through M1 coordinator, not SDK. Fresh actual/goal initialization, optional torque enabling in explicit play confirmation, bounded validation, rest delegate, stop/fault precedence.
- Add fake-hardware fault injection and distinct logging for applied/effective/sent/actual.

## P6 — Offline acceptance and docs
- Run default headless tests, renderer checks, lint/type; acceptance matrix with evidence. Test speed, invalid gaps, loops, clamping, stalls, wrist seam, rest failure, mode changes, UI permissions, absent hardware.
- Keep tests headless by default. Produce operator runbook and changelog/ADRs; report precisely which tests are offline versus physical.

## P7 — Supervised physical commissioning (separate explicit authorization)
- Operator confirms supported arm, power arrangement, workspace, rest pose, calibration and emergency power isolation. No unattended runs.
- Follow `runbooks/HARDWARE_PLAYBACK_LATER.md` from low-amplitude one-joint gestures to a short full-six-joint gesture, verifying record/preview/loop/stop/rest and telemetry.
- Gate B only if physical tests performed and results documented. Otherwise mark pending; never claim full M2 validated from simulation alone.

## Exit criteria
P0..P6 => Gate A complete. P7 => Gate B complete. Do not implement future agent integrations or rewrite old M1 controller policy.

# ADR-013 — Milestone 2 Behavior Engine: integration with the M1 controller

**Status:** Accepted 2026-11-07. The owner approved the integration review
(`planning/M2_INTEGRATION_REVIEW.md`) with resolutions D1–D6
(`planning/so101-milestone2-handoff/DECISIONS.md`, Round 5).
**Implemented in:**
- `src/so101_companion/behaviors/` (pure core: model, validate, repository, recorder, plan, preview, service);
- `control/coordinator.py` (`PLAYING_BEHAVIOR`, recording hooks);
- `control/timeline.py`;
- `ui/behavior_panel.py`.

**Does not change:** ADR-010's fixed control policy.

## Decision

1. **Behaviors are an additive layer on the single M1 controller.** It is the only code that talks to the arm.
   - The player is one more MOTION state, `PLAYING_BEHAVIOR`.
   - Each tick it puts the plan's pose into REQUESTED. The rest is M1 unchanged:
     - calibrated clamp (−5°, ratchet);
     - direct `send` with no limiter and `max_relative_target=None`;
     - telemetry, divergence diagnostics and fault latching.
   - Hold, Torque Off, faults, mode change and disconnect leave `PLAYING_BEHAVIOR` through `_set_connection`, which cancels the run without a return to rest.
   - Stop / Stop Behavior hand over to the M1 return to rest.
2. **Ownership.**
   - `MOVE_SLIDERS` is granted only in `ARMED`, so sliders can never fight the player.
   - Only one run exists at a time.
   - Recording and playback exclude each other: recording needs `TORQUE_OFF`, playback needs a run-ready state.
3. **Recording never commands anything.**
   - *Hand-guided takes:*
     - sampled from every fresh measured frame (receipt timestamp) in `TORQUE_OFF` with torque confirmed `OFF`;
     - `requested`/`sent` stored as null;
     - the take is aborted the moment those conditions break.
   - *Simulation takes:* record the authored slider target in `SIM_ONLY`.
   - **Gaps > 250 ms make a take unplayable.** 250 ms exactly is valid. The offending intervals are reported and nothing is filled in.
4. **Files** (D6): `behaviors/<uuid>.json`, schema v1, gitignored, written atomically.
   - Recorded samples are immutable. Edits change settings, names or authored keyframes, bump `revision` and append to `edits`.
   - Hand-guided behaviors carry the calibration fingerprint (ADR-014) and are refused under any other calibration.
5. **Effective trajectory** (`behaviors/plan.py`):
   - trim (boundary samples interpolated) → clamp to the **current calibrated bounds** in an audited derived copy → time ÷ speed (0.5–2×);
   - piecewise **linear** interpolation of the content, with no speed limiting (D2);
   - entry and loop-bridge transitions are **minimum-jerk**. ~~Peak 30°/s, minimum 0.5 s~~ → **amended by D8 / ADR-015:** per-joint speed and acceleration limits, minimum 0.15 s, no bridge when the loop's end matches its start;
   - interpolation happens in the logical frame, never "shortest way round". With the ADR-012 calibration the wrist arc is continuous, so no hidden ±360° path is possible.
6. **Stall-aware playhead** (D1): each issued tick advances logical time by at most one nominal period.
   - A serial stall delays the timeline (`timeline_delay_ms`); it never skips a segment or bursts.
   - The worker already sends exactly one target per issued tick.
7. **Preview gate** (D3, M2-REQ-12). **Amended by D7 (2026-11-07): the preview is optional.** A run without one starts from the fresh measured pose after the explicit confirmation. A supplied preview is still checked as below:
   - A `PreviewSession` plays the exact plan in MuJoCo (display only): entry from the measured pose, content, bridges, and the planned rest.
   - Only a *completed* preview with hardware context (calibrated bounds, fingerprint, fresh measured pose, valid rest pose) yields a **single-use** `PreviewApproval`.
   - Before moving, the controller re-checks every field of the approval:
     - nonce unused;
     - same behavior revision and settings;
     - same fingerprint and bounds;
     - the effective-content hash recomputed;
     - the fresh pose within ±3° arm / ±5 gripper of the previewed start.
   - A disconnected preview is provisional and cannot authorize a run.
8. **Combined confirmation** (M2-REQ-12, requirement 7): if torque is OFF, the confirmed Play request runs:
   1. a fresh read;
   2. the M1 `enable_torque_holding_present` (goal := present, then torque);
   3. the start, from that measured pose.
9. **Visualization vs physical range** (requirement 5). The trajectory is never clamped to the MuJoCo model range. Where the 3D model cannot show a pose (e.g. wrist_flex below −95°), the panel states "drawn at … (display only — the trajectory is NOT clamped)".
10. **UI independence** (requirement 6). `BehaviorService` exposes behavior-level operations only (list, preview, play by object/ID, stop, record). It has no raw-joint method. M3 / agent / MCP are not implemented.

## Fixed constants (`safety/policy.py`, not configurable)

| Constant | Value |
|---|---|
| `TIMELINE_MAX_STEP_PERIODS` | 1.0 |
| ~~`TRANSITION_PEAK_SPEED`~~ | replaced by `TRANSITION_MAX_SPEED` / `TRANSITION_MAX_ACCEL` (per joint), `LOOP_SEAMLESS_TOL_*` (ADR-015) |
| `TRANSITION_MIN_DURATION_S` | 0.15 (was 0.5; ADR-015) |
| `PLAYBACK_SPEED_MIN` / `PLAYBACK_SPEED_MAX` | 0.5 / 2.0 |
| `RECORDING_MAX_GAP_S` | 0.250 |
| `RECORDING_MAX_DURATION_S` | 600 |
| `PREVIEW_START_TOL_ARM_DEG` / `PREVIEW_START_TOL_GRIPPER` | 3 / 5 |

## Alternatives rejected

- **Feeding the player through `set_requested`.** The latest-intent slot drops frames, so samples would be skipped.
- **A separate device controller.** It would bypass M1's faults and clamps.
- **Cubic smoothing of recordings.** Rejected because the spec requires linear interpolation.
- **Clamping to simulation bounds.** Those are narrower than the physical bounds and would hide valid motion.
- **A `jsonschema` dependency.** Not allowed by the runtime-dependency policy; a hand-written semantic validator is used instead.

## Consequences

- At the measured ~16 Hz average (stalls), real playback at "1×" takes longer than nominal. This is reported, never compensated.
- After the return to rest, the elbow may report `Rest not reached` (3.4° vs ±3° in M1). The arm holds, and the result is shown.
- **Not safe unattended.** The ADR-009 wrist runaway was not reproduced and is explained only plausibly (ADR-012). The 5 V supply caused an undervoltage error once. Playback is supervised only.
- The scope-exclusion test no longer forbids "behavior engine" identifiers. The M3 exclusions stay.

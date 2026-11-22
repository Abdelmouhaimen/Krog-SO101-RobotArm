# Approved Milestone 2 decisions — rounds 1–4

## Round 1 — Authoring
- Physical gesture demonstration: **hand-guide the real arm with motor torque OFF**.
- Record **measured positions and requested positions in separate fields**. During torque-off hand guidance, command data are absent, not synthesized.
- Create behaviors from **both real robot and simulation**.
- Initial editing: **trim, loop, playback speed** (plus timeline keyframes for sim creation chosen in R2).
- Start behavior by **automatically making a smooth transition from current measured pose to its first pose**.
- **One behavior at a time**; design joint ownership to permit later composition, but do not implement multi-behavior overlays.

## Round 2 — Workflow
- User explicitly sets **Torque Off**, physically supports arm, **then presses Record**. Record must not change torque state.
- Simulation authoring supports **slider recording and editable timeline keyframes**.
- **Preview in MuJoCo and explicitly confirm every real playback**.
- Entry transition duration **derived from joint movement distance**; not a fixed universal duration.
- Normal completion **returns to saved M1 physical rest pose**.
- Repetitions insert **smooth end-to-start bridge**.
- Interrupting normal behavior **starts existing return-to-rest procedure**. Emergency Hold and Torque Off remain distinct overrides.

## Round 3 — Editing and runtime
- **Linear** interpolation of recorded positions; preserve recorded timing.
- **Reject recordings** with significant gaps.
- Support finite repeat counts and **repeat until manually stopped**.
- Playback speed **0.5x–2x**.
- If return to rest fails: **hold reached position and show `Rest not reached`**, respecting the existing ±3° threshold.
- Library uses **local files**, stable IDs, name, description, timestamps.
- Initial UI uses a **basic timeline**; joint-position curves are deferred.

## Round 4 — Final answers
- Consecutive sample gap **>250 ms invalidates** a recording; exactly 250 ms is allowed. Also invalidate unreadable/invalid samples.
- During serial stall **do not skip ahead or catch up**; delay logical playback timeline.
- Out-of-range keyframes **clamp to existing calibrated limits**, with visible audit and preview. Do not silently alter the immutable raw capture.
- **One explicit playback confirmation may also enable movement/torque**; it must state that clearly and safely initialize goals to the current measured pose first.

## Fixed M1 boundary (unchanged)
- Preserve calibrated physical bounds and the **5° margin**; do not add ground protection.
- **No custom 10°/tick limiter** and **LeRobot `max_relative_target=None`** in current fixed direct-target policy.
- No automatic per-joint torque-off for tracking divergence; maintain diagnostics, existing communication fault latching, manually accessible Emergency Hold and Torque Off.
- Preserve motor electrical protections and M1 gripper convention (0 closed, 100 open according to observed mapping).
- No autonomous or unattended real-arm playback until unresolved wrist behavior and reliable independent stop are resolved.

## Round 5 — Integration review resolutions (owner, 2026-11-07)
Source: owner review of `planning/M2_INTEGRATION_REVIEW.md`. Normative; supersedes any conflicting text above.
- **D1 Stall timing — approved.** Playback is stall-aware: each control tick advances the logical playhead by at most one nominal control period (×speed). Never skip trajectory segments, never jump forward or burst after a serial delay. Playback may take longer than nominal; the delay is reported (`timeline_delay_ms`).
- **D2 Smooth transitions — approved.** Entry and loop-bridge transitions use ease-in/ease-out (minimum-jerk) with a fixed **peak** of 30°/s on arm joints; the gripper follows the existing rest convention (30 units/s). This is NOT a limit on the recorded behavior itself, which keeps linear interpolation and its recorded timing, changed only by the 0.5×–2× speed setting.
- **D3 Entry preview — approved.** Preview the entry transition from the current *measured* robot pose. If, before playback starts, any arm joint moved more than 3° (gripper more than 5 units, the rest-tolerance convention) from the previewed start, a new preview is required. A disconnected simulation preview never authorizes real playback.
- **D4 Return-to-rest timing — change requested (M1 improvement).** The existing return-to-rest trajectory becomes stall-aware with the same clock rule as D1. Speed (30°/s), limits, ±3° arm / ±5 gripper arrival tolerance, hold behaviour and `Rest not reached` failure handling are unchanged. Regression tests must show serial stalls cause no rest-trajectory jumps.
- **D5 Calibration fingerprint — change requested.** Calibration-compatibility checks apply to hand-guided behaviors AND to the saved physical rest pose. An old rest pose is never silently accepted after recalibration. Existing rest-pose files without a fingerprint require an explicit, operator-confirmed migration (original kept as a backup) or re-recording; nothing is overwritten automatically.
- **D6 Storage — approved.** Versioned local files under `behaviors/`, excluded from Git; each has a stable ID, metadata, timestamps, trajectory samples and calibration identity.

### Additional integration requirements (owner, same review)
1. Preserve the latest M1 controller and fixed safety policy (ADR-010): no 10°/tick limiter, no LeRobot measured-relative limiter (`max_relative_target=None`).
2. Preserve calibrated joint limits, warnings, communication-fault handling and manual emergency controls.
3. Do not modify the official MuJoCo model assets.
4. Handle physical-range vs MuJoCo-model-range mismatch explicitly; never silently clamp a valid physical trajectory to satisfy a visualization limit.
5. Recorder and player are independent of the UI (callable later by M3; M3/agent/MCP not implemented).
6. A playback confirmation that enables torque initializes motor targets from fresh measured positions before moving.
7. Normal interruption → return to rest; Emergency Hold, Torque Off and communication faults keep their higher-priority M1 handling.
8. Recordings with a timestamp gap > 250 ms are rejected.
9. Real-arm playback requires a MuJoCo preview and explicit operator confirmation for every run.
10. Keep the power-supply and unexpected-wrist-motion risks documented; never claim unattended playback is safe.

## Round 6 — Gate B session decisions (owner, 2026-11-07)

**D7 — The MuJoCo preview is optional (supersedes requirement 9 above, M2-REQ-12's preview clause
and D3's "require another preview").** Asked during the first supervised session, the owner chose
"Remove the preview requirement".
- **Still required on every real run:** an explicit confirmation. If torque is off, the dialog
  discloses that the motors will be energized.
- **Unchanged:**
  - the fresh read before moving;
  - the smooth entry from the measured pose (≤30°/s);
  - the calibrated bounds and fingerprint checks, and the valid rest pose;
  - faults, Emergency Hold, Torque OFF and the return to rest;
  - ADR-010's fixed control policy.
- **A preview that is run is still checked:** single use, same behavior, settings, calibration and
  bounds, and within ±3° / ±5 of the previewed start. If it no longer matches, it refuses the run
  rather than being silently ignored.
- **Without a preview,** the confirmation dialog says "NO MuJoCo preview was run", and
  `behavior_started` logs `previewed: false`.

## Round 7 — Responsive transitions (owner, 2026-11-07)

**D8 — Energetic emotes (supersedes D2's 30°/s transition peak).** Entry and loop-bridge transitions use
short minimum-jerk trajectories bounded by per-joint speed and acceleration limits (ADR-015) instead of
the fixed 30°/s.
- A gesture whose end matches its start (within 1° / 2 units) loops immediately, with no bridge.
- There are no pauses, holds or returns to rest between repetitions.
- **Unchanged:**
  - the recorded gesture and its timing, and the 0.5–2× speed;
  - the return to rest after the last repetition or a normal interruption;
  - the calibrated limits, electrical protections, fault handling and manual emergency controls.
- Real-arm use needs the owner's approval of a supervised test (B11).

**D9 — Smooth hand-over from a behavior to the return to rest (owner, 2026-11-07, B11 finding).**
- **When it applies:** when a behavior completes or is stopped normally.
- **Change:** the unchanged 30°/s line to the rest pose now starts at the **last target sent**, not at
  the measured pose. It still does a fresh read first.
- **Why:** in B11 a 2× gesture trimmed mid-swing left the arm about 25° behind its target. The target
  snapped back 24° and the arm braked hard.
- **Unchanged:**
  - a plain STOP (sliders / armed) still starts from the measured pose;
  - Emergency Hold, Torque OFF and faults are unchanged;
  - arrival tolerance, timeout and hold are unchanged.

## Round 8 — SO-101 Studio UI redesign (owner request, 2026-11-07; ADR-016)

The owner asked for an animation-studio interface.
- **What changed:**
  - one mode dropdown;
  - Record / Play / Pause / Go Rest;
  - Emergency Hold always visible;
  - behavior speed plus "Getting into position" and "Between repetitions" presets;
  - engineering controls collapsed into Advanced.
- **Defaults chosen during implementation (adjustable):**
  - entry preset **Normal**, bridge preset **Fast**;
  - Simulation Only keeps the arm connected and held;
  - Go Rest from a still arm asks for confirmation;
  - the joint sliders stay in Advanced and open automatically when recording sliders or editing
    keyframes;
  - takes are auto-named.
- **Unchanged:** ADR-010, the M2 file format, preview D7 (optional), and the physical Play
  confirmation.

## Round 9 — Named poses and per-behavior endings (owner request, 2026-11-07; ADR-017)

- **Named pose library:** save current pose (real or 3D), rename, delete, preview in 3D, and move to a
  pose with a smooth, confirmed move.
- **Calibration check:** poses are validated against the current calibration before any real use.
- **"After behavior" is saved with each behavior:**
  - choices: Return to default rest, Hold last position, Move to saved pose;
  - it applies only after all repetitions complete; interruptions return to the default rest;
  - old files keep returning to rest;
  - Go Rest always targets the default rest pose.

**Round 9 addendum (owner, 2026-11-07).** While a behavior plays, including "until stopped":
- **Go** on a saved pose stops the gesture and moves smoothly to that pose. This is an interruption;
  the behavior's ending is not run.
- **Finish after this loop** completes the current repetition, then runs the behavior's own
  After-behavior ending.

# Milestone 2 — Normative product specification

## Goal
Give one SO-101 companion expressive motions (wave, excitement, disappointment, talking-style gripper action) by recording joint trajectories, editing locally, previewing inside the existing single-window MuJoCo UI, and playing them **attended** on physical hardware. Behaviors are local named assets, not policy-generated trajectories.

## Scope requirements
- M2-REQ-01: Reuse existing M1 robot/simulation adapter and authority modes without bypassing their state machine.
- M2-REQ-02: Real recording starts only when connected with valid calibration, readback works and **torque already OFF**. User manually supports arm and presses Record. Recorder never sends a movement target during hand guidance.
- M2-REQ-03: Record all six **measured** positions and actual monotonic-relative timestamps; requested/sent data are **separate optional fields** only if genuinely available. During physical torque-off capture, `requested` and `sent` are null/absent.
- M2-REQ-04: Support MuJoCo authoring via six sliders while recording and via manually inserted/adjusted timeline keyframes. Offline operations must not access serial hardware.
- M2-REQ-05: Reject saving a playable recording if any adjacent measured sample gap is **>0.250 s**, samples are invalid or timestamps are not strictly increasing. Inform user of offending interval(s); preserve diagnostic log separately if useful.
- M2-REQ-06: Store behaviors as local versioned files with stable ID independent of filename or display name, description, created/modified timestamps, six joint names/units, provenance and compatible calibration/model metadata.
- M2-REQ-07: Basic timeline supports trim, scrub/preview, playback speed 0.5–2.0, finite repeats and repeat-until-stopped. Joint curves not required.
- M2-REQ-08: Recorded segment playback uses **piecewise linear interpolation** in logical joint units and honors real timestamps; speed rescales recording timeline.
- M2-REQ-09: Entry from currently **measured** physical pose to behavior first pose is **smooth and distance-dependent**; loop bridge from end to start is smooth. These transition segments are distinct from linear recorded motion.
- M2-REQ-10: Normal end returns automatically using existing M1 rest routine and holds torque. Stop during playback cancels behavior and enters existing normal rest procedure.
- M2-REQ-11: Rest procedure reports failure if joint tolerances remain outside approved M1 bounds (notably ±3° arm tolerance); hold attained pose and display `Rest not reached`. No silent success or endless retry.
- M2-REQ-12 (**amended by D7, 2026-11-07: the preview is optional; the per-run explicit confirmation remains**): For real playback, a successful **MuJoCo preview of the exact effective trajectory**, followed by a fresh explicit operator confirmation **on every run**, is required. Confirmation may combine enabling motors and playback only with unambiguous disclosed physical movement.
- M2-REQ-13: Keyframes outside existing calibrated limits are clamped in an **effective derived trajectory**, with changed values/frames visibly identified and previewed. Raw capture must remain intact; clamp is not a substitute for calibration validation.
- M2-REQ-14: Maintain requested, staged/effective, sent, measured and displayed telemetry as implemented in M1. No made-up encoder data.
- M2-REQ-15: Serial stalls pause logical progression without skipping unissued trajectory samples. Playback may take longer than nominal; do not time-warp to catch up.
- M2-REQ-16: Only one active behavior owns the joints. Explicit cancellation, normal Stop, Emergency Hold, Torque Off, connection fault and user disconnect must have deterministic priorities.
- M2-REQ-17: Preserve fixed M1 direct-target policy and its calibrated bounds/margins; don't reintroduce removed velocity/relative-target clamps, IK, VR, ground collision check or new motor firmware tweaks.
- M2-REQ-18: Keep future stable behavior ID and pure internal play/stop methods, but **do not implement MCP, agent, streaming, speech or joint overlays**.

- M2-REQ-19 (D1/D4): The playback playhead AND the M1 return-to-rest trajectory advance by at most one nominal control period per issued tick; serial stalls delay the timeline, never skip or burst.
- M2-REQ-20 (D2, **amended by D8 2026-11-07**): Entry and loop-bridge transitions are minimum-jerk, as short as the per-joint speed/acceleration limits allow (ADR-015), min 0.15 s; a loop whose end matches its start (1° / 2 units) repeats without a bridge; the recorded content is never speed-limited; the return to rest is unchanged.
- M2-REQ-21 (D3): The authorizing preview is computed from the measured pose, current calibrated bounds and calibration fingerprint; Play is refused if the fresh pose differs by >3° (arm) / >5 (gripper), if any input changed, or if the preview was made without hardware context; each preview authorizes one run.
- M2-REQ-22 (D5): Rest-pose files carry a calibration fingerprint; a missing fingerprint requires explicit migration (backup kept) or re-recording; a mismatching one is rejected.
- M2-REQ-23: Preview shows physical-range positions beyond the MuJoCo model range as an explicit display limitation; the trajectory is never clamped to the model range.

## Non-goals
- No autonomous/unattended execution, scripted Valorant reactions, agent tools, MCP, audio sync, automatic gesture inference, motion retargeting, robot inverse kinematics, physics fidelity upgrade, or new servo gains.

## Definition of done
- **Gate A**: offline UI, storage, timing, simulation, fake-robot and fault tests pass. No motor required.
- **Gate B**: human-approved progressive physical recording/playback and stopping checks pass, with evidence. Unresolved risks explicitly acknowledged; don't misreport unfinished physical checks.

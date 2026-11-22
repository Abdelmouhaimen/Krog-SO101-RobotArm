# Timing and trajectory contract

## Physical recording
- Sample M1 `actual` observations with monotonic timestamp on receipt; keep reported bus/observation timestamp if trustworthy and distinguish it from UI timestamp.
- Measure every adjacent interval; gap `>250ms` means **recording invalid**. Never fill gaps to make a valid saved asset. Report the exact interval and allow re-recording.
- Recording is valid only with torque confirmed OFF throughout, a held/supportable arm, and consistent calibration. If state changes or read fails, stop capture and mark invalid.

## Linear interpolation inside recordings
For adjacent samples `(t0, q0)` and `(t1, q1)` and logical playback time `t` in `[t0,t1]`, use per-joint piecewise linear `q(t) = q0 + (q1-q0)·(t-t0)/(t1-t0)`. Five joints are degrees; gripper uses normalized scale. No cubic smoothing of the demonstration itself.

## Speed
- Speed 0.5x–2x scales the trajectory time parameter; original source timestamps remain immutable.
- 0.5x plays twice as long; 2x plays half as long if communication keeps pace. This is not a promise that hardware will meet that speed.
- Preflight/preview the **exact derived path** with chosen speed, trim, loop and clamping revision before real execution.

## Stall policy: never skip ahead
- Treat each successfully issued control update as advancing one scheduled piece of the playhead; if serial write/read blocks for 300ms, hold effective target/clock progression while stalled and **resume at the next not-yet-served point**; do not advance playhead by 300ms or issue catch-up bursts.
- Use measured elapsed time for logs, but a stall introduces explicit `timeline_delay_ms` rather than hidden time skip. Stop/fault handling must supersede timeline logic.
- During a stall the physical servo may still pursue its last accepted target; record that limitation, do not claim the robot physically freezes.
- A 20Hz target loop is nominal, not guaranteed. Regression tests must simulate 350ms and repeated stalls and show no skipped samples, stale playhead or burst command sends.

## Entry and loop transitions
- Transition from a **fresh measured** physical pose to the recorded start pose with smooth easing (distinct from linear content), duration scaled with largest required movement; gripper handled in its own normalized units.
- For loops, end-to-start bridge is smooth, non-instantaneous, visibly represented in preview, and included for every repeat including infinite repeats. No direct jump to frame 0.
- On completion, delegate return to rest to the existing M1 rest routine. Normal Stop does the same; Emergency Hold and Torque Off bypass rest and retain their M1 semantics.

## Wrist/seam requirement
Do not treat shortest angle on a circle as automatically safe. Respect the M1 wrist calibration and real continuous range; never transform a short visible gesture into an unpreviewed ±360° rotation at the seam.

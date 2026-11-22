# Milestone 2 — Behavior engine interview plan (NOT yet answered)

**Do not implement M2 until the user finishes this interview.** M1's data interface, safety, telemetry and simulation/physical adapter must be reused, not bypassed. The goal is to let the user create expressive motions manually, record them as timed joint positions, edit/preview and safely replay on the SO-101: excitement, waving, disappointment, talking via the gripper. No VR or IK is included by default; prior user explicitly rejected adding VR without asking.

## Starting briefing for the next assistant

> M1 is the one-arm LeRobot↔MuJoCo foundation with official unmodified SO-101 model, single desktop window, six joint sliders, and safety gating. For M2, the user wants to **control the arm manually and record gestures**, then preview/replay them. Do not assume how manual teaching works; ask. Do not add VR unless the user specifically requests it. Start with Round 1; preserve all M1 boundaries and current safety config.

## Round 1 — Experience / movement creation (essential)

1. **How will gestures be taught?** Sliders in the integrated UI, physical hand-guiding with torque temporarily disabled, separate physical leader arm, joystick, editing numeric keyframes, or combination? (Do not suggest VR as already requested.)
2. Should a recording capture **measured** physical positions, requested slider positions, or both with separate labels? User previously preferred measured physical values; confirm whether offline slider-created gestures are allowed too.
3. Desired UI: Record/Pause/Stop/Trim/Re-record, timeline, keyframe editor, preview speed and looping? Which subset first?
4. How are behavior names/tags chosen? User names manually, predefined emotions, descriptions, intensity labels, voice-gesture tags?
5. Which gestures must be supported in first usable release (e.g. wave, excited, sad, talk with gripper) and approximate acceptable movement character? Ask for one demonstrable acceptance clip per chosen behavior.
6. What output types to store: six joint angles + timestamps (default), velocity estimates, metadata, voice sync markers? Any cameras/observations? No video by default.
7. Should recording use the physical arm, simulated arm, or both and how do we distinguish source provenance?
8. Behavior start/end poses: relative to current pose, absolute recorded pose, pre/post transitions, automatic return to idle?
9. What repetition/intensity controls should each behavior expose: `repetitions`, `speed`, `amplitude`, `duration`? Avoid a complex graph editor without request.

**Decision gate:** choose one MVP creation path; mark any other method future. Particularly obtain explicit approval for torque-disable hand-guiding and risks if selected.

## Round 2 — Recording timing, control and safety

10. Recording sample rate / tolerable duration / dropped frame policy? Start from existing M1 20 Hz but don't silently assume it's required.
11. Preserve exact timestamps or resample and interpolate? Which interpolation acceptable (linear/cubic/smooth)?
12. Absolute joint-space recording or relative-offset gestures with scaling to different starting poses? How handle physical joint limit clipping?
13. How to handle transitions into first recorded frame and back to idle (max speed, pause durations, interruption)?
14. Collision expectations? M1 deliberately omits ground protection — do **not** reintroduce unless user revises scope. What prevents hitting desk/monitor during demonstration or replay?
15. What happens if a recorded frame exceeds calibrated limits or a hardware read is missing? Reject, trim, repair via UI, or halt with explicit error?
16. Is gripper speech animation continuous/frequency-controlled? Should amplitude/closed extent be limited separately?
17. Concurrent behaviors: can talking/gripper overlay a wave? Or initially only one animation at a time?
18. Emergency Hold, Torque Off, fault latching and reconnection must remain identical to M1. Who is allowed to abort/interrupt a gesture?
19. Preview must be deterministic at fixed wall clock speed or adapt to slower hardware feedback? How to show target/sent/actual tracking?

**Decision gate:** choose interpolation, playback concurrency and abort policy. Avoid implementing new low-level motor control.

## Round 3 — File format, library and acceptance

20. Storage: JSON/YAML/NPZ/SQLite? Preferred editability vs size, portability, versioning.
21. Are examples personal/private? Need exports/imports and backup or cloud sync?
22. Behavior compatibility: model name, calibration identity, joint names, coordinate convention and measured-vs-sim source stored in each file?
23. Define minimal playback API that M3 can later call without exposing raw joint trajectories (e.g. `play_behavior(id, intensity?, speed?)`, `stop_behavior()`). Do not implement agent tools yet.
24. UI organization: list/search/tags/favorites, thumbnails, test controls? Prioritize MVP.
25. Acceptance: record a 3-second wave, preview on MuJoCo, compare real playback and inspect safety clips; pass tolerances and fallbacks?
26. Which tests run offline vs require real arm? What constitutes verified recorded behavior?
27. How to handle backwards compatibility of behavior files and user-generated versions?

## M2 deliverable package *after interview*, not now

`m2/spec.md`, `m2/architecture.md`, `m2/trajectory-format.schema.json`, `m2/recording-controls.md`, `m2/safety-playback.md`, `m2/tasks.md`, `m2/acceptance-tests.md`, updated ADRs/handoff. Require Gate A (M1) complete before wiring real playback. The final agent-level API should remain behavior-only.

## Explicit M2 non-goals until revised

Valorant stream, AI agent, MCP, screen 3D localization, unsupervised physical training, new servo torque controller. VR remains out of scope.

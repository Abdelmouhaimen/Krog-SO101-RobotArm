# M2 architecture and integration boundaries

## Additive design: do not replace Milestone 1
```
Existing single-window UI ──┬── Behavior library + timeline + recording controls
                           ├── BehaviorRecorder ── existing observation stream (real or sim)
                           ├── BehaviorEditor ── validated immutable source + derived effective revision
                           ├── BehaviorPlayer ── scheduled full-six-joint targets
                           └── existing M1 coordinator ── calibrated clamp ── LeRobot/MuJoCo
```

## Components / single responsibilities
- `BehaviorRepository`: load/save/rename and revision history, schema validation and atomic writes, no serial imports.
- `BehaviorRecorder`: timestamp using monotonic clock; uses M1 actual observation while torque off; captures source and quality flags, no motor writes.
- `SimAuthoring`: records slider edits or explicit keyframes using simulation joint units and current mapping.
- `BehaviorEditor`: applies trim, keyframe edits, repeat/speed settings, clamping and metadata without overwriting raw capture.
- `TrajectorySampler`: takes immutable effective keyframes and logical playhead; returns full-six-joint position vector via linear interpolation.
- `TransitionPlanner`: planned entry and loop bridging using smooth easing, derived duration from distance, and existing M1 rest trajectory for finish/normal interruption.
- `BehaviorPlayer`: exclusive owner of target stream while active, integrates with coordinator authority, pauses on unsuccessful control cycles and latches faults.
- `PreviewRenderer`: shows exact effective path in MuJoCo while **not** issuing hardware targets, even if a physical arm is attached.
- `UI`: embedded timeline panel, library, record/preview/confirm/play/stop, status and fault explanations within existing single window.

## Architectural invariants
1. Only the M1 coordinator can send a physical target; behaviors never call servo SDK directly.
2. Never expose live unrestricted direct motor command interface to a future agent; M3 refers to behavior IDs.
3. Hand-guided physical capture: a pure observer, not command source; torque state must remain OFF.
4. UI mode/authority and behavior-player state must agree. Disallow enabling simultaneous slider and player authority.
5. Real and simulated trajectories share logical units: five calibrated degrees + gripper 0–100.
6. Wrist full-turn/seam mapping must use M1's verified calibration without independently normalizing angle across ±180 causing 360° path changes.
7. No automatic retry/resume on communication fault.

## Transition pacing
Use **the existing M1 rest-transition implementation as the first reference** for smooth transition shape and distance-based duration; do not invent a new safety clamp. The observed M1 rest pace was reported near 30°/s. Document chosen per-joint pacing, including gripper normalized units, in an ADR if the existing routine does not define it. Do not treat this pacing as a configurable hardware safety mode.

## Environment
Ubuntu 24.04; agent-neutral structure, current project files and dependency locks authoritative. Start from current local branch; do not reset recent hardware fixes. Use fake hardware and offscreen MuJoCo in CI; GUI/hardware checks require deliberate operator action.

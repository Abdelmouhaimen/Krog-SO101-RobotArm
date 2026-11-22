# SO-101 Studio: UI/UX redesign proposal and function map

**Status:** Implemented 2026-11-07 (owner request "Complete UI/UX Redesign", with two mockups).
**Scope:** Presentation and workflow only.
- **Unchanged:**
  - the LeRobot adapter and calibration;
  - the trajectory engine core;
  - the MuJoCo backend;
  - the behavior file format;
  - ADR-010's fixed control policy.
- **Controller additions:** small, and each one tested:
  - the mode workflow;
  - pause/resume of physical playback;
  - Go Rest in simulation and from a hold;
  - transition presets.

## 1. Layout (one window)

```
┌──────────────────────────────── SO-101 Studio ─────────────────────────────────────┐
│ ┌──────────── 3D viewport (~68%) ───────────┐ ┌──── Studio panel (~32%) ─────────┐ │
│ │ [badge: mode / PREVIEW — not the real arm]│ │ SO-101 Studio        [status pill]│ │
│ │                                           │ │ ⚠ fault banner (only on fault)   │ │
│ │          MuJoCo robot (live / preview)    │ │ Control mode  [▣→🤖 Sim → Real ▾] │ │
│ │                                           │ │ [● Record][▶ Play][⏸ Pause][⌂ Rest]│ │
│ │                                           │ │ [        ⚠ Emergency Hold       ] │ │
│ │                                           │ │ Behavior [Excited ▾] [⋯]  2.8 s   │ │
│ │                                           │ │ Status chips: entry → play → rest │ │
│ │                                           │ │ Behavior speed  [-] 1.0× [+]      │ │
│ │                                           │ │ Getting into position [Fast ▾]    │ │
│ │                                           │ │ Between repetitions   [Fast ▾]    │ │
│ │                                           │ │ Timeline ▕▓▓▓▓▓|▓▓▓▓░░░▏ 1.4/2.8 s │ │
│ │                                           │ │ Repeat [3] ☐ until stopped  [Keyframes]│ │
│ └───────────────────────────────────────────┘ │ ▸ Advanced settings & diagnostics │ │
│                                               └───────────────────────────────────┘ │
│ status bar: ● Ready · SO-101 · Connected · Calibration valid · Rest pose ready      │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

**Components:**
- `ui/theme.py`: dark theme tokens and stylesheet.
- `ui/widgets.py`: `CollapsibleSection`, `TimelineBar` (scrub, trim handles, playhead, keyframe ticks),
  `StatusChips`.
- `ui/overlay.py`: `OverlayConfirmer`. Confirmations and errors appear as cards inside the window, not
  as separate pop-up windows.
- `ui/studio_panel.py`: `StudioPanel` (mode dropdown, the four primary controls, behavior library,
  speeds, timeline). It reuses the M2 service, preview and plan logic.
- `ui/main_window.py`: composes the viewport, the studio panel, the **Advanced** section and the status
  bar.
- **Control stays out of the presentation code.** The panel submits requests only, and the controller
  stays the single owner of the arm.

## 2. Mode workflow (one dropdown)

| Selection | What happens (one in-window confirmation if the arm is affected) | Result if a step fails |
|---|---|---|
| PC **Simulation Only** | Stop motion targets; stay connected; torque unchanged (the arm keeps holding) | n/a |
| Robot→PC **Real → Simulation** | 1. Connect if needed (configured port and calibration). 2. Mirror mode. 3. **Torque OFF** (the confirmation says to support the arm). | Stays in the last mode that worked, or fault latched; reported |
| PC→Robot **Simulation → Real** | 1. Connect if needed. 2. Control mode. 3. If torque is off: fresh read, goal := present, torque on. 4. Enable movement (sliders in Advanced command the arm). | Same |

The controller runs the steps atomically (`EnterMode`). The dropdown always shows the controller's
**actual** mode, never the requested one.

## 3. The four primary controls (context-aware)

| Control | Simulation Only | Real → Simulation | Simulation → Real |
|---|---|---|---|
| **Record** / Stop | Records the sliders (Advanced opens on the sliders) | Hand-guided (torque off) | Disabled: "Switch to Real → Simulation to record by hand" |
| **Play** | Plays in MuJoCo | Plays in MuJoCo (the arm is limp) | Real arm after the existing D7 rules plus the in-window confirmation |
| **Pause / Resume** | Freezes or continues the MuJoCo playback | Same | Holds at the measured pose and suspends the timeline. Resume: fresh read, then a smooth move back onto the trajectory |
| **Go Rest** / **Save Rest** | Simulated arm to the rest pose | Becomes **Save Rest**: saves the hand-placed pose as the rest pose (confirmed; nothing moves) | Return to rest (M1 controller). From a still arm it asks for confirmation first |

**Emergency Hold** is always visible and separate. Pause never returns to rest.

## 4. Function map: every existing control and its new place

| Before | After |
|---|---|
| Status banner (mode/connection/torque/calibration/authorized) | Status pill + status bar (main screen); full detail line in Advanced |
| Fault banner | Main screen, top of the panel (always visible), with an "Open diagnostics" link |
| Divergence warning | Main screen (amber), only while it is active |
| STOP — return to rest | **Go Rest** (primary control) |
| EMERGENCY HOLD | **Emergency Hold** (always visible) |
| Mode radio buttons | **Mode dropdown** (workflow above) |
| Pause / Resume (SIM_ONLY whole-simulation pause) | Advanced → Simulation ("Freeze simulation"); the primary Pause controls playback |
| Port, calibration file, Browse | Advanced → Connection |
| Connect / Disconnect | Advanced → Connection (the mode workflow connects automatically) |
| Enable movement / Disarm | Advanced → Hardware (the mode workflow arms automatically) |
| Torque OFF / Enable torque | Advanced → Hardware (the mode workflow does it with confirmation) |
| Recover fault | Advanced → Hardware (the fault banner links to it) |
| Record / Adopt rest pose | **Save Rest** (Real → Simulation) + Advanced → Rest pose |
| Joint sliders | Advanced → Joint sliders (opened automatically when recording sliders or adding keyframes) |
| 3D display mapping | Advanced → 3D display mapping |
| Telemetry table, timing | Advanced → Telemetry |
| Event log, Export | Advanced → Event log |
| Rest pose line / three-stops note | Status bar ("Rest pose ready / needs adoption") + Advanced → Rest pose |
| Behavior library list | Behavior dropdown + ⋯ menu (New recording, New keyframes, Rename, Duplicate, Save settings, Delete) |
| Name / description / Save name | Inline rename (no pop-up) |
| Record hand-guided / sim / Stop recording | **Record** (context-aware) |
| New keyframe behavior | ⋯ → New keyframe behavior (auto-named, rename inline) |
| Keyframe table | **Keyframes** toggle under the timeline (keyframe behaviors) |
| Trim start/end spin boxes | Timeline trim handles (exact values shown) |
| Speed spin box | **Behavior speed** stepper 0.5–2.0× |
| Repeat / until stopped | Repeat spin box + "Until stopped" toggle |
| Save as default | ⋯ → Save playback settings |
| Scrub, Live view | Timeline scrub (display only); Live view returns automatically |
| Preview / Cancel preview | **Play** in a simulation mode; "Preview in 3D" (secondary) in Simulation → Real |
| Clamp report / display-limit notes | Small notes under the timeline |
| PLAY on real arm | **Play** in Simulation → Real |
| Stop behavior → rest | **Go Rest** while playing |
| Reason line (why disabled) | Short reason line under the primary controls |
| Recording name prompt (pop-up) | Auto-named "Take hh:mm:ss", renamed inline |
| Confirmation and error pop-ups | In-window overlay cards |

## 4b. Named poses and endings (ADR-017)
- **Behavior card:** an **After behavior** dropdown (Return to default rest / Hold last position /
  Move to saved pose), plus a pose selector shown only for "Move to saved pose".
- **Compact Poses row:** pose dropdown, **Save current**, **Go**, and ⋯ (Preview in 3D, Rename,
  Delete).

## 5. New: transition presets

- **"Getting into position"** sets the entry move and the resume move. **"Between repetitions"** sets
  the loop bridge.
- Each preset is a fixed scale of ADR-015's per-joint limits:

  | Preset | Speed | Acceleration | Minimum duration | Example |
  |---|---|---|---|---|
  | **Fast** | ×1.0 | ×1.0 | 0.15 s | Verified in B11 |
  | **Normal** | ×0.6 | ×0.45 | 0.25 s | |
  | **Gentle** | ×0.3 | ×0.15 | 0.5 s | About the old 30°/s |

- The seamless-loop skip applies in every preset.
- **Defaults:** entry **Normal** (an entry can start from anywhere), bridge **Fast**.
- Presets are a per-run choice, **not** stored in the behavior file (the format is unchanged). They are
  part of the plan hash, so a preview made with other presets does not authorize the run.

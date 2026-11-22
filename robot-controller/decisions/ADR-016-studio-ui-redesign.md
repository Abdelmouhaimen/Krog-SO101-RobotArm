# ADR-016 — SO-101 Studio: UI/UX redesign and its workflow additions

**Status:** Implemented 2026-11-07 (owner request "Complete UI/UX Redesign", with mockups).
Offline tests pass. The physical check of the new workflows has not been run.
**Proposal and full function map:** `planning/UI_REDESIGN.md`.
**Does not change:**
- ADR-010's fixed control policy (direct calibrated targets, 5° margins, no step limiter,
  `max_relative_target=None`, fault latching, manual Emergency Hold and Torque OFF);
- the LeRobot adapter and calibration;
- the M2 file format;
- the MuJoCo model assets.

## Decision

### 1. One window, two zones
- **Left (~66%):** a large MuJoCo viewport with a mode badge. It fills its area and has a little more
  camera headroom (view only).
- **Right:** a compact Studio panel, plus ONE collapsed **Advanced settings & diagnostics** section
  containing every engineering control.
- **Bottom:** a one-line status bar.
- **Confirmations:** shown as **in-window cards** (`ui/overlay.py`), with the same texts and blocking
  yes/no semantics as before. File choosers stay native dialogs (Advanced only).

### 2. Mode dropdown and `rq.EnterMode` (controller, atomic)
- **Simulation Only:** stops motion targets; the arm stays connected and torque is unchanged.
- **Real → Simulation:**
  1. connect if needed;
  2. mirror mode;
  3. **Torque OFF** (the confirmation says to support the arm).
- **Simulation → Real:**
  1. connect if needed;
  2. control mode;
  3. if torque is off: fresh read, then goal := present, then torque on;
  4. enable movement.
- **Every step reuses the existing M1 handler**, so its checks, events and fault handling are unchanged.
  The first step that fails ends the sequence and is reported (`mode_workflow_failed`). Nothing is
  retried.
- **Unknown torque:** if the torque state is UNKNOWN, the workflow refuses rather than guessing.
- **Locked while busy:** no mode change during recording, playback or return to rest.

### 3. Pause/Resume of physical playback (`PauseBehavior` / `ResumeBehavior`)
- **Pause:** fresh read, then hold at the measured pose; the timeline freezes. The state stays
  `PLAYING_BEHAVIOR`, so Emergency Hold, Torque OFF and faults still cancel it.
- **Resume:** fresh read, then a smooth minimum-jerk move (the "Getting into position" preset) back to
  where the timeline stands, then playback continues. The pause is not counted as stall delay.
- **Pause never returns to rest.**

### 4. Go Rest extensions
- **From a still, held arm** (`HOLDING` / `CONNECTED_READ_ONLY` in Simulation → Real) the unchanged M1
  return to rest is allowed. The UI asks for confirmation first, because the arm starts moving.
- **In Simulation Only** it moves the **simulated** arm (display only, never sent) to the recorded rest
  pose, or to a default simulated pose. A slider move cancels it.

### 4b. Save Rest (owner request)
In Real → Simulation the fourth primary control becomes **Save Rest**. It runs the existing confirmed
`RecordRestPose`: the measured pose is saved (clamped to the calibrated bounds) and the previous file is
backed up. Nothing moves. Advanced → Rest pose keeps the same action.

### 5. Transition presets: Gentle, Normal, Fast
- Each is a fixed scale of ADR-015's per-joint limits:

  | Preset | Speed | Acceleration | Minimum duration | Note |
  |---|---|---|---|---|
  | Fast | ×1.0 | ×1.0 | 0.15 s | |
  | Normal | ×0.6 | ×0.45 | 0.25 s | |
  | Gentle | ×0.3 | ×0.15 | 0.5 s | About the old 30°/s |

- They are chosen separately for "Getting into position" (entry and resume) and "Between repetitions"
  (bridge). The seamless-loop skip applies in all presets.
- **Per-run choice, not stored in the file.** Presets are part of the plan hash, so a preview made with
  other presets does not authorize the run.
- **Defaults:** entry **Normal**, bridge **Fast**.

### 6. Takes are auto-named
- Takes are named "Take hh:mm:ss" (or "Slider take …") and renamed inline. There is no naming pop-up.
- The ⋯ menu offers: New keyframe behavior, Rename, Duplicate (`BehaviorService.duplicate`), Save
  playback settings, Delete.

## Unchanged safety behaviour
- Physical Play still requires the in-window confirmation, which lists:
  - whether a preview was run;
  - energizing, if the motors are off;
  - presets, speed and repetitions;
  - supervision, emergency and the open risks.
- Emergency Hold is always visible.
- Faults and divergence warnings stay on the main screen.
- Manual Torque OFF and Recover are in Advanced, and the fault banner links to them.

## Tests
- `tests/integration/test_studio_workflow.py` (controller): workflow, failure paths, pause/resume,
  Go Rest, presets.
- `tests/gui/test_studio_panel.py` (UI with fake hardware): mode dropdown, primary controls, library,
  timeline, keyframes, overlay cards.
- The main-window GUI tests were updated.

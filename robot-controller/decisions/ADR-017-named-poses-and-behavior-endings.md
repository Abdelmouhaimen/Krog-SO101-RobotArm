# ADR-017 — Named pose library and per-behavior "After behavior" ending

**Status:** Implemented 2026-11-07 (owner request). Tested offline with fake hardware. Real-arm check
NOT RUN.
**Extends:** ADR-013 (Behavior Engine) and ADR-016 (Studio UI). It does not replace them.
**Does not change:**
- ADR-010's fixed control policy;
- the LeRobot adapter;
- the default rest pose and its behavior (ADR-011/014);
- Go Rest, which always goes to the designated default rest pose.

## 1. Named poses (`behaviors/poses.py`, pure)
- **Contents:** six joints in the logical frame, plus a stable UUID, name, source, timestamps and
  clamped joints.
- **Source:**
  - `simulation`: the 3D arm in Simulation Only;
  - `real_arm`: the measured pose in a hardware mode. It is clamped to the calibrated bounds when
    saved (the same rule as the rest pose) and stores the calibration fingerprint.
- **Storage:** `poses/<id>.json`, schema v1, written atomically, local and gitignored. A fake-hardware
  run uses `artifacts/fake-hardware-poses`.
- **Before any real use**, `pose_problems_for_arm` refuses a pose that:
  - was saved with another calibration; or
  - has any joint outside the current calibrated bounds. A pose is never clamped silently.

## 2. Move to a pose (`rq.MoveToPose`, controller)
- **Real arm** (Simulation → Real, from a held or armed arm, torque on, no fault):
  1. validate the pose;
  2. fresh read;
  3. a minimum-jerk `SmoothTransition` with the "Getting into position" preset;
  4. a new motion state, `MOVING_TO_POSE`, using a stall-aware clock (each tick advances by at most
     one control period);
  5. then `HOLDING`: the servos keep the pose as their last target.
- **Interruptions:** Go Rest returns to the default rest from the last target. Emergency Hold, Torque
  OFF, faults and mode changes cancel the move (`pose_move_cancelled`).
- **Simulation Only:** the 3D arm moves; nothing is sent.
- **The UI confirms** every real-arm move.

## 3. "After behavior" (saved in the behavior file)
- **Where it's stored:** `settings.after = {"action": "rest" | "hold" | "pose", "pose_id": …}`.
- **Old files:** files without the key keep returning to the default rest.
- **Saving:** changing the dropdown saves a new revision immediately (`BehaviorService.set_after`).
- **It runs only when ALL repetitions COMPLETED:**
  - **rest:** the unchanged return to rest, starting from the last target (D9);
  - **hold:** `HOLDING` at the last sent target; nothing else is sent;
  - **pose:** a pose move from the last target (entry preset), then hold.
- **A normal interruption** (Stop / Go Rest, including "until stopped") always returns to the default
  rest pose. It is never treated as a completion.
- **A "pose" ending is validated at Play,** before anything moves:
  - a missing or deleted pose, or one from another calibration, refuses the run with an explanation;
  - the UI disables Play and shows the reason.
- **Previews and confirmations** show the planned ending.

## Tests
- `tests/unit/test_poses_and_endings.py`;
- `tests/integration/test_behavior_endings.py`;
- `tests/gui/test_studio_panel.py` (ADR-017 section).

They cover: CRUD, calibration checks, file compatibility, hold / rest / pose endings, missing poses,
completion versus interruption, Emergency Hold / Torque OFF / Go Rest during an ending, and direct
moves (real and simulated).

## Addendum — ending a looping run somewhere else (owner request, same day)

- **Go to a pose while playing.** `MoveToPose` is now also allowed in `PLAYING_BEHAVIOR` and
  `MOVING_TO_POSE`.
  - It is an explicit operator interruption: `behavior_stopped` with reason "operator Go to pose".
  - It continues smoothly from the last target, the same way as D9.
  - The behavior's own ending is **not** run.
  - The pose is validated first; if it isn't usable, the behavior keeps playing.
  - There is no extra confirmation card while the arm is already moving, consistent with Go Rest.
- **Finish** (`rq.FinishBehavior`, the "Finish after this loop" button, shown while playing).
  - The current repetition completes; if the arm is in a bridge, the next repetition completes.
  - Then the behavior's **own** After-behavior ending runs, as if that repetition were the last.
  - It counts as a completion: `behavior_completed`, with `finished_by_operator: true`.
  - Pause/Resume and Go Rest are unchanged.
- **Tests:**
  - `test_finish_completes_the_current_loop_then_runs_the_ending`;
  - `test_finish_with_a_pose_ending_and_during_a_bridge`;
  - `test_go_to_a_pose_while_playing_interrupts_smoothly`;
  - `test_go_to_an_unusable_pose_while_playing_keeps_playing`;
  - GUI `test_finish_button_and_go_to_pose_while_playing`.

# ADR-014 — M1 changes requested in the M2 review: stall-aware return to rest, rest-pose fingerprint

**Status:** Accepted 2026-11-07 (owner resolutions D4 and D5). These are targeted changes to
Milestone 1 behaviour; everything else in ADR-010 and ADR-011 is unchanged.

## D4 — Return to rest is stall-aware

- **Before:** `RestTrajectory.at(now)` used wall-clock time. After a 350 ms serial stall, the next
  target jumped ahead by up to 30°/s × 0.35 s ≈ 10.5°.
- **Now:** the coordinator drives the same straight-line trajectory with a `StallAwareClock`
  (`control/timeline.py`). Each issued tick advances it by at most one nominal period (50 ms).
  - The arrival check uses the same logical time, and so does the timeout (`duration + 3 s`).
  - `rest_reached` and `rest_not_reached` report `timeline_delay_ms`.
- **Unchanged:**
  - the speed (30°/s, gripper 30 units/s) and the 0.5 s minimum;
  - the bounds and ratchet;
  - the ±3° / ±5 arrival tolerance;
  - the hold on arrival, the hold-where-it-is with `REST NOT REACHED` on timeout, and the event names.
- **Consequence:** with stalls the return takes longer. The timeout is measured in logical time, so
  stalls do not cause a false `REST NOT REACHED`.
- **Test:** `test_rest_return_does_not_jump_after_a_stall`. Three 350 ms stalls; no target step
  exceeds 1.5°; the reported delay is 1050 ms.

## D5 — The rest pose is tied to the calibration

- **Fingerprint** (`devices/calibration.calibration_fingerprint`): SHA-256 over the six motor
  entries (id, drive_mode, homing_offset, range_min, range_max). The robot id is not enough: the
  ADR-012 re-centring changed motor 5 and kept `left_follower`.
- `RestPose` now stores `calibration_fingerprint`. On connect, the rest pose is classified as:

  | File | Result |
  |---|---|
  | No file | none |
  | Matching fingerprint | loaded |
  | **No fingerprint (legacy)** | `needs_adoption`: not used; Normal Stop and Play stay unavailable |
  | Different fingerprint | `mismatch`: rejected; only re-recording helps |
  | Outside the bounds | rejected (as before) |

- **Adopt rest pose…** is a new, explicitly confirmed action, available only for a legacy file. It:
  1. shows the stored pose;
  2. **backs the file up** (`rest_pose.<id>.bak-<time>.json`);
  3. rewrites the file with the current fingerprint, keeping the hand-written note in `provenance`.
- **Saving never destroys an existing file.** Every save (record or adopt) backs up the previous
  file first. Nothing is migrated automatically.
- **Effect on this machine:** `config/rest_pose.left_follower.json` was hand-shifted for the
  re-centred wrist and has no fingerprint. On the next connect it shows "Rest pose NOT usable … Adopt
  rest pose…". The operator either adopts it after checking the pose, or records a new one.
- **Tests:**
  - `test_legacy_rest_pose_needs_explicit_adoption_with_backup`;
  - `test_rest_pose_from_another_calibration_is_rejected`;
  - the existing M1 rest-pose tests, unchanged.

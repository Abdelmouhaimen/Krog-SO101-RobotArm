# ADR-015 — Responsive entry and loop transitions (per-joint limits, seamless loops)

**Status:** Implemented 2026-11-07, software only; supervised real-arm test (B11) pending.
**Owner decision:** D8, `planning/so101-milestone2-handoff/DECISIONS.md`, Round 7.
**Amends:** ADR-013 §5 (transition speed).
**Does not change:**
- ADR-010's fixed control policy;
- the return to rest (ADR-011/014: 30°/s, ±3° arrival, hold);
- the recorded content: linear interpolation at its recorded timing × 0.5–2×.

## Problem

Entry and bridge transitions used one peak speed for every joint, 30°/s, with a 0.5 s minimum. In
the first supervised session (2026-11-07):
- the entry from the rest pose to a hand-recorded gesture took **10.2 s**;
- the loop bridges took **2.55 s**.

Emotes felt mechanically slow.

## Decision

1. **Per-joint limits.** Entry and bridge are still synchronized joint-space minimum-jerk segments.
   Each is now as short as the per-joint speed AND acceleration limits allow:
   `T = max(0.15 s, max_j(1.875·Dj / Vj, √(5.7735·Dj / Aj)))`.

   | Joint | V (°/s) | A (°/s²) | Basis |
   |---|---|---|---|
   | shoulder_pan | 120 | 500 | Light inertial load |
   | shoulder_lift | 90 | 350 | Carries the whole arm against gravity; not yet measured on this arm |
   | elbow_flex | 110 | 450 | Gravity-loaded; not yet measured on this arm |
   | wrist_flex | 150 | 700 | Wrist verified at about 220°/s on this arm (ADR-012) |
   | wrist_roll | 150 | 700 | Same |
   | gripper | 150 units/s | 700 units/s² | 0–100 scale |

   Reference: the STS3215 no-load speed is about 270°/s at 7.4 V, and it drops with voltage. This arm
   runs from a 5 V supply that once caused an undervoltage error. The limits stay below the verified
   figure, with extra margin on the loaded joints.

2. **Seamless loops.** If a gesture ends within 1° (arm) and 2 units (gripper) of its first pose, the
   bridge is skipped and the next repetition starts immediately. The same rule applies to the entry:
   if the arm is already at the first pose, there is no entry move.
   - The step at the seam is at most that tolerance, which is smaller than the content's own per-tick
     steps.
   - Otherwise a short minimum-jerk bridge is used, with a 0.15 s minimum (3 control periods), so it is
     never a step.

3. **Unchanged:**
   - continuity, with zero speed at the ends of every transition;
   - monotonic paths with no overshoot, so no new bound or wrist seam can be reached;
   - interpolation in the logical frame (no ±360° wrist path);
   - the stall-aware clock (D1);
   - the calibrated bounds and ratchet;
   - fault latching and the manual emergency controls;
   - the return to rest.

## Evidence (offline)

- **Unit tests:**
  - per-joint speed and acceleration, sampled numerically at 1 kHz;
  - whole-range moves, which stay monotonic;
  - seamless versus bridged loops;
  - recorded timing preserved at 0.5×, 1× and 2×.
- **Controller loop at 20 Hz, fake arm:**
  - a large entry, 4 loops and a 350 ms stall;
  - no sent step exceeds `V_j × 50 ms`;
  - the return to rest is still 30°/s.
- **MuJoCo physics** (`tests/integration/test_transition_physics.py`; official model, force-limited
  position actuators, gravity, 20 Hz zero-order hold): a large entry, bridged loops and seamless loops
  are tracked within 10°, with no new contacts.
- **Simulation with the owner's own take** (scratch script, not committed):

  | Run | Entry (old → new) | Bridge (old → new) |
  |---|---|---|
  | From the rest pose | 10.1 s → 2.75 s | 2.55 s → 0.58 s |
  | From the side pose seen at connect | 10.6 s → 2.7 s | — |
  | Whole range | 11.7 s → 3.3 s | — |

  Tracking error stayed at or below 7° in all of them.
- **Model caveat.** In the model, the actuators run at their torque limit for 50–80% of a fast
  transition, against 20–50% for the old rule and up to 48% for the recorded content itself. This is
  partly an artefact of the very stiff model actuators and the 20 Hz step targets. It still means the
  real servos will draw noticeably more current during transitions, which is the 5 V supply risk.

## Risks and the real-arm test (B11)

- **Higher current during transitions** can trigger the 5 V undervoltage error seen once before. B11
  increases the distance step by step and stops at the first voltage or overload warning.
- **The shoulder_lift and elbow_flex limits are not yet verified on this arm.** B11 measures the
  tracking (requested / sent / actual) before any limit is raised.
- **Not safe unattended.** The ADR-009 wrist risk is still open.

## Addendum — D9: smooth behavior-to-rest hand-over (B11 finding, 2026-11-07)

**What B11 showed:**
- Runs: 3 real runs (1× and 2×, up to 5 loops).
- Transition limits: every entry and bridge step stayed within its per-joint limit, with no fault and
  no voltage warning.
- Measured under load at 2×: elbow 194°/s, wrist_flex 149°/s, shoulder_lift 138°/s.

**The problem:** a gesture trimmed to end mid-swing at 2× left the arm about 25° behind its target.
The return to rest started from the *measured* pose, so the commanded target snapped back 24° in one
tick and the arm braked from about 150°/s.

**The change:** `_start_rest(…, from_last_target=True)` is used for behavior completion and the normal
behavior Stop.
- The 30°/s line starts at the last sent target, which is inside the bounds.
- `stop_to_rest_started` logs `starts_from` and `lag_at_start`.
- The fresh read still runs first, and a failed read latches a fault.
- The arrival check still uses the measured pose (±3°, duration + 3 s).

**Unchanged:** a plain M1 STOP starts from the measured pose.

**Test:** `test_behavior_hands_over_to_rest_from_the_last_target_without_braking`.

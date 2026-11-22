# Milestone 2.6: Follow Me and Talk Mode, design review

**Status:** APPROVED by the owner, 2026-11-07, with decisions D1–D10. Implemented as ADR-019 (see that
ADR for the as-built design).
**How it was checked:**
- the official SO-101 model (`vendor/SO-ARM100/Simulation/SO101/so101_new_calib.xml`, unchanged), using
  MuJoCo FK;
- your calibration (`left_follower.json`), your 3 recorded behaviors, your saved pose and your rest pose;
- the controller code after Milestone 2.5.

**The physical arm was not touched.** The analysis scripts are read-only. The render is in
`docs/evidence/follow-me-review/normal_reversed_topdown.png`.

## 0. Summary: verdicts and corrections

| # | Claim in the request | Verdict |
|---|---|---|
| 1 | `shoulder_pan` is a horizontal yaw | **Correct.** Its axis is vertical, world (0, 0, **−1**). FK matches a pure rotation to 1e−13°. |
| 2 | Measure `r` from the base | **Must be corrected.** The pan axis is **38.8 mm in front of the base origin** (x = 0.0388 m). Bearings measured from the base origin are wrong by 6–8° for normal reaches and up to **38°** near the axis. Measure from the **pan axis**. |
| 3 | `θ = θ_front + s(q − q0) + φ` | **Exact once the reference point is corrected** (FK error ≤ 4e−13° at 7 pan angles × 4 configurations). The sign depends on the angle convention: **s = −1** with the usual counter-clockwise `atan2` (z up), **s = +1** if angles are measured clockwise from above (the direction of +pan). |
| 4 | φ is a continuous direction that needs a robust circular median | **Partly.** On the SO-101, lift, elbow and wrist are parallel horizontal axes. With pan fixed, the gripper center stays in the arm's vertical plane, so **φ is always near 0° or near 180°**: deviation ≤ atan(8.1 mm / ρ), from the wrist-roll site offset (checked on 20,000 random poses). The real question is **which side of the pan axis the gripper is on**. A circular median is still the right estimator: an arithmetic mean on your recording "real_hand_guided 12:33:14" gives **−43.7°** instead of ±180°. |
| 5 | Normal ≈ 0°, reversed ≈ 180° | **Confirmed.** An extended arm gives φ = +0.06°; your rest configuration gives 179.84°. **All three of your recordings, your saved pose and your rest pose are REVERSED** (φ within 0.3° of 180°). |
| 6 | Reversed θ = 0 → pan ≈ −180° "or equivalent" | **There is no equivalent.** Pan travel is **±104.9°** (209.8° in total), so a reversed behavior can only face θ ∈ [75.1°, 180°] ∪ [−180°, −75.1°] (front = pan center). θ = 0 leaves a **75° residual** and must be reported UNREACHABLE. |
| 7 | q0 = the calibrated pan reference | **Needs an explicit "front" setting.** Logical pan 0 is the middle of the pan travel (your calibration is symmetric), and the model then points the arm along +x. Your own recordings face 97–132° clockwise from there (see §2). "Front" must be a configured value, not an assumption. |
| 8 | Smoothstep `3u² − 2u³` for Talk Mode | **Works, but use min-jerk (quintic) instead.** Smoothstep has nonzero acceleration at both ends, so acceleration jumps at every segment boundary, twice per "syllable". The repository's min-jerk `SmoothTransition` has zero end acceleration, and its peak acceleration is about 4% lower. |
| 9 | WebSocket target channel | **Feasible, but it needs your approval.** The scope test bans the word `websocket` (an M1 VR-teleoperation exclusion), and the API currently runs uvicorn with `ws="none"`. |

## 1. Geometry (Q1–Q5)

**Model facts.** All values below were read from MuJoCo with the model at q = 0.

| Joint | Axis in world | Anchor (m) | Model range | Your calibrated bounds (−5°) |
|---|---|---|---|---|
| shoulder_pan | (0, 0, −1) | (0.0388, 0, 0.0624) | ±110° | **±104.9°** |
| shoulder_lift | (0, 1, 0) | (0.0692, −0.0183, 0.1166) | ±100° | ±99.9° |
| elbow_flex | (0, 1, 0) | (0.0972, −0.0183, 0.2292) | ±96.8° | ±92.3° |
| wrist_flex | (0, 1, 0) | (0.2321, −0.0183, 0.2344) | ±95° | ±97.2° |
| wrist_roll | (−1, 0, 0) (along the forearm) | (0.293, 0, 0.234) | −157…163° | ±163° |
| gripper | jaw hinge | — | −10…100° | 0…100 units (1 unit = 1.32° of servo) |

**Q1: pure yaw.** Yes. The pan axis is exactly vertical. Lift, elbow and wrist bend about parallel
horizontal axes, so with pan fixed the arm stays in one vertical plane.
- **The plane's offset:** it sits at y = −18.3 mm, but the gripper site `gripperframe` lies within
  0.2 mm of y = 0. Wrist roll moves the site on a 7.9 mm circle.
- **The physical arm:** the same holds only if the base is mounted level. This is not checked here.

**Q2: forward and sign.**
- **Forward:** at logical pan 0 the arm extends along model **+x**.
- **Sign:** the axis is −z, so **positive pan turns clockwise seen from above**, toward model −y. That is
  the robot's right when "front" is +x.
- **Mapping:** the logical→model mapping for pan is the identity (sign +1, offset 0), and the B02 mirror
  test (2026-11-07) confirmed visually that the 3D arm follows the real one on all six joints.
- **Display offsets:** they are not needed for Follow Me. The only non-zero one is `wrist_roll −2.7°`,
  and it changes φ by less than 0.05°.

**Q3: reference point.** Use the **`gripperframe` site** (between the jaws, on the fixed jaw body)
relative to the **pan axis anchor** (x = 0.0388, y = 0).
- **Gripper opening:** it doesn't move this site.
- **Validity threshold:** the horizontal distance ρ must be at least **60 mm**, which bounds the φ error
  at 7.7°. At ρ ≥ 100 mm the error is at most 4.6°.

**Q4: φ with pan held at q0.** Correct, and simpler than it looks: because the pan is a pure yaw, φ
computed at *any* fixed pan, minus that pan, gives the same value. I use the model's pan = 0 (logical
0).

**Q5: the yaw equation against FK.** Convention: θ is **clockwise-positive from above**, so +θ turns
the same way as +pan.

```
ψ      = −atan2(y_g − y_axis, x_g − x_axis)   at pan = 0          (representative direction, CW)
β(q)   = wrap180(q + ψ)                                           (gripper bearing, CW from model +x)
target: β = F + θ   (F = configured front, CW from model +x)
q      = F + θ − ψ + 360·k
```

This matches the request's `q0 + (θ − φ_ref + 360k)/s` with s = +1, q0 = F and φ_ref = ψ.
- **Choice of k:** pan travel (209.8°) is less than 360°, so **at most one k lands inside the bounds**.
  Continuity and calibrated limits can never disagree, and there is never a choice between two
  in-range solutions.
- **No solution in range:** clamp to the bound with the smaller residual
  `wrap180(F + θ − ψ − q_bound)`, with hysteresis (see §4).

**FK check** (front F = 0; pan solved from the formula, bearing measured by FK):

| Configuration | ψ | θ | Pan q | FK bearing | Status |
|---|---|---|---|---|---|
| NORMAL (extended) | −0.06° | 0 | +0.06° | 0.00° | reachable |
| NORMAL | −0.06° | +23 | +23.06° | +23.00° | reachable |
| NORMAL | −0.06° | +90 | +90.06° | +90.00° | reachable |
| REVERSED (your rest configuration) | −179.84° | 180 | −0.16° | 180.00° | reachable |
| REVERSED | −179.84° | +120 | −60.16° | +120.00° | reachable |
| REVERSED | −179.84° | 0 | +104.9° (bound) | −74.94° | **UNREACHABLE, residual 74.9°** |

The top-down render of these six cases is `docs/evidence/follow-me-review/normal_reversed_topdown.png`.

## 2. Your data and the "front" problem (Q6)

| Item | Recorded pan | ψ (pan → 0) | Valid time | Spread p90 / max | Class | Absolute bearing (CW from +x) |
|---|---|---|---|---|---|---|
| hello (trim 1.78–4.30 s) | −78…−72° | 180° | 100% (ρ 243–269 mm) | 0.0° / 0.1° | REVERSED | ≈ 105° |
| real_hand_guided 12:33:14 | −79…−66° | 180° | 100% (ρ 224–299 mm) | 0.3° / 0.3° | REVERSED | ≈ 108° |
| Take 13:55:39 | −84…−82° | 180° | 100% (ρ 276–290 mm) | 0.0° / 0.1° | REVERSED | ≈ 97° |
| Pose 14:31:04 | −48° | 179.9° | ρ 348 mm, gripper 27 mm above the table | — | REVERSED | ≈ 132° |
| Default rest pose | −72.7° | 179.84° | ρ 263 mm | — | REVERSED | ≈ 107° |

**Your working direction is about 105° clockwise of the pan-travel center,** with the arm folded back
over the pan axis.
- **With front = your working direction (F ≈ 105°):** a reversed behavior needs q = θ − 75°, so it can
  follow θ ∈ [−29.9°, +179.9°]. That is lopsided: barely 30° to one side.
- **Normal behaviors** would sit at the pan limit for θ = 0.
- **The fix is a choice for you:**
  - **(a)** accept the asymmetric range;
  - **(b) recommended if you want symmetric following:** rotate the robot's base about 75° on the desk
    once, so your working direction is the middle of the pan travel. Reversed behaviors could then
    follow ±104.9°.
  - Recordings stay valid either way: Follow Me ignores the recorded pan, and without Follow Me they
    would simply face about 75° further round.

**"Front" must be configurable.** I propose a Studio action, "Set front = current facing", that stores
F = q_measured + ψ_active in the local (gitignored) config. The default is F = 0, the pan-travel center.

## 3. Representative direction for behaviors and poses (Q7, Q8, Q12)

**Algorithm** (pure function, `simulation/direction.py`, run once per item):
1. Take the effective trimmed samples on the behavior's canonical channel and set pan to 0. FK uses
   `gripperframe` relative to the pan axis.
2. Weight each sample by half the distance to its neighbours (time-weighted, so irregular sampling is
   handled).
3. **Valid** samples have ρ ≥ 60 mm. **Side:** NORMAL if |φ| < 90°, otherwise REVERSED. This is
   unambiguous, because valid samples are always within 7.7° of 0° or 180°.
4. **ψ** = the weighted circular median of the valid samples: argmin Σ wᵢ·|wrap180(ψ − ψᵢ)|. A 0.1°
   grid costs under a millisecond.
5. **Report:** ψ, class, usable-time fraction, minority-side time fraction, weighted p90 and max
   deviation, and whether it is compatible.

**Classification thresholds** (set from the geometry; your data is far from all of them):

| Class | Rule |
|---|---|
| NORMAL / REVERSED | usable ≥ 80% of the effective duration **and** minority side ≤ 5% of the usable time |
| MIXED | minority side > 5% (the gripper passes over the pan axis): **incompatible** |
| UNDEFINED | usable < 80% (too long within 60 mm of the axis, i.e. the arm nearly vertical): **incompatible** |

Your data: minority 0%, usable 100%.

**Q7, which estimator:** the circular median on wrapped distances. It's robust to a few near-axis
samples and correct across ±180°. An arithmetic mean or median is wrong on your data. A circular mean is
fine for these clean recordings but less robust.

**Q8, ambiguity:** detected through the *side* fractions and the near-axis time, not through angular
spread. Angular spread is tiny by construction on this robot, so it can't reveal a recording that
crosses over.

**Q12, caching:**
- **Cost:** FK takes about 38 µs per sample, so a whole behavior takes 2–7 ms. It is computed **when a
  behavior is loaded, edited or re-trimmed**, never during playback.
- **Cache key:** behavior id, revision, effective trim, canonical channel, model manifest sha256, pan
  mapping sign, non-pan display offsets, calibration fingerprint and algorithm version.
- **Storage:** in memory, with an optional derived file `behaviors/.derived/direction.json`. **Behavior
  files are not modified**, and no migration is needed.
- **Poses and rest:** saved poses and the rest pose use the same function, computed when the pose is
  saved, loaded or adopted.
- **The controller never runs FK.** It receives ψ with each request. For the rest pose and for a
  slider-set pose, the app injects a `direction_fn` that the controller calls only on the event (rest
  loaded or recorded; Follow Me enabled with no active behavior), never per tick.

## 4. Follow Me control (Q9, Q10, Q11)

**Which ψ is active:**

| Situation | ψ used |
|---|---|
| A behavior plays | that behavior's ψ, fixed for the whole run |
| A pose move | the target pose's ψ, from the start of the move |
| The rest return | the rest pose's ψ |
| Holding after a completed behavior ("hold last position") | that behavior's ψ |
| Armed with sliders, nothing else | ψ computed once when Follow Me is enabled |

A class is **never recomputed per tick**.

**Pan tracker** (an online trajectory generator, overlay only; not a global limiter):
- **State:** (q, v), starting from the **last pan command sent**, so enabling it never jumps.
- **Each tick:** the solver gives q* (the latest target only, with a 0.5° deadband). Then:

  ```
  v_des = sign(e)·min(v_max, √(2·a_max·|e|)),
  v ← v + clamp(v_des − v, ±a_max·dt),
  q ← q + v·dt
  ```

  This is continuous in position and velocity, never overshoots, and needs no queue.
- **Limits:** `v_max` and `a_max` are the existing ADR-015 per-joint transition limits scaled by a preset
  (default Normal: pan 72°/s, 225°/s²; Fast: 120°/s, 500°/s²). That is the same infrastructure and the
  same limits as every planned transition. **No M1 limiter returns.**
- **Typical times (Normal):** 23° ≈ 0.64 s, 90° ≈ 1.6 s, the full 209.8° ≈ 3.2 s (Fast: 0.43 s, 1.0 s,
  2.0 s).
- **Stale input:** if updates stop, the last target is held, never reset to 0.

**Q9, a reversed behavior beyond the limits:**
- **Behavior:** the pan goes to the nearest bound, measured by bearing residual. The status is
  **LIMITED** (residual ≤ 20°) or **UNREACHABLE** (more than 20°, the threshold is configurable), and the
  residual is reported.
- **No false success:** tracking success is never claimed in either case.
- **Hysteresis:** when the target sits opposite the forbidden sector, the two bounds have almost equal
  residuals. Stay at the current bound unless the other one is better by more than 10°, which prevents
  a 210° swing between bounds.

**Q10, switching between normal and reversed behaviors** (ψ jumps 180°):
1. **One deliberate move.** q* jumps by up to 180° (clamped to the bounds) and the tracker executes it
   as one bounded move. The status shows `REORIENTING` with the expected duration and the end-state
   reachability.
2. **No repeated flips.** They can't happen, because ψ and the class are fixed per active item and only
   change when that item changes.
3. **The entry transition must ignore pan.** When Follow Me is active, the behavior's entry is planned
   with pan held. Otherwise a 105° pan delta that will never be executed would stretch a 0.4 s entry to
   1.4 s. This is a small option on `compile_plan`.

**Q11, coexistence with the controller** (checked against `coordinator.py`):

| Existing feature | Interaction / required change |
|---|---|
| **Merge point** | `tick()`: the active motion (behavior, pose, rest, simulation, sliders) produces `requested`, the overlays replace pan and/or gripper, then the existing calibrated clamp, **one** `robot.send`, read. No new thread, driver or loop. |
| **Preemption** (ADR-018) | Behavior→behavior, →pose, →rest: the overlays don't change. `_handover_start` already starts from the last sent target, which includes the overlay values, so there is no jump. |
| **Loops and endings** | Unchanged for the five joints. An ending pose or rest gets that item's ψ. |
| **HOLDING** | **Change needed.** HOLDING currently sends nothing (a tested M1/M2 property: the servos keep their last target). To follow while holding a pose, rest or "hold last position", **HOLDING must send while an overlay is active**. Emergency Hold also uses HOLDING, so Emergency Hold **turns the overlays off**, and HOLDING after an Emergency Hold still sends nothing. |
| **Rest arrival** | `_check_rest_arrival` compares all six joints. With Follow Me it must skip pan (and the gripper while Talk runs), or it reports "Rest not reached" every time. The rest-line duration should also leave out the pan delta. |
| **Go Rest** | As requested: the five joints go to rest and pan keeps following. **This changes what "Go Rest" means** (see decision D3). |
| **Pause / Resume** | Pause holds the **whole** arm, including pan and gripper: the overlays freeze. Target updates during the pause are kept (latest value) and applied after Resume. The resume move then reaches the frozen overlay value, and the tracker continues from there. |
| **Emergency Hold, Torque OFF, fault, mode change** | Both overlays turn **off**. They must be re-enabled explicitly; for the API, only after the emergency latch is released in the Studio. |
| **Sliders (ARMED)** | When an overlay owns a joint, that joint's slider is disabled. |
| **Simulation Only** | Studio Play in Simulation Only is the UI preview, not the controller. Overlays would apply only to controller-owned simulated motion (sliders, simulated Go Pose, simulated Go Rest). See D6. |

## 5. Talk Mode (independent gripper overlay)

**T1, independent overrides:** yes. Both are per-joint replacements at one merge point after the motion
source. The trajectory engine is unchanged apart from the two small options above (entry and rest
ignoring overlay joints).

**T2, merge order:** motion → Follow Me (pan) → Talk (gripper) → calibrated clamp → send. The two
overlays own different joints and never conflict.

**T3, starting without a jump:** Talk starts from the **last gripper command sent**. Its first segment is
a min-jerk move from that value.

**T4, stopping:** offset decay. With Δ = g_talk − g_motion at stop time:

```
g(t) = g_motion(t) + Δ·(1 − s(u)),  T = max(0.25 s, the min-jerk minimum for |Δ|)
```

Position is continuous and the motion keeps animating underneath. Disabling Follow Me uses the same
decay on pan.

**T5, ranges and speeds** (from your data and the existing verified limits):
- **Your hand-guided gripper speeds:** p50 0–30, p95 67–70 units/s (88–92°/s); one spike of 475.
- **Your gripper use:** 1–33 units.
- **Fixed limits** (ADR-015, verified for transitions, B11): gripper ≤ 150 units/s, ≤ 700 units/s².
- **Hardware caveat:** the gripper was never tested for fast motion. **M2-B06 has not run**, and the
  power and voltage faults (gripper "Input voltage error", motor 4) are unexplained.
- **Proposed defaults** stay inside the ADR-015 limits. Closed level = **3 units**, not 0, so the jaw
  doesn't stall against its stop.

| Preset | Opening above closed | v_max | a_max | Pause chance (0.15–0.45 s) | Typical open–close rhythm |
|---|---|---|---|---|---|
| Gentle | 0–10 units (13°) | 60 u/s | 300 u/s² | 25% | ≈ 1.5 Hz |
| Normal | 0–20 units (26°) | 100 u/s | 500 u/s² | 15% | ≈ 1.5 Hz |
| Excited | 0–32 units (42°) | 150 u/s | 700 u/s² | 8% | ≈ 1.4 Hz, larger |

**Rhythm limit:** acceleration, not speed, sets the rhythm. A min-jerk segment needs
T ≥ √(5.77·Δ/a_max): for example 0.26 s for a 6-unit opening at Normal. The rhythm estimates include the random duration factor (mean 1.3) and the pauses. Real speech
syllables run at 4–5 Hz, so reaching them needs about 7–11× more gripper acceleration (the rhythm scales with √a). **Raising that needs a supervised
gripper test first**, and I don't recommend it before the power issue is understood.

**T6, natural movement without jitter:**
1. **Segments, not per-tick noise.** Each segment is a min-jerk move whose duration is the minimum
   allowed for its size Δ, multiplied by a random factor U(1.0, 1.6).
2. **Openings and closings alternate.**
   - **Open** = closed + A·f, with f ~ Beta(2, 5): mostly small, mean 0.29. With probability 0.1 an
     emphasis draw uses Beta(5, 2).
   - **Close** = closed + U(0, 0.15)·A.
3. **Pauses** are holds at the closed level.
4. **Uniform targets look like yawning:** a uniform distribution puts half of all openings above A/2.
5. **Reproducible:** a seeded `random.Random(seed)` makes the motion repeatable in tests.

At 20 Hz, a segment of at least 0.25 s gets 5 or more ticks.

**T7, a behavior superseded while Talk runs:** Talk continues untouched; only the five behavior-owned
joints change.

**T8, precedence:**
- **Go Rest:** cancels Talk (as requested). The rest line starts from the last sent target, which
  includes the Talk gripper value, so there is no jump.
- **Pause:** freezes the generator, and Resume continues it.
- **Emergency Hold, Torque OFF, fault:** Talk turns off immediately, and the API cannot restart it while
  the emergency latch is set.

**T9, duplicate requests and client failures:**
- **Repeated start:** `start` while Talk is active keeps the same generator. It renews the lease, and a
  new intensity applies from the next segment.
- **Stop when inactive:** `stop` while inactive is a 200 no-op.
- **Lease:** `lease_s`, default 10 s, range 1–60.
  - **Expiry:** the gripper glides back to the motion's gripper value and the status says `expired`.
  - **Delayed stops:** the client sends `lease_s ≈ remaining audio + 0.3 s` (renewed for long audio).
    A late or lost stop then overruns by at most that margin.
- **Latency:** the state changes on the next tick (≤ 50 ms) after the controller handles the request.

**T10, testable without the arm:** yes. FakeRobot plus the fake clock covers the logic. MuJoCo
simulation covers the visual checks.

## 6. API (extends M2.5, same token, same gates)

| Route | Semantics |
|---|---|
| `POST /api/v1/follow` `{enabled, target_angle_deg?, transition?}` | A ledger command (enable or disable). It goes through the physical-control gate, the emergency latch and readiness, like any motion. |
| `PUT /api/v1/follow/target` `{angle_deg}` | Writes a **latest-value slot**, like the slider intent: no queue, and no ledger record per update (counters only). Returns 202 at once, or 409 if Follow Me is off. |
| `GET /api/v1/follow/status` | enabled, θ target, F, active ψ and class, q* and q, facing estimate `wrap(q + ψ − F)` (fixed ψ, no live FK), REACHABLE / LIMITED / UNREACHABLE / REORIENTING / SUSPENDED, residual, last update age. |
| WebSocket `/api/v1/follow/targets` | Bearer token in the `Authorization` header of the upgrade request (query-string tokens refused). Messages `{"angle_deg": x}` go into the same slot; 5–20 Hz is fine. Status is pushed at ≤ 5 Hz. The socket closes on disable or Emergency Hold. Needs `websockets` in the `api` extra and uvicorn `ws` enabled. |
| `POST /api/v1/talk/start` `{intensity, lease_s?, seed?}`, `POST /api/v1/talk/stop`, `GET /api/v1/talk/status` | As in §5. Start and stop are ledger commands (idempotent); status covers active, target, intensity, owner, lease left, last start/renew, and the refusal reason. |

**Video is not affected:** the 5 FPS video stays independent.

## 7. Studio UI

**Main panel:** a compact "Follow Me (pan only)" card with:
- an on/off toggle and a target angle (spin box or dial);
- the actual facing, and the active ψ with NORMAL / REVERSED / MIXED;
- a reach badge (REACHABLE / LIMITED / UNREACHABLE with the residual, or REORIENTING).

Next to it, Talk: on/off, Gentle / Normal / Excited, and a live mouth indicator.

**Advanced:** "Set front = current facing", the thresholds, the tracker preset and the Talk parameters.

**Simulation Only:** the target can be set by hand there for testing.

## 8. Decisions I need from you

| # | Decision | My recommendation |
|---|---|---|
| D1 | Angle convention | +θ = clockwise from above = the direction of +pan = the robot's right, which matches image-x for a future camera. |
| D2 | Front reference | Configurable F, set with "Set front = current facing". **Also consider rotating the base about 75°** (§2) so your reversed content can follow ±105° instead of −30°…+180°. |
| D3 | Go Rest while Follow Me is on | As requested (pan keeps following), plus a separate "Stop following" control. The rest pan is then reached only when Follow Me is off. |
| D4 | Pause | Freezes the whole arm, including both overlays (as above). |
| D5 | MIXED or UNDEFINED behavior while Follow Me is on | The behavior still plays. Follow Me is **SUSPENDED** (pan held at its last target, reported) until a compatible item becomes active. The alternative is to refuse Play. |
| D6 | Simulation-Only testing with behaviors | **Move Simulation-Only playback into the controller** (a simulated run: it computes targets and never sends). The Studio and the API then share one player in simulation too, and API Play works in Simulation Only. The alternative is to test with `--fake-hardware` only. |
| D7 | WebSocket | Narrow the VR scope exclusion (`websockets?` → keep `webxr`, `teleop`, `vr_`), add `websockets` to the `api` extra, enable uvicorn `ws`. |
| D8 | HOLDING sends while an overlay is active | Yes, with the Emergency Hold exception above. |
| D9 | Talk speed | The defaults above (inside the verified ADR-015 limits). A faster rhythm only after a supervised gripper test and the power check. |
| D10 | The behavior's own recorded pan | Ignored while Follow Me is on (it owns pan), and blended back in when Follow Me is turned off. |

## 9. Proposed automated tests (fake hardware, fake clock, MuJoCo FK)

1. **Geometry:**
   - the pan axis is vertical (axis −z);
   - bearing = q + ψ to 1e−9 at many pan angles and configurations;
   - measuring from the base origin is shown to be wrong;
   - ψ ≈ 0° for the extended arm and ≈ 180° for the rest configuration.
2. **Direction metadata:**
   - your three recordings classify REVERSED and compatible (golden values);
   - a synthetic crossing behavior is MIXED;
   - a near-axis behavior is UNDEFINED;
   - wrap-around (+179° / −179°) is handled;
   - irregular sampling uses the time weights;
   - trim changes the result;
   - the cache key invalidates on a new revision, trim or model;
   - behavior files stay byte-identical.
3. **Solver:**
   - the request's examples (normal 0 / +23 / +90; reversed 180 / 120; reversed 0 UNREACHABLE with a
     75° residual);
   - at most one in-range k;
   - bound hysteresis (no flapping);
   - never REACHABLE when the residual exceeds the threshold.
4. **Tracker:**
   - position and velocity continuous; within the preset limits;
   - latest value wins (100 updates in one tick → one target);
   - stale input holds the last target;
   - deadband.
5. **Controller integration:**
   - Talking loop + Follow Me: the five joints are byte-identical to playback without Follow Me, and pan
     tracks;
   - Talking → Celebration → pose → natural ending → Go Rest, with Follow Me untouched throughout;
   - normal↔reversed switch = one REORIENTING move;
   - entry not stretched by the overlay pan;
   - rest arrival ignores overlay joints;
   - HOLDING sends only while an overlay is on;
   - Pause freezes the overlays and Resume continues;
   - Emergency Hold, Torque OFF and faults turn the overlays off, and the API is refused while latched;
   - one `send` per tick, all from the controller thread.
6. **Talk Mode:**
   - a seeded run is reproducible;
   - every segment is within the preset's v/a and the calibrated bounds;
   - no per-tick jitter (bounded sign changes per second);
   - the start begins at the current command;
   - the stop decays smoothly back to the motion's gripper;
   - duplicate start = one generator plus a renewed lease;
   - stop is idempotent;
   - the lease expires;
   - Go Rest cancels Talk;
   - simultaneous Follow Me + Talk + behavior.
7. **API:**
   - auth on every route and on the WebSocket upgrade;
   - latest-value target updates at 20 Hz over HTTP and the WebSocket, with no ledger growth;
   - enable and start are refused when the gate is off or the latch is set;
   - the Studio and the API produce identical targets.
8. **Simulation:** top-down renders of the solved poses, as in the evidence image.
9. **Regression:** the full M1/M2/M2.5 suites, ruff and mypy.

## 10. Out of scope (unchanged)

- inverse kinematics or Cartesian tracking;
- perception or ManySight;
- an AI agent;
- speech generation, audio or lip-sync;
- Milestone 3;
- any physical test without your separate approval.

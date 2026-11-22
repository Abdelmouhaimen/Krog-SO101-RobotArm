# ADR-019: Follow Me and Talk Mode overlays (Milestone 2.6)

**Status:** Implemented 2026-11-07, based on the owner-approved review
(`planning/FOLLOW_ME_TALK_DESIGN_REVIEW.md`) and decisions D1–D10. Tested offline with fake hardware and
MuJoCo, then in two owner-supervised real-arm Studio sessions on 2026-11-07 (partial; see
`IMPLEMENTATION_STATUS.md`). **Overlays over the API on the real arm: NOT RUN.**

**Does not change:**
- ADR-010's fixed control policy (calibrated bounds −5°, no step limiter, `max_relative_target=None`);
- the LeRobot adapter;
- calibration;
- the electrical and communication fault handling;
- the manual emergency controls.

## Decision

1. **Two independent, persistent overlays** on top of the existing single controller:
   - **Follow Me** owns `shoulder_pan` while it is enabled.
   - **Talk Mode** owns `gripper` while it is active.
   - **Everything else:** the active behavior or pose owns the other joints (and pan / gripper when no
     overlay owns them).
   - **One merged target per tick:** the controller composes it in `tick()`: motion target → Follow Me
     → Talk Mode → the unchanged calibrated clamp → one `send`. There is no second loop, thread, driver
     or serial connection.
   - **The base is kept separately:** the controller keeps the motion's own target in `_base`, so an
     overlay's output never feeds back into it.

2. **Geometry**, verified on the official model with FK:
   - **Reference point:** angles are measured about the **shoulder-pan axis**, which is vertical and
     38.8 mm in front of the base origin, at the `gripperframe` site.
   - **Convention:** clockwise seen from above = +pan = the robot's right (D1); s = +1.
   - **Equations:** with ψ the behavior's or pose's representative direction (pan held at 0) and F the
     configured front:

     ```
     facing = wrap180(s·q + ψ − F),
     q      = (F + θ − ψ + 360k) / s
     ```

   - **Choosing k:** the calibrated travel is < 360°, so at most one k fits the bounds. Otherwise the pan
     goes to the bound with the smaller residual, with 10° hysteresis, and the target is reported as
     LIMITED (≤ 20°) or UNREACHABLE (`AT_LIMIT`, with the residual). That is never reported as tracking.
   - **Near ties** prefer the current bound, or else the bound nearer the current pan. This fixes a
     ~178° swing seen on the real arm.
   - **No display offsets:** FK uses the configured joint *signs* and none of the display offsets.

3. **One fixed direction per behavior, pose and rest pose**
   (`behaviors/direction.py`, `simulation/direction.py`):
   - **Method:** FK of the effective (trimmed) samples with pan normalized; samples within 60 mm of the
     axis excluded; a time-weighted circular median.
   - **Classes:** NORMAL / REVERSED, or MIXED (more than 5% of the usable time on the other side of the
     axis) or UNDEFINED (under 80% usable). MIXED takes precedence over UNDEFINED.
   - **Cache key:** id, revision, trim, channel, calibration, model identity, mapping signs and the
     algorithm version.
   - **When it runs:** at load or edit time, never per tick. Behavior files are never modified.
   - **How the controller gets it:** directions arrive in the play / pose requests. The controller has
     its own analyzer for event-time use only (rest pose, current pose).
   - **After slider moves:** the controller re-measures the current pose once the sliders have settled
     (0.3 s), so targets stay absolute after the arm is reshaped by hand.

4. **Pan tracker:** an online second-order generator.
   - **Continuity:** continuous position and velocity, discrete-time braking, overshoot at most a·dt²/2
     with no oscillation.
   - **Limits:** the existing ADR-015 per-joint transition limits times the preset (default Normal:
     72°/s, 225°/s²).
   - **Targets:** latest value only (a lock-protected slot shared by HTTP, the WebSocket and the
     Studio), with a 0.5° deadband.
   - **Stalls:** the overlay `dt` is stall-aware (capped at 2 periods), so a serial stall delays the
     motion and never jumps.

5. **Ownership changes are continuous:**
   - **Enabling** starts from the last pan command sent.
   - **Disabling** ("Stop following", D10) and Talk stopping use a min-jerk offset decay back to the
     motion's own, possibly moving, value.
   - **Shorter motions:** motions plan with overlay-owned joints already at their goal, so a pan Follow
     Me will override never stretches an entry, rest line, pose move or resume move.

6. **Semantics (D3–D8):**

   | Situation | Behavior |
   |---|---|
   | Behavior replaced / Go Pose / natural ending | Overlays survive. The ending runs for the joints no overlay owns. No inserted rest. |
   | Go Rest with Follow Me ON | The five joints go to rest and pan keeps following. The arrival check skips overlay-owned joints. |
   | Go Rest with Follow Me OFF | Unchanged full six-joint rest. |
   | Go Rest and Talk Mode | Go Rest cancels Talk. The rest line takes the gripper from its last command. |
   | Pause / Resume | The whole arm holds, overlays included. New targets are stored; Resume continues smoothly toward the latest one. |
   | MIXED / UNDEFINED owner | Follow Me is SUSPENDED: pan holds its last valid target, the other joints continue, the reason is reported. It resumes on a new compatible owner or an explicit operator re-check (Follow ON again). |
   | Holding with an overlay engaged | The merged target is sent every tick. Without overlays, holding still sends nothing. |
   | Emergency Hold, Torque OFF, fault, Disarm, mode change, recording | Every overlay stops at once. The API cannot restart them while the emergency latch is set. Turning an overlay *off* is always allowed. |

7. **Talk Mode:** seeded procedural animation.
   - **Motion:** alternating min-jerk openings (Beta(2,5) sizes, occasional Beta(5,2) emphasis) and
     closings, random durations, brief pauses.
   - **Presets:** gentle, normal and excited, all within the ADR-015 gripper limits (≤ 150 units/s,
     ≤ 700 units/s²) and at most about 1.5 Hz (segment ≥ 0.34 s), as decided in D9.
   - **Closed level:** 3 units, so the jaw never stalls against its stop.
   - **Lease:** 1–60 s, default 10. Repeating `start` renews it without a second generator; `stop` is
     idempotent.

8. **Front reference** (D2):
   - **Setting it:** "Set front = current facing", a manual bearing, or the pan center.
   - **Storage:** per calibration fingerprint and model identity in `config/follow_front.json`
     (gitignored), in memory under fake hardware.
   - **Reach:** the reachable target range for the active reference is always reported.

9. **Simulation Only** (D6):
   - **Controller-owned motion:** overlays apply to sliders, simulated Go Pose and Go Rest.
   - **Studio 3D playback:** while an overlay is engaged, the timeline preview feeds its pose to the
     controller (`set_sim_preview`, with its direction and paused state). The one composer then adds
     the overlays, and the window takes the overlay joints from the composed result.
   - **Unchanged:** with no overlay engaged, preview, scrub, pause and rendering behave as before.

10. **API** (same token, gates and latch):
    - **Routes:**
      - `POST /api/v1/follow`;
      - `PUT /api/v1/follow/target`, latest value, no ledger record per update;
      - `GET /api/v1/follow/status`;
      - WebSocket `/api/v1/follow/targets`, with a Bearer header on the upgrade;
      - `POST /api/v1/talk/start` and `/talk/stop`, `GET /api/v1/talk/status`.
    - **WebSocket scope (D7):** WebSocket is enabled only for this route (uvicorn `websockets-sansio`).
      The VR scope test was narrowed to allow just the word "websocket" in the two API files, and a test
      pins the single route.

## Consequences

- **The owner's recordings, rest pose and saved pose are all REVERSED** (ψ ≈ 180°). With the front set
  to their working direction (about 105° clockwise of the pan center) they follow −30°…+180°. The
  software reports this range; rotating the base is optional.
- **Studio changes:**
  - a compact Follow Me / Talk card;
  - pan and gripper sliders disabled while their overlay owns the joint;
  - reference details, "Set front" and the thresholds in Advanced;
  - confirmation before an overlay starts on the real arm.
- **Not exposed:** IK, Cartesian tracking, audio or speech, VR, perception and M3 are not part of this
  milestone.

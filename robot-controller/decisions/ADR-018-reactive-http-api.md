# ADR-018: Reactive HTTP API (Milestone 2.5)

**Status:** Implemented 2026-11-07 (owner request "Milestone 2.5 — Reactive SO-101 FastAPI Service").
Tested offline with fake hardware. **Real-arm API commissioning: NOT RUN** (it needs the owner's separate
approval).

**Plan:** `planning/API_INTEGRATION_PLAN.md`. **Usage:** `docs/HTTP_API.md`.

**Extends:** ADR-013 and ADR-015/016/017.

**Does not change:**
- ADR-010's fixed control policy (calibrated bounds −5°, no step limiter, `max_relative_target=None`);
- the LeRobot adapter;
- the trajectory engine;
- calibration and joint limits;
- the electrical / communication fault handling;
- the manual emergency controls.

## Decision

1. **One controller, two interfaces.**
   - The HTTP server (FastAPI on uvicorn, in a background thread) submits the *same* request objects
     as the Studio to the *same* `ControllerCoordinator` queue.
   - The controller thread (`ControllerWorker`) stays the only owner of the serial bus.
   - The API reads state only from a lock-protected **published snapshot**.
   - No second controller, no API-specific player, and no direct adapter access.

2. **Command ledger** (`control/commands.py`):
   - Every motion request is tracked: `queued → running ⇄ paused → ending → completed`, or a terminal
     `superseded / cancelled / rejected / failed`.
   - Each record carries `superseded_by` / `replaced` / `cancelled_by`, `after_behavior_cancelled`, and
     latencies (received → handled, received → first target).
   - Studio requests are tracked automatically, as `source: "studio"`.
   - API requests arrive in an `rq.Tracked` envelope, so the request dataclasses do not change.
   - Only the controller thread writes command state.

3. **Reactive preemption** (in the controller, so the Studio and the API behave identically):
   - **Allowed states:**
     - **Play:** permitted while a motion is in flight (PLAYING_BEHAVIOR, MOVING_TO_POSE,
       RETURNING_TO_REST).
     - **Go Pose:** permitted during a rest return too.
     - **Go Rest during a rest return:** takes over the running trajectory.
   - **Takeover** (`_take_over`): the controller drops the current owner and records it as superseded.
     The owner can be:
     - the behavior run, with its future loops and any pending Finish;
     - a pose move, including an "After behavior" ending move;
     - a rest return, including an ending rest.
   - **No return to rest is ever inserted between commands.**
   - **Start pose:**
     - **Always a fresh read first.** A failed read latches the fault and nothing new moves.
     - **Arm still:** the transition starts at the **measured** pose.
     - **Arm in flight:** the transition starts at the **last commanded target**. This is owner decision
       D9 (ADR-015): restarting at a lagging measured pose snaps the target back (up to 24° measured)
       and brakes hard, which is the discontinuous jump this milestone forbids. The measurement and the
       lag are recorded in `motion_superseded`.
   - **Transition:** the existing `SmoothTransition` / `compile_plan` entry transition with the
     requested preset.
   - **Natural completion** still runs the behavior's saved ending, or this run's request-level override
     (which never rewrites the file).

4. **Precedence:** Emergency Hold, Torque OFF and latched faults keep their precedence.
   - **Emergency latch:** every Emergency Hold (Studio or API) sets it.
     - **While set:** the controller refuses every API motion request.
     - **Release:** a direct Studio operator motion or mode action, or a disconnect. The API cannot clear
       it.
   - **Torque:** the API never sets `enable_torque` (the controller refuses it), so it cannot energize
     the motors.

5. **Authentication and exposure:**
   - **Tokens:** Bearer tokens, stored as SHA-256 hashes in `~/.config/so101-companion/api-tokens.json`
     (mode 0600, outside the repository).
     - **Creating one:** `so101-companion --create-api-token NAME` shows the token once.
     - **Checking:** constant-time comparison; an unreadable or world-readable file fails closed.
   - **Binding:** 127.0.0.1 by default. A non-loopback host is refused unless `[api] allow_remote =
     true`.
   - **No dialogs:** API requests open no dialog and are built with `confirmed=True`, but only if the
     service is configured for it.
   - **Physical control gate:** `[api] allow_physical_control` is false by default. The **controller**
     checks it at handling time, so a mode change between the HTTP check and the handling cannot slip
     past it. With the gate off and an arm connected, API motion is refused; in Simulation Only the API
     moves the 3D arm.
   - **Fake hardware:** `--fake-hardware` counts as no physical device.
   - **Starting the server** never connects, arms, or changes the mode.
   - **Headless mode** is fake-hardware only. Real-arm API use goes through the Studio window.

6. **Video:** `SimulationVideo` renders on demand with its own MuJoCo scene and GL renderer, on its own
   thread.
   - The thread runs only while at least one client is subscribed.
   - There is one newest-frame slot (no queue), and frames are always at least 1/5 s apart.
   - The last disconnect stops the thread and closes the renderer.
   - Nothing in the video path touches the control path except reading the published snapshot.
   - It is an RTSP-ready seam: a future adapter is one more subscriber.

## Consequences

- **The Studio gains** the same supersede semantics. In Simulation → Real another behavior can be
  selected and played while one runs, and the confirmation says it replaces the running motion.
- **Event changes:** `behavior_stopped` (operator Go to pose) became `behavior_superseded`. A pose move
  replaced by a newer command emits `pose_move_superseded` (not `pose_move_cancelled`).
- **No physical response time is promised.** With fake hardware the dispatch is about 1 ms and the first
  target about 16 ms (the next tick at 20 Hz). On the arm, a serial stall (100–800 ms observed) delays
  the next tick; the stall-aware clocks delay the motion and never jump.
- **Not exposed over HTTP:** recording, raw joint targets, motor control, calibration writes,
  connect/arm/mode changes, library deletion.

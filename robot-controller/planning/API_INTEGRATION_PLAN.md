# Milestone 2.5: Reactive HTTP API integration plan

**Status:** Plan written 2026-11-07 after a code review, then implemented (ADR-018).
**Scope:**
- An authenticated FastAPI service on top of the **existing** controller, the Behavior Engine and the
  Named Pose Library.
- No second controller, no new trajectory engine, no Milestone 3 work.

## 1. What the code does today (review findings)

| Area | Current design | Consequence for the API |
|---|---|---|
| **Thread ownership** | `ControllerWorker` (one thread) is the only caller of `ControllerCoordinator`, and so the only owner of the serial bus. Other threads may only call `coordinator.submit(request)` (a `queue.Queue`) and `set_requested()`. | The API **submits requests**, exactly like the Studio panel. It never touches `RobotPort` or LeRobot. |
| **Serialization** | Requests are handled one at a time on the controller thread, between ticks. `handle()` re-checks `available_actions()` and the confirmation for every request. | We already have a serialized dispatcher with one authoritative state. The API adds command tracking on top of it, not another queue. |
| **Motion owners** | Three mutually exclusive timed motions: `BehaviorRun` (PLAYING_BEHAVIOR), `PoseMove` (MOVING_TO_POSE) and the rest trajectory (RETURNING_TO_REST). In SIM_ONLY there is a fourth: `_sim_rest`, the 3D arm only. | "One authoritative active motion" already holds. Preemption means **replacing** the owner, never adding a second one. |
| **Preemption today** | Go to pose is allowed while PLAYING or MOVING, and starts from the last target (D9). Go Rest is allowed while PLAYING or MOVING. **Play is refused** while PLAYING, MOVING or RETURNING. Go to pose and Go Rest are refused while RETURNING. | Play must be able to supersede any motion, and the pose and rest requests must also be allowed during a rest return. |
| **Endings** | `_complete_behavior()` starts the "After behavior" move right away (a rest return or a pose move); a Finish request sets `finish_rep`. | A "pending After Behavior action" is either a pending Finish or an ending move in flight. Superseding either one cancels it, and this must be recorded. |
| **Emergency Hold** | Puts the arm in HOLDING, which is the same state a completed motion ends in. Play is allowed from HOLDING. | Without a change, an API Play right after an Emergency Hold would move the arm again. This needs an **emergency latch** that API requests cannot clear. |
| **Confirmations** | `handle()` rejects any request with `confirmed=False`. The Qt panel asks the operator first. | API requests are built with `confirmed=True` only when the service is configured for physical control. The controller enforces that gate itself, not the HTTP layer, so a mode change in the meantime cannot slip past it. |
| **Snapshots** | `coordinator.snapshot()` reads mutable controller state. The worker publishes a snapshot after every tick and request (to the Qt bridge). | The API must read a **published** snapshot (a lock-protected latest copy), never call `snapshot()` from an HTTP thread. |
| **Simulation Play** | In Simulation Only, Studio Play is the panel's MuJoCo preview animation (UI code), not a controller motion. | API Play requires Simulation → Real: the real arm, or `--fake-hardware` for rehearsal. API Go Pose and Go Rest in Simulation Only move the 3D arm, through the same controller paths as the Studio. |
| **Rendering** | `OffscreenRenderer` is tied to the thread that created its GL context (the Qt GUI thread). | The video stream gets its **own** model, data and renderer, created on a dedicated producer thread on demand and released when the last client leaves. |

## 2. Architecture

```
            Studio window (Qt)                      HTTP API (uvicorn thread, FastAPI)
   OverlayConfirmer → BehaviorService ─┐      ┌─ auth → validate → CommandLedger.create
                                       ▼      ▼                    → coordinator.submit(Tracked(...))
                         coordinator.submit()  (one queue)                 │ waits ≤ ack_timeout for the
                                       │                                   │ controller's verdict, never
                  ┌────────────────────▼─────────────────────┐             │ for the motion
                  │ ControllerWorker thread (sole bus owner) │◄────────────┘
                  │ handle(): permissions, emergency latch,  │
                  │ physical-control gate, preemption,       │
                  │ ledger transitions; tick(): one target   │
                  │ per cycle, clamp, send, read             │
                  └───────┬───────────────────────┬──────────┘
          PublishedSnapshot (lock)          CommandLedger (lock)
                          │                       │
        GET /status, video pose source     GET /commands/{id}
                          │
   SimulationVideo producer thread (on demand, ≤5 FPS, its own MuJoCo model + GL renderer) → JPEG
```

- **One process, one controller, two interfaces.**
  - **With the window:** `so101-companion --api` runs the HTTP server in a background thread of the
    Studio process. It uses the window's `BehaviorService` and the same coordinator.
  - **Headless:** `so101-companion --headless --api --fake-hardware [--fake-arm]` is for development and
    tests. It is fake hardware only. Real-arm API use goes through the Studio window, where the operator
    connects and selects Simulation → Real. The service never connects or arms anything because it
    started.
- **Command ledger** (`control/commands.py`):
  - Thread-safe and bounded.
  - Every tracked request gets a record: `queued → running ⇄ paused → ending → completed`, or a terminal
    `superseded / cancelled / rejected / failed`.
  - Each record holds `superseded_by` / `replaced`, `cancelled_by`, timestamps (received, handled,
    started, first send, finished) and the idempotency key.
  - Studio requests are recorded as well (`source: "studio"`), so an API client can see when the operator
    took over.
- **Tracked envelope:** `rq.Tracked(request, command_id, source, physical_allowed)`. The request
  dataclasses do not change, and requests from the Studio without an envelope are auto-tracked.

## 3. Reactive preemption (controller changes, shared by Studio and API)

1. **Allowed states:**
   - **Play:** permitted while PLAYING_BEHAVIOR, MOVING_TO_POSE or RETURNING_TO_REST, as well as from a
     still arm.
   - **Go Pose:** permitted while RETURNING_TO_REST too.
   - **Go Rest:** permitted while RETURNING_TO_REST. It takes ownership of the trajectory already running;
     no restart and no jump.
2. **Takeover** (`_take_over_motion`): the controller drops the current owner and records it as
   superseded.
   - The owner can be a behavior run (including a pending Finish and future loop repetitions), a pose
     move (including an "After behavior" ending move), a rest return (including an ending rest), or a
     simulated move.
   - The old record becomes `superseded` with `superseded_by`. If it was in its ending,
     `after_behavior_cancelled: true` is added.
   - **No return to rest is inserted.**
3. **Start of the replacement transition:**
   - **Always a fresh read first.** A failed read latches the fault, so nothing new moves and the old
     motion is cancelled by the fault.
   - **Arm still** (holding, armed, or paused): the transition starts at that **measured** pose.
   - **Arm in motion:** the transition starts at the **last commanded target**. The fresh measurement and
     the lag between the two are recorded in the `motion_superseded` event.
     - **Why:** this is the owner's D9 decision (ADR-015). Starting from a lagging measured pose snaps the
       target back (up to 24° measured on the arm) and brakes hard, which is exactly the discontinuous
       jump this spec forbids.
     - **What the agent sees:** the arm keeps one continuous commanded path and turns smoothly toward the
       new goal, using the existing entry-transition planner (`SmoothTransition` with the requested
       preset).
4. **Natural completion** still runs the behavior's saved ending, or the request-level override carried
   in the `PlaybackSettings` of that run (the behavior file is not touched).
5. **Precedence:** Emergency Hold, Torque OFF and latched faults keep the precedence they have today.
   - **Emergency latch:** every Emergency Hold sets it, from the Studio or the API. While it is set, the
     controller rejects every API motion request.
   - **Clearing the latch:** only a direct Studio operator motion or mode action clears it, or a
     disconnect. The API cannot clear it.
   - **Torque:** the API never sets `enable_torque`, so it can never energize the motors.

## 4. Authentication and exposure

- **Every `/api/v1` route needs `Authorization: Bearer <token>`,** checked against SHA-256 hashes in a
  token file outside the repository.
  - **Default location:** `~/.config/so101-companion/api-tokens.json`, mode 0600.
  - **Creating a token:** `so101-companion --create-api-token NAME` prints the token once.
  - **Checking:** constant-time comparison.
  - **Failures:** a missing or invalid token gets 401 and nothing is recorded or dispatched.
- **Binding:** the server binds 127.0.0.1 by default. A non-loopback host is refused unless
  `[api] allow_remote = true`. OpenAPI documentation is served; there are no unauthenticated control
  routes.
- **Physical control gate:** `[api] allow_physical_control = false` by default.
  - **Off:** API motion requests are rejected whenever a robot is connected. In Simulation Only the API
    still moves the 3D arm.
  - **Fake hardware:** `--fake-hardware` counts as "no physical device" and enables the gate.
  - **Emergency Hold:** never gated.
- **No dialogs:** API requests open no dialogs. Calibration checks, bounds, the rest pose, torque state,
  faults and every other readiness rule are still enforced by the controller.

## 5. Routes (`/api/v1`)

| Method & path | Purpose |
|---|---|
| `GET /status` | Mode, connection, torque, fault, emergency latch, readiness with reasons, active command, behavior/pose progress |
| `GET /behaviors`, `GET /poses` | Library listings (ids, names, durations, saved settings and endings; pose source and validity for this calibration) |
| `POST /behaviors/{id}/play` | Optional speed 0.5–2, `repeat` (1–1000 or `"until_stopped"`), `getting_into_position` / `between_repetitions` (gentle/normal/fast), `after_behavior` (saved/rest/hold/pose) plus `after_pose_id` |
| `POST /poses/{id}/go` | Optional `transition` preset |
| `POST /rest` | Go Rest (the designated default rest pose) |
| `POST /playback/pause`, `POST /playback/resume` | Studio pause/resume semantics |
| `POST /emergency-hold` | Best-effort hold at the measured pose and the emergency latch |
| `GET /commands/{id}`, `GET /commands` | Lifecycle, progress, errors, latency |
| `GET /simulation/frame`, `GET /simulation/stream.mjpeg` | On-demand JPEG and MJPEG (≤5 FPS) |

**POST behavior:**
- **Responses:**
  - **Accepted:** 202 with the command record. The handler waits at most `ack_timeout_s` (default
    0.25 s) for the controller to *handle* the request, so the response already says `running` or
    `rejected`. It never waits for the motion.
  - **Controller rejection:** 409, with the reason and the record.
  - **Ack timed out:** 202 `queued`; poll `GET /commands/{id}`.
- **Idempotency:** `Idempotency-Key` header.
  - **Same key and same body:** returns the original record and does not dispatch again.
  - **Same key and a different body:** 409.

## 6. Video

- **Shape:** `SimulationVideo` has reference-counted subscribers and one producer thread. The thread
  exists only while at least one client is subscribed.
- **Rendering:** the thread builds its own MuJoCo scene and offscreen renderer, renders the latest
  *published* displayed pose at most every 0.2 s, and JPEG-encodes it with Pillow into a single
  latest-frame slot.
- **Clients:**
  - Each client waits for a sequence number newer than its last one, so a slow client skips frames and
    nothing queues.
  - A stream client unsubscribes in `finally` when it disconnects. The last unsubscribe stops the thread
    and closes the renderer.
- **Performance:** nothing in the video path touches the controller, apart from reading the published
  snapshot.
- **Future RTSP:** the frame source is a small protocol (`subscribe` / `wait_frame` / `unsubscribe`), so
  an RTSP adapter can be added later as one more subscriber.

## 7. Latency and timing (to measure and document, not promise)

- **Dispatch:** HTTP receive → controller handled. With fake hardware this is typically below a few
  milliseconds, because `submit()` wakes the worker at once.
- **First target:** the first target of the new motion goes out at the next control tick, ≤ 1 period
  (50 ms at 20 Hz).
- **On real hardware:** a tick can be delayed by a slow serial read or write (stalls of 100–800 ms were
  observed, ADR-013/014). The stall-aware clocks delay the motion; they never jump. This is documented as
  a limit, with no fixed physical response time promised.

## 8. Tests (fake hardware)

- **Controller level** (deterministic clock):
  - finite and infinite play;
  - pose supersedes a loop; behavior A → B;
  - the ending is cancelled by preemption;
  - no intermediate rest; start pose and continuity;
  - the saved ending, and a request-level override with the file unchanged;
  - pause/resume and Go Rest parity;
  - Emergency Hold and fault precedence, and the emergency latch;
  - Studio and API requests produce the identical target sequence.
- **API level** (TestClient and a real worker thread):
  - auth (401 and nothing dispatched); non-blocking POST;
  - idempotency; concurrent POSTs (one active motion; sends stay monotonic and come from one thread);
  - the physical gate; dispatch latency measurement.
- **Video:**
  - no producer without clients; ≤5 FPS; release when the last client leaves;
  - the control loop rate is unaffected while streaming (fake renderer, plus an optional real EGL render
    test);
  - a real uvicorn server with a streaming client that disconnects.
- **Regression:** the full Milestone 1 and 2 suites, ruff and mypy.

## 9. Not in scope

- Recording, raw joint targets, motor control, calibration writes, connect/arm/mode changes and library
  deletion over HTTP.
- RTSP.
- Milestone 3 (perception, agent, speech).
- Physical API commissioning, which needs the owner's separate approval.

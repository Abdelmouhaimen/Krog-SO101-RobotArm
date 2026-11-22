# SO-101 HTTP API (Milestone 2.5)

The API controls the SO-101's saved behaviors and named poses, so an external program (the future
Milestone 3 companion) can drive the arm. It is a **second interface to the same controller** as
SO-101 Studio, with the same movements, checks and safety behavior (ADR-018). The interactive OpenAPI
documentation is at `http://127.0.0.1:8765/docs` while the service runs.

> **Real arm:** the API has been tested with fake hardware only. Using it on the physical arm needs the
> owner's separate approval and a supervised session (see the last section).

## 1. Install and start

```bash
uv pip sync --python .venv/bin/python --torch-backend cpu requirements/hardware-linux-x86_64-py312.lock
.venv/bin/python -m so101_companion --create-api-token my-agent   # prints the token ONCE; keep it secret
```

The token's SHA-256 hash is stored in `~/.config/so101-companion/api-tokens.json`, mode 0600, outside
the repository. You can override the location with `[api] token_file` or `SO101_API_TOKEN_FILE`.
To revoke a token, delete its entry from that file; the change takes effect without a restart.

| How | Command | Notes |
|---|---|---|
| **Studio + API** (normal) | `so101-companion --api [--config config/local.toml]` | The window title shows the API URL. The operator connects and selects modes in the window, as before. |
| **Headless, fake arm** (development) | `so101-companion --headless --api --fake-hardware --fake-arm` | No window. A FAKE arm is connected and put in Simulation → Real, and its start pose becomes the rest pose. Prints the URL as JSON. |
| **Allow the real arm** | add `--api-allow-physical-control`, or `[api] allow_physical_control = true` | Off by default. Without it, API motion is refused while an arm is connected. |

**Local config is automatic:** without `--config`, `so101-companion` loads the gitignored
`config/local.toml` (from the current directory, else the repository) when it exists. So putting
`[api] enabled = true` and `allow_physical_control = true` there makes plain `so101-companion` equal to
`--api --api-allow-physical-control`. Set `SO101_COMPANION_CONFIG=<path>` to choose another file, or
`SO101_COMPANION_CONFIG=` (empty) for the built-in defaults only. The tests always use the built-in
defaults.

**Headless mode refuses real hardware:** a real arm is always connected and armed by the operator in the
Studio window. Starting the service never connects or arms anything.

**`[api]` configuration** (`config/default.toml` shows every key):

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Loopback only. Any other host also needs `allow_remote = true`. |
| `port` | `8765` | |
| `allow_physical_control` | `false` | Let authenticated requests move the real arm when the controller is ready. |
| `ack_timeout_s` | `0.25` | A POST waits at most this long for the controller's verdict, never for the motion. |
| `stream_max_fps` | `5` | MJPEG frame rate. 5 is also the hard maximum. |
| `frame_width`, `frame_height`, `jpeg_quality` | 640, 480, 80 | Video settings. |

## 2. Authentication

**Header:** every `/api/v1` route needs `Authorization: Bearer <token>`.

**Failures:** a missing or invalid token gets `401` before anything is recorded or sent to the robot.

**What a token does not do:** it skips the confirmation dialogs, nothing else. Calibration, joint
limits, rest pose, torque, faults and every readiness check still apply.

## 3. Routes

| Method & path | What it does |
|---|---|
| `GET /api/v1/status` | Mode, connection, torque, fault, `emergency_hold_latched`, per-action `ready` (with a reason when false), `active_command`, behavior and pose progress, pose |
| `GET /api/v1/behaviors` | Saved behaviors (id, name, duration, `saved_settings` including the saved ending) |
| `GET /api/v1/poses` | Named poses (id, name, source), and whether each is usable with the connected arm's calibration |
| `POST /api/v1/behaviors/{id}/play` | Play a behavior. Body (all optional): see below. |
| `POST /api/v1/poses/{id}/go` | Move smoothly to a pose, then hold. Body: `{"transition": "gentle"\|"normal"\|"fast"}`. |
| `POST /api/v1/rest` | Return to the designated default rest pose. |
| `POST /api/v1/playback/pause` / `resume` | Studio Pause/Resume: hold at the measured pose; on resume, rejoin the trajectory smoothly. |
| `POST /api/v1/emergency-hold` | Best-effort hold at the measured pose. Latches against further API motion. |
| `GET /api/v1/commands/{id}` / `GET /api/v1/commands?limit=50` | Command lifecycle, progress, errors and latency |
| `GET /api/v1/simulation/frame` | One JPEG of the MuJoCo view |
| `GET /api/v1/simulation/stream.mjpeg` | MJPEG stream, at most 5 FPS |
| `POST /api/v1/follow`, `PUT /api/v1/follow/target`, `GET /api/v1/follow/status`, WebSocket `/api/v1/follow/targets` | Follow Me (§4b) |
| `POST /api/v1/talk/start`, `POST /api/v1/talk/stop`, `GET /api/v1/talk/status` | Talk Mode (§4b) |

**Play body** (overrides apply to this run only; the behavior file is never modified):

```jsonc
{
  "speed": 1.5,                       // 0.5–2.0, default: saved
  "repeat": 3,                        // 1–1000 or "until_stopped", default: saved
  "getting_into_position": "normal",  // gentle | normal | fast
  "between_repetitions": "fast",      // gentle | normal | fast
  "after_behavior": "saved",          // saved | rest | hold | pose
  "after_pose_id": null               // required when after_behavior = "pose"
}
```

**Not available over HTTP:** recording, raw joint targets, motor control, calibration writes,
connect/arm/mode changes, and deleting behaviors or poses.

## 4. Commands are reactive

**Every POST returns at once:**

| Response | Meaning |
|---|---|
| `202` + `"state": "running"` | The controller accepted the command and the motion has started. |
| `202` + `"state": "queued"` | The controller had not handled it within `ack_timeout_s` (for example, a slow serial cycle). Poll it. |
| `409` + `"state": "rejected"` | The controller refused it, and nothing moved. `detail` says why (for example "Emergency Hold is latched", "torque is OFF", "fault … is latched"). |
| `200` + `"idempotent_replay": true` | A retry with the same `Idempotency-Key` returns the original command and sends nothing again. The same key with a different body gets `409`. |
| `404` / `422` / `503` | Unknown behavior or pose / invalid body / controller not running. |

**A new motion command supersedes the running one:** play, pose and rest commands are never queued
behind each other.

| Running now | New request | Result |
|---|---|---|
| Talking, looping forever | Go to pose Watching | Talking stops at once; the arm turns smoothly toward Watching. Talking becomes `superseded` (`superseded_by` = the pose command). |
| Celebrating | Play Talking | Celebrating is cancelled, and the arm transitions directly into Talking's entry. |
| Any behavior | Go Rest | The behavior is interrupted and the arm returns to the default rest pose. |
| A behavior's "After behavior" move in progress | Anything | That ending is cancelled (`after_behavior_cancelled: true`). |
| Nothing new | (the behavior ends by itself) | Its saved ending runs, or this run's override. The command is `completed` when the ending is done. |

**Rules for every takeover:**
- No return to rest is inserted.
- The future trajectory, its loop repetitions and any pending Finish or ending are cancelled.
- A fresh position read always comes first.
- **Start of the new move:**
  - **From a still arm:** the new move starts at the **measured** pose.
  - **While the arm is moving:** it starts at the **last commanded target**, so the commanded path never
    jumps. Restarting at a lagging measured pose would snap the target back and brake hard (owner
    decision D9). The measured pose and the lag are logged (`motion_superseded`).

**Command states:**
- **Active:** `queued` → `running` ⇄ `paused` → `ending` → `completed`.
- **Terminal:**
  - `superseded`: replaced by a newer motion command;
  - `cancelled`: Emergency Hold, Torque OFF, Disarm, or a mode change by the operator;
  - `failed`: a fault was latched, or rest was not reached;
  - `rejected`: refused by the controller.

**Studio commands appear in the same ledger** (`source: "studio"`), so a client can see when the
operator took over.

**Precedence:**
- **Emergency Hold** (from the window or the API) cancels the running command and **latches**. API motion
  stays refused until the operator makes a direct motion or mode choice in the Studio.
- **Torque OFF and latched faults** cannot be overridden by any API request.
- **Torque:** the API never turns torque on.

## 4b. Follow Me and Talk Mode (Milestone 2.6, ADR-019)

These are two independent **overlays**. They are not motion commands, so they never supersede the
running behavior. Play, Go Pose and natural endings leave them running.

| Overlay | Owns | Everything else |
|---|---|---|
| Follow Me | `shoulder_pan` | the active behavior or pose |
| Talk Mode | `gripper` | the active behavior or pose |

**Angle convention:** `angle_deg` is measured **clockwise seen from above** (toward the robot's right),
starting from the configured front. The operator sets the front once in the Studio: Advanced → Follow Me
→ "Set front = current facing".

```bash
H="Authorization: Bearer $SO101_API_TOKEN"
curl -X POST -H "$H" -d '{"enabled": true, "target_angle_deg": 0}' http://127.0.0.1:8765/api/v1/follow
curl -X PUT  -H "$H" -d '{"angle_deg": 23}' http://127.0.0.1:8765/api/v1/follow/target   # latest value wins
curl -H "$H" http://127.0.0.1:8765/api/v1/follow/status
curl -X POST -H "$H" -d '{"enabled": false}' http://127.0.0.1:8765/api/v1/follow           # Stop following
curl -X POST -H "$H" -d '{"intensity": "normal", "lease_s": 8}' http://127.0.0.1:8765/api/v1/talk/start
curl -X POST -H "$H" http://127.0.0.1:8765/api/v1/talk/stop
```

**Target updates:**
- **Rate:** send them at 5–20 Hz over `PUT /follow/target` or the WebSocket `/api/v1/follow/targets`.
  Only the latest value is used, and no command record is created per update.
- **WebSocket token:** it goes in the `Authorization` header of the upgrade request. Query-string tokens
  are refused.
- **WebSocket replies:** `{"angle_deg": x}` gets an ack, and a full status at most every 0.2 s.
- **Disconnects and stale input:** if updates stop or the socket closes, the last target is held. It is
  never reset to 0.

**Proxies:** Python `websockets` ≥ 17 honours `HTTP(S)_PROXY`. For a local robot, connect with
`proxy=None`; through a proxy the upgrade fails with HTTP 400.

**Follow status:**
- **State:** `OFF`, `TRACKING`, `REORIENTING`, `AT_LIMIT` (cannot face the target: see `reach` and
  `residual_deg`), `SUSPENDED` (the active behavior's direction is MIXED or UNDEFINED, so pan holds; see
  `suspended_reason`), `PAUSED` or `HANDING_BACK`.
- **Reach:** `REACHABLE`, `LIMITED` or `UNREACHABLE`.
- **Also reported:** `facing_estimate_deg`, `reachable_range` (the target interval this behavior or pose
  can face), and `reference` (ψ, its class, the usable fraction and the spread).

**Talk status:** `state` (`ON`, `PAUSED`, `STOPPING` or `OFF`), `intensity`, `gripper_command` and
`lease_remaining_s`. A repeated `start` renews the lease, with no second generator. A `stop` while
inactive is harmless.

**Talk lease:** it bounds a crashed client. Send `lease_s` ≈ the remaining audio plus 0.3 s, and repeat
`start` to extend it for long audio. Talk then ends by itself even if the `stop` is late or lost.

**Precedence:**
- **Go Rest** cancels Talk Mode. Pan keeps following while Follow Me is on.
- **Pause** freezes both overlays.
- **Emergency Hold, Torque OFF and faults** stop both overlays, and the API cannot restart them while the
  emergency latch is set. Turning an overlay **off** is always allowed.

## 5. Video

- **Frames are rendered only when requested.** No client means no thread, no GL context and no frames.
  The first client starts rendering, and the last disconnect stops it and releases the renderer.
- **Rate:** at most 5 FPS. A slow client always gets the newest frame, never a growing backlog.
- **Rendering** runs on its own thread with its own MuJoCo model, off the control path.
- **What it shows:** the 3D arm as it is displayed: the measured pose in hardware modes, the simulated
  pose in Simulation Only.

```bash
curl -H "Authorization: Bearer $SO101_API_TOKEN" http://127.0.0.1:8765/api/v1/simulation/frame -o frame.jpg
# In a browser that sends the header (or via a small proxy):  /api/v1/simulation/stream.mjpeg
```

## 6. Timing: measured, not promised

**Measured with fake hardware** (20 Hz controller, `test_dispatch_latency_with_fake_hardware`, 40
samples):

| | Median | Max |
|---|---|---|
| HTTP receive → controller handled the command | ≈ 1 ms | ≈ 1.4 ms |
| HTTP receive → first target of the new motion sent | ≈ 16 ms | ≈ 21 ms (≤ one 50 ms tick) |
| Full HTTP round trip of a POST (TestClient) | ≈ 13 ms | ≈ 15 ms |

**On the real arm, a fixed response time cannot be promised:**
- **Serial stalls:** a single read or write on the servo bus has been seen to stall for 100–800 ms
  (ADR-013/014). During a stall no new request is handled and no tick runs.
- **No jumps after a stall:** timed motions use stall-aware clocks, so a stall *delays* the motion; the
  next target is never a jump ahead.
- **Servo lag:** the servos follow their targets with their own lag. Fast gestures leave the arm behind
  its target, which is why a takeover continues from the last target.

## 7. Python client

`examples/api_client.py` is a standard-library client and CLI, and a test runs it against a live
server:

```python
from api_client import SO101Client

c = SO101Client("http://127.0.0.1:8765", token="so101_...")
talk = c.play(TALKING_ID, repeat="until_stopped")  # returns at once
...
watch = c.go_pose(WATCHING_ID)  # supersedes Talking immediately
assert c.command(talk["id"])["state"] == "superseded"
c.wait(watch["id"])  # optional
```

## 8. Real-arm API commissioning (NOT RUN; needs the owner's approval)

**Setup, before the first request:**
- Folded rest pose saved.
- Fresh calibration.
- Studio started with `--api --api-allow-physical-control`.
- The operator selects Simulation → Real in the window.
- The operator watches the arm, with a hand on Emergency Hold or the servo power.

**Checks to run:**
1. `GET /status`: `ready.play_behavior.ok` is true.
2. Go Pose from rest (Gentle).
3. Play a short behavior with `"repeat": 1`.
4. Loop a behavior until stopped, then supersede it with Go Pose. Watch for a smooth turn, no stop at
   rest, no jump.
5. Behavior A → behavior B.
6. Go Rest during a loop.
7. Emergency Hold from the window during an API loop: the API is then refused until the operator acts
   in the Studio.
8. Torque OFF during an API move.
9. Repeat item 4 at 2× speed.

**Record:** the event log (`--event-log`).

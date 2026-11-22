# M3C — Perception tools, independent Follow Me and live startup checks

Implemented and launched on 2026-11-11. No commits, automated paid provider calls or real-arm movements were
performed by the agent. The existing Studio controller and recordings were not changed.

## What changed

Removed the SCREEN/USER attention controller, `set_attention`, its manual keys, attention config,
attention-dependent idle rules and superseded tests. The existing M3B personality, idle/mood selection,
reaction priorities and reaction-to-idle continuation remain. All 15 recorded behaviors remain available
independently of Follow Me. Only an existing safety hook releases following before Go Rest/pose.

The architecture is now:

```
Windows screen sender / webcam tracker → existing RAM-only LAN receiver
                                               ↓
                           existing voice-companion
                         perception tools + Follow Me switch
                                               ↓
                                existing Studio robot API
```

No new server, dashboard, controller, motion logic, model or recordings were added.

## The four new LLM tools

| Tool | Result |
| --- | --- |
| `get_perception_status` | Actual receiver connectivity/authentication, screen/face availability and ages, errors |
| `get_latest_screen` | Fresh JPEG supplied as actual image content to the next normal Responses request |
| `get_face_bearing` | Face found, camera/robot-relative bearing, confidence, age, freshness and calibration/data readiness |
| `set_follow_me(enabled)` | Requests or stops the existing Follow Me pan overlay; never selects an animation |

The three perception tools remain callable when a sender or robot is unavailable, returning accurate
missing/unavailable results. The existing robot tools and builtin tools remain in the same registry.

Image input was verified against official OpenAI documentation:

- [GPT-6 Luna modalities](https://developers.openai.com/api/docs/models/gpt-6-luna): image input supported.
- [Responses function-call outputs](https://developers.openai.com/api/reference/resources/responses/methods/create):
  `function_call_output.output` supports an array of `input_text` and `input_image` parts. The image
  uses `image_url: "data:image/jpeg;base64,..."`, `detail: "auto"`.

The existing ToolRegistry now carries an optional multimodal result separately from its short text
metadata, so image payloads are not dumped into logs. The existing conversation tool loop passes that
result into the next Responses call. Mocked end-to-end tests verify exact JPEG bytes in that request.
History retains the text tool result, removing image parts before future turns so old screenshots
are not sent again by default. The tool makes no separate provider call. No screenshot scheduler
calls an LLM; the agent/user must request the tool. Existing usage metering, budget checks and session
limits remain unchanged; provider input-token usage includes image input. No paid vision test ran.

## Follow Me behavior and gates

`set_follow_me(true)` requires an active voice session, configured/checked calibration and a fresh
face bearing. It requests tracking; the result distinguishes `requested` from actually `enabled`.
The independent async worker establishes a new observation baseline, checks actual robot readiness,
then enables/forwards through Studio's existing `POST /api/v1/follow` and latest-value
`PUT /api/v1/follow/target`. The LLM does not issue 20 Hz target calls. The existing WebSocket endpoint
is also available in Studio; the measured dedicated async HTTP transport meets the target rate.

`set_follow_me(false)` invalidates pending work, stops forwarding and releases its own overlay.
It does not stop, restart or replace a behavior. All moods/idles/reactions are available with the
switch either on or off. Character guidelines suggest when to use tracking; no animation is tied to it.

Freshness limit: face 0.5 s; screen 5 s. Confidence is informational only and never gates following. Acquisition hysteresis 0.2 s,
target rate up to 20 Hz. New enable requests and receiver reconnects require a new sequence after a
baseline. Generations reject stale asynchronous completions. Face loss/staleness,
uncalibrated input, receiver loss, robot faults/Emergency Hold, Studio takeover, deliberate Go Rest/pose
and session end prevent new tracking and release the owned overlay. An already externally enabled
Follow overlay is not adopted. Failed releases stay pending and retry before any new target.

The robot API is the only movement authority. Talk Mode remains independently driven by actual speech
playback. No raw joint targets or fake 0-degree no-face targets are sent.

## Startup checklist and live updates

The existing TUI includes a compact checklist; it does not start a paid session automatically.
Actual read-only checks run before Start, refresh every second, and are included in the model's
instructions. Pressing Start repeats audio preparation and service checks. Missing LAN streams do
not prevent a normal voice session. Statuses are OK, Waiting, Unavailable and Error.

Checks show API reachability separately from arm connection, real/fake driver, fault/Emergency Hold,
and companion/API physical movement authorization. API reachability alone never means the arm is armed.
Receiver status distinguishes reachability/authentication and current screen/face ages. Calibration is
an explicit checked configuration value; the receiver does not prove camera-to-robot geometry.
Audio devices are enumerated and matched; capture/output open only when the existing Start control
is used. Credential checks report presence only. Provider connectivity says "not checked" because no
paid provider connection is made for readiness. The existing audio/AEC and provider implementations
and model/voice settings were not modified.

## Real application launch

After offline checks, the actual entry point was opened in a GNOME desktop terminal:

```bash
cd voice-companion
.venv/bin/so101-voice --no-physical-robot
```

Window title: **SO-101 Voice Companion — Readiness**. Latest launch PID is **185174**; the terminal was reopened at 16:16:00 with the final changes.
This is the real application, not the fake demo or an offscreen test. It remains open for interactive
use. The voice session was left stopped; pressing **Start** / **s** is the existing explicit action
that opens paid voice services. The agent did not press Start. The user subsequently exercised a voice session through the existing UI. Physical movement is
blocked in this companion session. Studio already had a real arm connected/torque ON before launch;
this work did not connect, energize, home or move it, or change its state.

Logged startup checklist at 16:06:53 Europe/Paris:

```
[OK] Robot API reachable
[OK] Arm connected — LeRobot SOFollower, HOLDING
[Waiting] Physical movement unauthorized in companion
[OK] Receiver reachable; authentication accepted
[OK] Gaming screen available — last observation 0.9s ago
[Waiting] Webcam face angle unavailable; waiting for gaming PC — last observation 68.3s ago
[Waiting] Face tracking not ready: camera-to-robot calibration unchecked
[OK] Audio input/output devices available; capture opens on Start
[OK] Deepgram credentials configured, OpenAI configured
[Waiting] Provider connectivity not checked (no paid requests)
```

These are time-specific observations. Earlier in this run the webcam was fresh with confidence 0.5;
by application startup it had gone stale. The live checklist updates as senders start/stop. Seeing a
fresh face packet does not establish checked calibration or sufficient tracking confidence.

## Receiver and Ethernet

The existing receiver process was reused, not duplicated. `GET /health` returned HTTP 200 with
`ok=true`, `robot_control_enabled=false`. Authenticated `/v1/status` and frame retrieval were checked.

- AI PC: `enp2s0`, **192.168.50.1/24**.
- Windows PC: **192.168.50.2/24**, neighbor resolved on the cable.
- Receiver: **http://192.168.50.1:8766**, bound only to that Ethernet address.
- Studio: **http://127.0.0.1:8765**.
- Wi-Fi default route remains via **10.192.41.1** on `wlp0s20f3`.

Real screen and face packets have now been observed at the existing receiver. They were not the
fake integration packets, which use a different temporary loopback server/port and test session IDs.
Screen packets became fresh during this run; webcam packets became stale later. Exact Windows monitor
selection and webcam calibration have not been independently supervised from this Linux session.

The shared token remains in ignored local `.env` files (receiver and companion). Never paste it into
logs or chat. Both Windows senders use the same value. No network or firewall settings were changed
in this simplification task. Existing direct-link UFW/peer restrictions remain.

If the receiver is stopped later, restart it using its existing launcher:

```bash
cd lan-receiver
./start_receiver.sh
```

Do not start a second receiver while the existing port is listening. Windows apps remain separate;
use their existing launchers, destination `http://192.168.50.1:8766`, the shared token, and selected
monitor/webcam. Face capture, screenshots and paid image reasoning remain separate operations.

Read-only standalone diagnostics (optional; the real app already displays readiness):

```bash
cd voice-companion
.venv/bin/python scripts/perception_check.py --duration 10
```

## Calibration still required

The owner subsequently confirmed angle calibration and authorized real testing. Local `[follow]`
now has `calibration_valid=true`, `allow_physical=true`; the freshness and robot safety gates remain. Camera center is 0 degrees, not 180. Only the sender's checked robot-relative `angle_deg` is
forwarded; `camera_angle_deg` is a different coordinate system. A fixed yaw offset is only approximate
when the webcam and robot have different positions.

Before separate supervised physical authorization: verify Studio's existing front reference; measure
webcam, robot and normal seated head positions; check the sender's sign/FOV/inversion/yaw offset;
compare measured robot-relative bearings at center/left/right and near/far distances without movement.
Restrict use to the checked seating envelope. If a constant offset is inadequate, keep tracking
blocked until measured geometry is implemented/reviewed. Do not invent joint offsets or change
calibration automatically. Recheck after moving the robot/webcam or changing camera settings.

Studio's Follow API has no lease/watchdog: if its API becomes unreachable, the companion can retry
release but cannot guarantee delivery; an abrupt process kill can leave the last target held. Existing
independent Studio safety controls and supervised real-arm validation remain necessary.

## Validation and changed files

Full companion regressions, all fake-arm integrations, receiver tests, lint and strict typing were run.
Final full run: **177 passed, 1 skipped, 5 deselected** (38.91 s). The skipped PipeWire smoke
test refuses a second echo canceller while the real companion is running; it passed before launch.
**4 fake-arm integrations** and **8 receiver tests** passed; lint and strict typing passed. Main test commands:

```bash
cd voice-companion
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
SO101_REPO=../robot-controller \
  .venv/bin/python -m pytest -q -s -m robot_fake
cd ../lan-receiver
.venv/bin/python -m pytest -q tests
```

The fake integrations use the real Studio API with explicit fake-hardware/fake-arm flags and temporary
credentials. Real recordings and Studio source are unchanged. The loopback transport measurement in
`M3C_MEASUREMENTS.json` is **19.87 Hz**, median ingest-to-API **38.49 ms**, max **51.61 ms**; it is not a
real Ethernet or physical servo latency claim.

Core files: `perception/lan.py`, `perception/follow.py`, `robot/follow.py`,
`tools/perception_tools.py`, `tools/registry.py`, `readiness.py`, `app.py`, `ui/tui.py`.
The existing conversation tool-output path and history support image parts; STT, TTS, AEC, Talk Mode,
model settings, costs/session limits, and M3B behavior selection/continuation remain intact.
Config now uses `[perception]` and `[follow]`; obsolete `[attention]` config is removed. Tests are in
`tests/test_perception_tools.py` and the existing fake-arm integration files. Nothing was committed.


Final live follow-up: the screen sequence advanced from 188 to 189 over a 2.1-second sample and a
fresh **117,815-byte JPEG** was retrieved. Face sequence advanced from 678 to 710; the latest face
age was approximately **0.018 s**, confidence **0.5**. Both Windows streams were therefore genuinely
arriving at this check. Calibration remains unchecked and confidence below the default 0.7 threshold;
physical following was not enabled. The checklist changes dynamically rather than retaining its
startup Waiting result.


Latest verified application startup at 16:16:00: robot API and real arm connected; physical movement
unauthorized; receiver authentication accepted; screen fresh (0.2 s); face fresh (0.0 s); calibration
unchecked; audio devices and credentials available. PID 185174 remained running after verification.
The launch flag also deliberately refuses recorded arm behaviors in this session, explaining the
logged `play_robot_behavior` refusal during interactive voice use. Physical behavior testing requires
separate owner authorization; no movement was enabled to resolve that refusal.
A final concurrent-readiness/target freshness regression also passed (20 focused perception tests).


Owner-authorized follow-up at 16:23:38: real application reopened as PID 191550 with physical behavior
and Follow Me permissions enabled, and camera-to-robot calibration marked checked on the owner's
confirmation. Both screen and webcam observations were fresh at startup. Follow Me still requires
an explicit tool request, a live session, fresh face bearings and the existing API safety checks.

The monochrome terminal was caused by inherited `NO_COLOR=1`. `./start_companion.sh` now launches the
same entry point with that variable removed. The real process was verified with `TERM=xterm-256color`,
`COLORTERM=truecolor` and no NO_COLOR. Readiness status labels are colored. IDLE now says mic off;
provider text explains Start is required, then updates from actual STT/TTS state and verified OpenAI
responses without adding startup provider probes. Focused tests: 33 passed; lint and strict typing passed.


Owner-requested simplification: confidence no longer controls Follow Me. The threshold and its
configuration were removed. A fresh, valid received face angle is used regardless of the confidence
score; confidence remains visible only as observation metadata. Face loss, expiry, explicit on/off,
calibration and the existing robot safety/ownership checks remain unchanged.

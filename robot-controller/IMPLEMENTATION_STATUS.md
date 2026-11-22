# Implementation status

# Milestone 2.6: Follow Me + Talk Mode (2026-11-07, ADR-019)

**Software: implemented and tested offline (fake hardware plus MuJoCo). Real arm: two owner-supervised
Studio sessions on 2026-11-07 (partial; see below). Follow Me / Talk over the HTTP API or WebSocket on the
real arm: NOT RUN.**
- **Design:** the owner approved review `planning/FOLLOW_ME_TALK_DESIGN_REVIEW.md` with decisions D1–D10.
  The as-built design is ADR-019.
- **Overlays:** two independent, persistent overlays merged in the single controller's tick. Follow Me
  owns `shoulder_pan` and Talk Mode owns `gripper`; behaviors and poses keep the other joints. There is
  no second loop or driver.
- **Geometry** (verified with FK on the official model):
  - angles are measured about the shoulder-pan axis (38.8 mm in front of the base origin), clockwise =
    +pan;
  - each behavior, pose and the rest pose gets one cached representative direction (NORMAL / REVERSED /
    MIXED / UNDEFINED);
  - one reachable pan solution exists, otherwise LIMITED / UNREACHABLE with the residual (`AT_LIMIT`,
    never reported as tracking);
  - the front reference is set in the Studio and stored per calibration and model.
- **Semantics:**
  - **Kept running:** overlays survive behavior replacement, Go Pose and natural endings.
  - **Go Rest:** with Follow Me on, pan keeps following and rest arrival ignores pan. With Follow Me off,
    the full six-joint rest is unchanged. Go Rest cancels Talk.
  - **Pause** freezes everything. Ambiguous behaviors **suspend** tracking.
  - **Overrides:** Emergency Hold, Torque OFF, faults and mode changes stop both overlays.
  - **Holding:** with an overlay engaged the merged target is sent; without one, nothing new is sent (as
    before).
- **Talk Mode:** seeded min-jerk procedural gripper animation, gentle / normal / excited, all within the
  verified ADR-015 gripper limits and ≤ ~1.5 Hz, with a renewable lease.
- **API:**
  - `POST /follow`, `PUT /follow/target` (latest value, no ledger growth), `GET /follow/status`;
  - the authenticated WebSocket `/follow/targets` (D7: the VR scope test narrowed to this one route);
  - `POST /talk/start`, `/talk/stop`, `GET /talk/status`.
- **Studio:** a compact Follow Me / Talk card; pan and gripper sliders yield to the overlays; Advanced →
  "Follow Me & Talk Mode" (front reference, details). Simulation Only composes onto the existing 3D
  playback.
- **Evidence:** `docs/evidence/follow-me/` (5 sheets + `summary.json`, from `scripts/follow_me_evidence.py`).
  FK-measured facing equals the target in every reachable case.
- **Your data:** your recordings, rest pose and saved pose are REVERSED. With the front at your working
  direction (≈ 105° clockwise of the pan center) they can follow −30°…+180°; the software reports this
  range.
- **Real-arm Studio sessions** (owner-operated GUI, 2026-11-07; logs in the gitignored
  `commissioning-reports/studio-2026-11-07/events-7.jsonl` and `events-8.jsonl`):
  - **Session 7:**
    - Follow Me tracked target changes within about 0.5° after the first turn. The first turn stalled about
      40° short, probably an obstruction.
    - Talk normal / excited: the gripper was commanded 3.3–25 and measured 3.7–24.
    - Two defects found (fixed below).
    - Go Rest reported `rest_not_reached`: the shoulder sagged about 9° from the raised rest pose, and
      serial stalls of 656–815 ms were seen.
  - **Fix 1, unreachable target:** an unreachable target swung pan about 178° to the far bound. The bound
    choice now prefers the bound nearer the current pan on a near tie.
  - **Fix 2, absolute targets:** a target was relative to a stale rest REVERSED reference after the arm was
    reshaped with sliders. Slider moves now re-measure the owner direction once the arm settles (0.3 s),
    so the target is absolute.
  - **Session 8** verified both fixes:
    - unreachable 0° at rest → pan −104.9° (a 32° turn), reported `AT_LIMIT`;
    - after reshaping with sliders the reference stayed REVERSED until the gripper crossed the axis, then
      NORMAL; for target 0° the pan went to −0.3° with FK facing −0.4°.
    - Shoulder sag at rest and serial stalls of up to about 0.8 s were still seen.
  - **Session 2026-11-08** (voice companion over the API, real arm):
    - Talk start / renew / stop from the voice app worked.
    - Two `READ_FAILED` faults happened while the arm was holding idle. The servos kept power and torque
      through both: the pose was unchanged at recovery.
    - **Cause:** the `--screenshot` commissioning aid called `window.grab().save()` every second.
      `QPixmap.save` keeps the GIL for the whole PNG encode (about 130 ms), and both faults fell inside
      one of those stalls. The Feetech SDK's receive loop gives up the GIL during an empty read; when it
      got it back after its 50 ms wall-clock timeout, it reported "no status packet" although the reply
      was waiting in the buffer.
    - **Fix:** `WindowShooter` grabs on the GUI thread and saves a `QImage` (which releases the GIL) on
      a background thread. Measured on the real Studio window, the block on other threads dropped from
      about 120 ms to about 15 ms. Regression test: `tests/gui/test_screenshot_gil.py`.
    - Plain `so101-companion` (no `--screenshot`) was never affected.
  - **Not tested on the real arm:** gripper B06, Talk gentle, Pause with overlays, the Simulation-Only
    preview, and Emergency Hold / Torque OFF while an overlay is active.
- **Tests:**
  - **473 default passed:** 61 new (46 Follow Me / Talk, including the two real-arm regressions, + 1 GUI
    + scope and allowlist updates).
  - **9 render passed.**
  - **ruff and mypy:** clean.
  - **Offscreen Studio smoke** with `--api --fake-hardware`: passed.

# Milestone 2.5: Reactive HTTP API (2026-11-07, ADR-018)

**Software: implemented and tested offline (fake hardware). Real-arm API commissioning: NOT RUN** (it
needs the owner's separate approval; the checklist is in `docs/HTTP_API.md` §8).
- **What exists:**
  - an authenticated FastAPI service `/api/v1` (`src/so101_companion/api/`): status and readiness,
    behavior and pose listings, play (with per-run overrides), go to pose, go rest, pause/resume,
    Emergency Hold, command progress, and on-demand JPEG/MJPEG simulation video;
  - OpenAPI docs at `/docs`; a stdlib Python client in `examples/api_client.py`.
- **One controller:**
  - the API submits the Studio's request objects to the same `ControllerCoordinator` queue, and the
    controller thread stays the only bus owner;
  - a command ledger (`control/commands.py`) tracks Studio and API commands alike.
- **Reactive preemption** (shared with the Studio):
  - **Takeover:** Play, Go Pose and Go Rest supersede any running motion (a behavior including its loops,
    pending Finish or ending, a pose move, or a rest return). No return to rest is inserted in between.
  - **Start pose:** always a fresh read first. A still arm starts from the measured pose; an arm in
    flight continues from the last commanded target (D9, no jump).
  - **Studio:** in Simulation → Real another behavior can now be selected and played while one runs.
- **Safety:**
  - **Tokens:** Bearer tokens, stored as hashes outside the repository; 401 records and executes nothing.
  - **Binding:** loopback by default; a remote host needs `allow_remote`.
  - **Physical gate:** `allow_physical_control` is off by default and enforced by the controller itself.
  - **Emergency latch:** any Emergency Hold refuses API motion until the operator acts in the Studio.
  - **Torque:** the API never energizes the motors.
  - **Headless mode** runs with fake hardware only; starting the service never connects or arms.
- **Measured with fake hardware:**
  - dispatch ≈ 1 ms;
  - first target ≈ 16 ms (≤ one 20 Hz tick);
  - video ≤ 5 FPS, only while a client watches;
  - the control loop is unaffected while streaming.
  - Serial-stall limits are documented; no physical response time is promised.
- **Dependencies:** a new `api` extra (fastapi, uvicorn, pillow), included in `dev`. Both lock files
  were regenerated; the only change is the additions.
- **Tests:** 412 default tests pass (39 new for the API, plus 1 GUI), plus 9 render tests (including a
  real-EGL video test); ruff and mypy clean. The offscreen Studio smoke with `--api --fake-hardware`
  passed.

# Named poses and per-behavior endings (2026-11-07, ADR-017)

**Software: implemented and tested offline (fake hardware).** **Real arm, 2026-11-07, owner-driven: PARTIAL PASS.**
- **Worked on the arm:**
  - Save current, both from the 3D arm and from the real arm (fingerprinted);
  - Go to pose: folded to upright in 3.6 s (Normal), then holding;
  - "hello" ×3 with After behavior → Move to saved pose: a smooth 1.4 s move to the pose, then hold, and
    no return to rest.
- **Also worked on the arm:**
  - **Finish after this loop**, on "hello" until stopped: loop 5 completed, then a 1.4 s move to its
    ending pose and a hold;
  - **Go while playing**: the gesture stopped mid-loop and the arm moved smoothly (1.65 s) to the pose.
- **Fixed during this session:** the Poses dropdown now lists the saved poses at startup.
- **Not yet tried on the arm:** Hold last position, and Emergency Hold during an ending move. Both are
  covered by offline tests.
- **What exists:** a pose library (save, rename, delete, preview, Go), the "After behavior" setting
  saved in each behavior file, and a new motion state, `MOVING_TO_POSE`.
- **Tests:** 373 default tests pass, plus 8 render tests.

# SO-101 Studio UI (2026-11-07, ADR-016)

**Software: implemented and tested offline.** **Real arm, 2026-11-07, owner-driven: PASS.**
- **Mode dropdown:** both workflows ran. A failed first connect (motor 4 did not answer) left the app
  safely in Simulation Only.
- **Playback:** Play, Pause/Resume (held in place, then a smooth 0.4 s rejoin), Go Rest (also from a
  still arm, with confirmation) and Save Rest all worked.
- **Large entry (B11 step 5):** 3.2 s, with every step inside its limit.
- **Long run:** 25 loops (74 s) with no fault.
- **Open items:**
  - an intermittent servo power / motor 4 bus fault (13:19 and 13:53);
  - the owner saved a raised side rest pose, so expect up to about 9° of shoulder sag and
    "Rest not reached". A folded rest pose is recommended.
- **Layout:** one window: large 3D view, Studio panel, collapsed Advanced section, status bar.
- **Function map:** `planning/UI_REDESIGN.md`.
- **Controller additions:**
  - the `EnterMode` workflow;
  - physical Pause/Resume;
  - Go Rest from a hold and in simulation;
  - transition presets.
- **Tests:** 343 default tests pass, plus 8 render tests; ruff and mypy are clean. Offscreen smoke with
  fake hardware: passed.

# Milestone 2 — Behavior Engine (2026-11-07)

**Gate A (software, offline): PASSED.**
- Default suite: **324 passed**, 10 deselected (310 at Gate A, 3 Gate B fixes, 10 for D8 responsive transitions incl. MuJoCo physics).
- **D8 responsive transitions (ADR-015):** B11 PARTIAL on the real arm. 0.6–2.0 s entries, 0.4–1.3 s bridges, 1× and 2× with up to 5 loops; no faults.
- **D9 (B11 finding):** a behavior hands over to rest from the last target sent, so the arm no longer brakes hard.
- `-m render` (EGL): **8 passed**.
- `ruff check` / `ruff format --check` / `mypy --strict`: clean.
- Offscreen app smoke (fake hardware): passed; the panel renders in the single window.

**Gate B (supervised physical, M2-B01…B10): PARTIAL, first session 2026-11-07.**
- **PASS:** B01–B05 and B07 (one hand-guided take, two real runs; details in the acceptance matrix).
- **NOT RUN yet:** B06 (gripper and wrist), B08 (Stop mid-run), B09 (Emergency Hold / Torque OFF mid-run), B10, and 0.5× (both runs were at 1×).
- **Software findings fixed (313 tests pass):**
  1. `behavior_started` now records the preview that authorized the run.
  2. A disabled Preview or PLAY now says why. Preview is locked while movement is enabled.
  3. PLAY is blocked as soon as a preview starts.
  4. **D7:** the preview is optional (owner decision, `DECISIONS.md` Round 6).
- **Session notes:**
  - On connect, the arm was folded with the base at +102° and the wrist bent back, far from the saved rest pose. The calibration checked out (the 3D view matched the arm).
  - The legacy rest pose was adopted, with a backup.
  - The first return to rest (from the side position) was smooth; the elbow missed by 3.6° (`REST NOT REACHED`, known). Both behavior runs then reached rest.

**Specs and decisions:**
- Specs: `planning/so101-milestone2-handoff/` (owner resolutions D1–D6 in `DECISIONS.md`, Round 5).
- Review: `planning/M2_INTEGRATION_REVIEW.md`.
- ADRs: `decisions/ADR-013` (integration) and `ADR-014` (M1 changes: stall-aware rest, rest-pose fingerprint).
- Evidence per test: `planning/so101-milestone2-handoff/milestone-2/tests/acceptance-matrix.md`.

## What exists (M2)
- **Behavior core:** `src/so101_companion/behaviors/` (pure: no Qt, serial or MuJoCo).
  - `model.py`: schema v1 files with a stable UUID, immutable samples, settings, and an edit log with revisions.
  - `validate.py`: the 250 ms gap rule (250 inclusive), monotonic time, six finite joints, provenance, and the >180° seam/wrap check.
  - `repository.py`: `behaviors/<uuid>.json`, atomic writes, stale-revision refusal; gitignored (`/behaviors/`).
  - `recorder.py`: hand-guided (measured frames only) and simulation-slider takes.
  - `plan.py`: trim, an audited clamp to the calibrated bounds, speed 0.5–2×, linear content, minimum-jerk entry and loop bridges (**D8 / ADR-015:** per-joint speed and acceleration limits, 0.15 s minimum, seamless loops without a bridge; was 30°/s), finite or "until stopped" repeats, and content and preview hashes.
  - `preview.py`: an exact MuJoCo preview (entry, content, bridges, planned rest) and a single-use `PreviewApproval`, issued only with hardware context.
  - `service.py`: UI-independent operations, with no raw-joint path.
- **Controller** (`control/coordinator.py`):
  - `PLAYING_BEHAVIOR` is a MOTION state. It uses the M1 clamp, send, faults and divergence diagnostics unchanged.
  - New requests: `StartRecording`, `StopRecording`, `PlayBehavior`, `StopBehavior`, `AdoptRestPose`.
  - `control/timeline.py`: the stall-aware clock used by playback *and* the return to rest.
- **UI** (`ui/behavior_panel.py`, a collapsible "Behaviors (M2)" box in the existing window):
  - library, rename and delete;
  - hand-guided and slider recording with a live REC/gap display; invalid takes are reported and never saved;
  - a keyframe table (simulation behaviors); trim, speed and repeats; scrub;
  - preview with the clamp report and a note about the model-range display limits;
  - per-run confirmed Play, which states when the motors will be energized; Stop behavior → rest; a live segment / repetition / stall-delay line.
- **Rest pose (ADR-014):** an "Adopt rest pose…" button and a status line.

## Important for the next hardware session (operator action needed)
- **Your saved rest pose is not used until you act.** `config/rest_pose.left_follower.json` has no calibration fingerprint (it predates ADR-014 and was hand-shifted for the wrist re-centring).
  - On connect, the app shows "Rest pose NOT usable … Adopt rest pose…". Normal Stop and Play stay unavailable until then.
  - Either adopt the file after checking the displayed pose (it is backed up first), or record a new rest pose.
- **Expect `Rest not reached` after behaviors.** The elbow's M1 rest error was 3.4° against ±3°. It is reported, the arm holds, and nothing retries.
- **Playback can run longer than nominal.** Serial stalls (up to ~350 ms observed) delay the timeline (`timeline_delay_ms` in the panel and events). It never skips or catches up.
- **Open risks (not resolved by M2):**
  - the ADR-009 wrist runaway (not reproduced, plausibly explained by the old seam position, ADR-012);
  - the 5 V supply (gripper input-voltage error once);
  - stale goal registers after power-up (R15).
- **Playback is supervised only**, never unattended. Have the servo-power cut-off within reach; USB removal is not an E-stop.

## Deviations from the M2 package text (documented)
- **Phase order.** Controller integration (package P5) was done before the UI (package P4), as approved in the review.
- **Preview optional (D7, Gate B).** The package required a preview before every run; the owner removed that requirement on 2026-11-07. A preview is used to authorize a run only when connected in REAL_TO_SIM or SIM_TO_REAL (a fresh measured pose) with a valid rest pose. A SIM_ONLY or disconnected preview is labelled PROVISIONAL.
- **Gap rule scope.** The 250 ms rule applies to timed takes (hand-guided, slider). Authored keyframes are only checked for ordering and validity, since they are sparse by design.
- **Validation without `jsonschema`.** The runtime-dependency test forbids it. Validation is hand-written; the schema file is checked structurally in tests.

---

# Milestone 1

**Date:** 2026-11-07
**Gate A (software, offline): PASSED** (re-verified after the policy change, rest feature and wrist range: 250 default tests, 8 render).
**Gate B (physical hardware): MOSTLY VERIFIED — wrapped up 2026-11-07 at the owner's request** (incl. wrist re-centring, ADR-012). Verified on
the real arm: connect, mirror (all six joints, mapping fixed), direct control, bounds clamp, Emergency
Hold, Torque Off, manual recovery, record rest pose, and Normal Stop return + hold. **Not run:** the
deliberate USB-unplug fault test (B06) and the full limit/gripper scripts (c04/c05). **Open:** the
rest-arrival tolerance on elbow_flex (3.4° vs ±3°, see below).
**Control policy:** **FINAL fixed M1 policy (ADR-010, 2026-11-07)** — calibrated bounds −5° margin,
targets sent directly via LeRobot, no step limiter, `max_relative_target=None`, no configurable safety
options. Communication faults stay latched; Emergency Hold and Torque Off are manual.

## Hardware commissioning results (2026-11-07) — OBSERVED ON THE REAL ARM

Setup: `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B14030170-if00`, calibration
`left_follower.json` (from SO-101-VR-Control, unchanged), LeRobot 0.4.4, 5 V supply
(Present_Voltage 5.1–5.2 V), Ubuntu 24.04 Wayland, Intel Mesa.

| Stage / test | Result | Evidence |
|---|---|---|
| B01 connect + read | **PASS** | Connected through LeRobot; the motor calibration registers match the file; all six positions read at **20.0 Hz** (median cycle 1.6 ms), noise 0.000° over 121 reads; 0 commands. One connect attempt failed during configure() with **"Input voltage error" on the gripper (id 6)**: power-supply sag (5 V supply, undervoltage limit 4.0 V). The retry worked. |
| B02 real → MuJoCo mirror | **PASS** after a mapping fix (wrist later re-centred, ADR-012: display offset now −2.7°) | All six joints follow in the same direction (all signs +1, gripper low = closed → `model_min` confirmed); **wrist_roll needed a −95° display offset** (saved in `config/local.toml`). Its encoder seam (±180°) lies about 80° from the working pose (calibration kept, owner decision). |
| B03 first commanded motion (shoulder_pan +5°) | **PASS** | requested = staged = sent; measured −99.12 → −94.46° in 0.35 s; other joints 0.00°. |
| Enable movement at rest (bounds ratchet) | **PASS** (2nd attempt) | Three joints rest inside the 5° margin: max deviation 0.09° over 62 armed ticks. |
| **Wrist runaway incident** | **Observed, not reproduced** | On the 1st Enable, wrist_roll spun about 337° away from goal == present after hand rotations through its seam; stopped manually with Emergency Hold. Registers healthy. ADR-009. |
| Operator slider session (base full range, shoulder sweeps) | **PASS**, plus a false-trip fix | Base clamped at ±104.9° (calibrated −5°); measured ±104.7°. The old step limiter and 20° cap behaved as designed (since removed by ADR-010). The first divergence-guard version tripped falsely on a fast shoulder reversal; the detector was fixed and validated on the recorded trace. Recovery flow worked (read-only, torque UNKNOWN). |
| Wrist after re-centring (ADR-012) | **PASS** | Mirror continuous over −165…+167.5° (no seam wrap); commands clamped at **±163°** (measured −162.7°); tracking error median 0.36°; fast slider moves ran at full servo speed (~220°/s, fixed policy); ratchet kept the wrist at +167° on Enable until moved inward; no fault/overload/divergence warning. |
| Real control rate | 20.0 Hz read-only; ~16 Hz average while armed (median 20 Hz, occasional bus stalls) | event logs |
| B08 record rest pose + Normal Stop (new policy) | **PASS with one tolerance miss** | Recorded the measured pose (shoulder_lift/elbow_flex/wrist_flex clamped to the bounds as decided). Two STOP returns ran as smooth 30°/s trajectories (2.4 s, 3.1 s) and then HELD. Both reported **`rest_not_reached`**: elbow_flex settled **3.4°** from target (tolerance ±3°); shoulder_lift 2.7°, wrist_flex 1.0°, pan 0.4°. The arm held safely where it was, as specified. Cause: gravity load on the elbow with LeRobot's low P-gain (16) at 5 V. Owner judged the behaviour good. |
| B07 Emergency Hold / Torque Off | **PASS (observed)** | Emergency Hold stopped the wrist runaway and held. Torque Off was used repeatedly for hand-guided mirroring (arm limp, mirror continued). Enable torque was not exercised separately (sessions reconnected instead). |
| Manual fault recovery | **PASS (observed)** | After the (false) divergence fault: Recover → one read → CONNECTED_READ_ONLY with torque UNKNOWN; nothing resumed. |
| B09 control rate, armed (new policy) | **Measured** | Median tick spacing **50 ms (20 Hz)**, median cycle 2.5 ms; occasional bus stalls up to ~350 ms (max cycle 259 ms) → **~16 Hz average**. Reported honestly; not a guaranteed 20 Hz. |
| B04 limit scripts, B05 gripper script, B06 USB-unplug fault | **NOT RUN** | The base was observed clamping at its ±104.9° bound in the GUI session; gripper mapping verified visually in B02. The USB-unplug latch is covered offline only. |

### Software fixes made because of hardware findings (all with tests)
1. **Torque reported UNKNOWN after a connect that fails inside configure().** Only when the port was
   actually opened; import or argument errors leave the torque state unchanged.
2. **Live "3D display mapping" panel** (offset/reverse per joint, gripper direction, Save to config),
   affecting the display only.
3. **Full-turn joints (wrist_roll) shown at their true angle**, wrapping ±360°, even inside the
   official model's 40° range gap. This fixed a visual "lap".
4. **Tracking-divergence detector** (`safety/divergence.py`), validated on two real recorded traces.
   Automatic torque-off was later removed by ADR-010; it is now a **diagnostic** warning only.
5. **`--event-log` (live JSONL telemetry) and `--screenshot` (periodic window PNG)** options for
   supervised commissioning.
6. **Fixed control policy (ADR-010):** step limiter removed, `max_relative_target=None`,
   central `safety/policy.py`, `[safety]` configuration rejected.
8. **wrist_roll re-centred and range fixed (ADR-012, motor configuration change, owner-approved).**
   - The measured mechanical dead zone (−99.2…−76.1°) was moved onto the encoder seam: motor 5
     Homing_Offset −669 → 381, file updated, backup kept.
   - The usable arc is now continuous (−168.4…+168.5°); allowed range **±163°**
     (`policy.JOINT_RANGE_LIMITS`, applied to hardware and slider bounds).
   - Display offset −95° → −2.7°; rest pose shifted accordingly.
   - **Wrist servo overload afterwards** ("missing motor 5"; the servo was replying `Overload error`).
     Cleared by power-cycling the servo supply; offset persisted. Likely cause: the earlier −75°
     limit without margin (1.1° from the stop). Details in ADR-012.
7. **Rest pose + Normal Stop (ADR-011, T-039):**
   - *Record rest pose…* (measured, clamped to bounds, stored per calibration id).
   - *STOP*: a 30°/s synchronized trajectory, arrival ±3°/±5 within duration + 3 s, then HOLD
     with torque on.
   - Emergency Hold and Torque Off stay separate.
   - Offline-tested (248 default + 8 render tests pass); **first real execution pending owner review.**

| Phase | Status | Evidence |
|---|---|---|
| P0 dependency/model/UI feasibility | DONE | Wayland desktop smoke egl+glfw (`docs/evidence/p0/`), ADR-005 |
| P1 contracts, mapping, fake robot | DONE | `domain/joints.py`, `simulation/mapping.py`, `devices/fake.py`; `tests/unit/test_joints.py`, `test_mapping.py`, `test_calibration.py` |
| P2 limits, controller loop, state machine | DONE | `safety/`, `control/coordinator.py`, `control/worker.py`; `tests/integration/test_controller_*.py`, `test_worker.py` |
| P3 integrated GUI | DONE | `ui/main_window.py`, `ui/dialogs.py`; `tests/gui/test_main_window.py`; offscreen screenshots `docs/evidence/m1-final/fake-hardware-ui/` |
| P4 LeRobot hardware adapter | DONE (offline) | `devices/lerobot_so101.py`, `devices/calibration.py`; `tests/driver_contract/` runs the **real LeRobot 0.4.4 `SOFollower` code** with a fake bus; ADR-006 |
| P5 offline acceptance (Gate A) | **PASSED** | results below; acceptance matrix A01–A18 |
| P6 hardware runbooks/commissioning | IN PROGRESS (supervised, see above) | `scripts/commissioning/c01…c08`, `COMMISSIONING_GUIDE.md`, ADR-008 |

## Offline verification results (final code, 2026-11-06)

```text
.venv/bin/ruff check src tests scripts              All checks passed!
.venv/bin/ruff format --check src tests scripts     68 files already formatted
.venv/bin/mypy   (strict)                           Success: no issues found in 35 source files
.venv/bin/python -m pytest  (DISPLAY/WAYLAND unset)  230 passed, 10 deselected   [hardware venv, LeRobot 0.4.4 installed]
   unit 144 · integration 57 · gui 15 (offscreen Qt) · driver_contract 14 (real LeRobot code, fake bus)
dev-only venv (no LeRobot) -m pytest                216 passed, 14 skipped (driver contract), 10 deselected
.venv/bin/python -m pytest -m render                8 passed   (real EGL renderer, full app offscreen, gripper 0≠100)
offscreen app smoke (--smoke-seconds 5)             passed: 30.6 fps render, controller 20.01 Hz, 0 skipped deadlines,
                                                    1 top-level window, SIM_ONLY/DISCONNECTED, 0 device accesses
                                                    (docs/evidence/m1-final/offscreen-sim-only/metrics.json)
-m desktop (visible windows)                        2 passed in P0; not re-run at your request (no windows)
```

Every test session runs under a device guard: any serial-device open, `/dev` enumeration or
(outside the contract tests) driver import fails the run. Violations recorded: 0.

## What exists

```text
src/so101_companion/
  app.py                       CLI; always SIM_ONLY; --config, --fake-hardware, --gl-backend, --smoke-seconds
  config.py + config/default.toml   validated TOML config (20°/100 caps, 10-unit steps, 5° margin, mapping)
  domain/joints.py, state.py   six-joint JointFrame (finite, exact keys, units), modes/connection/torque, Snapshot
  safety/bounds.py             ranges + 5° margin (arm only), gripper 0..100, no-outward-ratchet (ADR-007 §1)
  safety/step_limit.py         VR-Control 10-unit-per-issued-tick limiter (MIT attribution)
  control/coordinator.py       single owner of state and bus calls; safety ordering; fault latch; hold/torque/recover
  control/worker.py            20 Hz monotonic thread, coalesced intents, skipped deadlines (no catch-up)
  control/requests.py          operator requests (confirmations carried explicitly)
  devices/calibration.py       read-only validation of an EXISTING LeRobot calibration; calibrated degree bounds
  devices/lerobot_so101.py     LeRobot 0.4.4 SOFollower adapter (lazy import, version allow-list, register check)
  devices/fake.py              fault-injecting FakeRobot + demo calibration (tests / --fake-hardware only)
  devices/protocol.py          RobotPort contract + typed HardwareErrors
  simulation/                  pinned model verification, MuJoCo pose preview, logical<->MJCF mapping, renderer
  telemetry/events.py          structured ring log, redacted JSONL export
  ui/main_window.py, viewport.py, dialogs.py   ONE window: 3D, 6 sliders, mode/hardware/hold, telemetry, log
  diagnostics/device_guard.py  blocks serial access until a confirmed Connect (dev safety net, not security)
scripts/fetch_model_assets.py  pinned official model fetch + sha256 check
scripts/commissioning/c01…c08  operator-run Gate B procedures (+ _common.py, _motion.py)
tests/{unit,integration,gui,driver_contract,smoke,desktop}/
requirements/dev-…lock, hardware-…lock          hashed locks (CPython 3.12, manylinux_2_39, CPU torch)
decisions/ADR-005…008          implementation decisions, alternatives, consequences
COMMISSIONING_GUIDE.md, THIRD_PARTY_NOTICES.md, SOURCE_LOCK.md
docs/evidence/p0, docs/evidence/m1-final       screenshots + metrics (all offscreen except P0)
```

## Decisions — how they are realised (as of ADR-010)

* **Calibrated limits with a 5° margin** on the five arm joints, from the existing LeRobot calibration
  file, in LeRobot's own degree convention. The formula is proven equal to VR-Control's. The gripper
  range is 0..100 with no margin.
* **Targets are sent directly** (ADR-010): no step limiter, LeRobot `max_relative_target=None`, nothing
  configurable. SENT is LeRobot's returned value. *(The 10-units/tick limiter and the 20°/100 cap were
  implemented and hardware-observed on 2026-11-07, then removed by owner decision.)*
* **Tracking divergence** is a diagnostic warning only (ADR-009/010).
* **Gripper protections:** LeRobot's existing limits stay untouched (500/250/25, written by `configure()`).
* **No ground protection, IK, VR, behaviours or AI.** `test_scope_exclusions.py` enforces this.
* **Hardware authority:** connecting needs explicit confirmation. Arming needs SIM_TO_REAL, a valid
  calibration, known torque ON and a separate confirmation. There is never automatic calibration,
  automatic mode selection, automatic connection or automatic port choice.
* **Faults:** any communication failure latches FAULT. After that there are no more bus calls and no
  reconnection or resume. Actual readings are marked STALE and torque UNKNOWN. Recovery is manual and
  needs confirmation.
* **Stops:** Emergency Hold (best effort) and Torque Off (confirmed) are separate actions. Neither is
  presented as an emergency stop. **Stop-to-Rest / rest pose: not implemented yet** (T-039; next step,
  to be reviewed with the owner before the first real execution).

## Deviations from the spec text (all documented, none weakens a decision)

* The `RobotPort` contract is extended with `verify_calibration`, `enable_torque_holding_present` and
  `describe`. The spec's `initialize(calibration_path, port)` signature is kept.
* Module names differ from the proposal: `telemetry/events.py` (not `logging/`, which would shadow the
  stdlib) and `safety/bounds.py`. The proposed `ui/controls.py` is merged into `ui/main_window.py`.
* Event contract: per-tick `sent`/`actual` live in `control_tick` records rather than separate
  `command_sent`/`observation_received` events, to avoid tripling the log volume. Hold, connect and
  enable events carry their own poses. All other listed events, including `render_fault`, are emitted.
* `--mode` accepts only `sim-only`, because hardware modes are chosen in the window after connecting.
  `mode_on_start` and `hardware_autoconnect/autoenable` are accepted only with their safe values.
* Additional defensive checks (ADR-007): the no-outward-ratchet clamp, the calibration-register check,
  goal := present before enabling torque, torque UNKNOWN gating, and the version allow-list.

## Hardware-dependent items still UNVERIFIED (need you and the arm)

1. LeRobot `connect(calibrate=False)` + `configure()` behaviour on this arm, including the brief
   torque off/on and any motion while it happens (B01).
2. That your calibration file matches the motors' registers, and what the real calibrated ranges are (B01, B04).
3. Joint **signs** and any zero **offset** between your calibration and the official model, for all
   five arm joints (B02).
4. **Gripper direction** (0 = closed?), and the matching `logical_gripper_0_maps_to` (B02, B05/c04).
5. Real motion through the pipeline: arrival, step limit, LeRobot 20° cap on hardware (B03, c05 cap test).
6. The arm stopping before mechanical stops at the 5°-margined limits (B04).
7. Achieved controller Hz, bus latency and render rate with real serial traffic (B09/c06).
8. Emergency Hold effectiveness; Torque Off (arm falls, supported); Enable torque holds without
   jumping (B07/c08).
9. USB unplug → FAULT latch, no auto-recovery; recovery by Disconnect + Connect (B06/c08).
10. Rest pose: recorded only (c07). **Normal Stop-to-Rest is not implemented** (T-039; needs your approval).
11. Torque state after disconnect, quit and interpreter exit (LeRobot destructor, R14).

## Known limitations and source-compatibility risks

* **LeRobot pinned to 0.4.4.** Newer versions are refused until the driver-contract suite passes
  against them and the allow-list is extended. 0.7.0 needs `numpy<2.3`, which conflicts with the
  current lock.
* **torch (CPU) is required by LeRobot's imports.** The hardware install is about 1 GB, and the first
  Connect takes about 7 s while LeRobot is imported (on the controller thread; the UI stays responsive).
* **Device guard is not a security boundary.** It is a Python-level safety net that native code can bypass.
* **LeRobot's destructor may still disconnect on process exit** and disable torque. The UI and scripts
  always ask for an explicit disconnect first (R14).
* **Recovery cannot revive an unplugged port.** After a USB unplug the LeRobot port handle is dead; the
  documented path is Disconnect + Connect.
* **Emergency Hold depends on the same thread as everything else.** It waits behind any in-flight serial
  call on the single controller thread (at most one bus transaction).
* **The 3D view is a kinematic pose preview,** not a physics or actuator simulation. Out-of-range
  logical values are clamped for display and flagged.
* **Mapping is provisional.** The arm sign/offset and gripper direction defaults are documented choices
  (ADR-006), not measured ones.
* **Rendering runs on the GUI thread** (about 12–21 ms per frame at 640×480). This is fine at 30 fps on
  this Intel GPU but untested on other GPUs. OSMesa isn't available on this host.
* **The desktop (visible-window) test was not re-run for the final build,** at your request. The final
  build is verified offscreen with the real EGL renderer.
* **`MANIFEST.json` was regenerated** for the final package (it lists checksums of all tracked documents
  and sources).

## Open decisions for later (not blocking Gate A)

* T-039: rest-pose motion semantics (trajectory, speed, arrival tolerance, timeout, holding torque),
  after B08 recording.
* Your config values for joint signs/offsets and gripper direction after B02/B05.

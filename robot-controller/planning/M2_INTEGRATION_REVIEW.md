# M2 Behavior Engine — integration review against the actual M1 code

**Status:** APPROVED by the owner 2026-11-07 with adjustments D4 (stall-aware rest) and D5 (rest-pose fingerprint) — see `so101-milestone2-handoff/DECISIONS.md` Round 5.
**Date:** 2026-11-07.
**Handoff package:** `planning/so101-milestone2-handoff/` (unzipped with `-n`; no existing file replaced).

## 1. Baseline (M2-T001)

| Item | Value |
|---|---|
| HEAD | `4f1c372f659c9bd9b05ebf58a56dc0298bb4ad52` "Milestone 1: … hardware-commissioned" (on `09636e3`) |
| Working tree | clean except untracked `planning/` |
| Interpreter / libs | Python 3.12.3 venv; LeRobot 0.4.4, MuJoCo 3.17.0, PySide6 6.12.0. Locks: `requirements/{dev,hardware}-linux-x86_64-py312.lock` |
| Tests | default `pytest`: **250 passed**, 10 deselected. `-m render` (EGL): **8 passed** |
| Private files (gitignored; must not be touched) | `config/local.toml` (port, calibration path, wrist display offset −2.7°) and `config/rest_pose.left_follower.json` |
| Machine calibration | `left_follower.json`; motor 5 Homing_Offset 381 (ADR-012) |
| Last known hardware state | Motor 5 overload cleared by a power cycle; all six motors at Status 0. No fault is known to be active, but this has **not been re-checked since** (Gate B precondition) |

## 2. How M1 actually works (the parts M2 plugs into)

- **One owner.** `control/coordinator.py`, `ControllerCoordinator`, is the only code that calls the robot. `control/worker.py` drives it on one thread:
  - it handles queued requests before every tick;
  - it **skips missed deadlines rather than replaying them**, so a stall never causes a catch-up burst. This is already part of the M2 stall policy.
- **Two inputs.**
  - `submit(request)`: a queue of operator requests.
  - `set_requested(values)`: a *latest-only* intent slot. It drops intermediate frames, so it is **unsuitable for playback**, which must not skip samples.
- **Command path in MOTION** (`{ARMED, RETURNING_TO_REST}`, mode `SIM_TO_REAL` only): requested → `JointBounds.clamp(hold_outside=staged)` = staged → `robot.send` → sent; then `observe` → actual.
  - No limiter, and `max_relative_target=None` (ADR-010).
  - The clamp also enforces the *no outward ratchet* rule: a joint already outside its range may stay or move inward, never further out.
- **Normal Stop pattern** (`_on_stop_to_rest`, `_check_rest_arrival`):
  - It reads a fresh pose, plans a `RestTrajectory` (a linear joint-space line, all joints synchronized, 30°/s, minimum 0.5 s) and enters `RETURNING_TO_REST`.
  - Each tick sets requested = `traj.at(now)`.
  - Arrival within ±3° arm / ±5 gripper → `HOLDING`.
  - Otherwise, once the deadline (duration + 3 s) passes, it holds the measured pose and reports `REST NOT REACHED`.
  - **M2's player should follow this same pattern.**
- **Permissions.** `available_actions()` is the single gate for every operator action. `MOVE_SLIDERS` is granted only in `SIM_ONLY` or `SIM_TO_REAL+ARMED`, so a new motion state automatically locks out the sliders.
- **Leaving a motion state.** `_set_connection()` clears per-motion state (`_rest_traj`, `_goal_in_flight`). Every fault, hold, torque-off or mode change goes through it, which gives a natural place to cancel a behavior.
- **Torque handling.**
  - `TORQUE_OFF` is a read-only polling state that reads every tick.
  - `EnableTorque` writes goal := present and then turns torque on (the stale-goal fix, R15).
  - `EnableMovement` re-reads the pose and seeds requested = staged = measured before `ARMED`.
- **Display path.** `snapshot.displayed` → `JointMapping.logical_to_model` → `PoseScene` in the one window.
  - In hardware modes, displayed = actual.
  - wrist_roll is a full-turn joint and is shown at its true angle.
- **Rest pose.** It is stored per `robot_id` only, with no calibration fingerprint, and is clamped into the calibrated bounds when recorded.

## 3. Integration map (M2 component → M1 hook)

| M2 component | Where it lives | M1 hook it uses | Notes |
|---|---|---|---|
| `BehaviorRepository`, validator, fingerprint | new `src/so101_companion/behaviors/` (pure; no Qt, serial or MuJoCo) | `domain/joints.validate_joint_values`, the `FileRestPoseStore` atomic-write pattern | Hand-written validator (C2) |
| `BehaviorRecorder` (real, hand-guided) | coordinator-owned helper, runs on the controller thread | new `StartRecording` / `StopRecording` requests; one hook in `_record_actual()` that receives every fresh `JointFrame` | Never calls `send`. Allowed only in `TORQUE_OFF` with `Torque.OFF` and a valid calibration. Any state change aborts the take and marks it invalid |
| Simulation slider recorder | same helper, `SIM_ONLY` | per-tick `requested` (the authored target) | No serial access, because `SIM_ONLY` never touches the robot |
| Keyframe editor | `behaviors/` model + UI | none | Gap rule not applied (C11) |
| `TrajectorySampler`, `TransitionPlanner`, `PlaybackPlan` | `behaviors/plan.py`, `transitions.py` | `JointBounds` (calibrated) for the derived clamp; rest constants from `policy` | Pure and deterministic; the plan hash becomes the preview token |
| Stall-aware playhead | `behaviors/playhead.py`, advanced from `coordinator.tick` | worker deadline skipping | Advances only on a tick whose previous send succeeded (D1) |
| `BehaviorPlayer` | **new `Connection.PLAYING_BEHAVIOR` added to `MOTION`**; a tick branch like the `RETURNING_TO_REST` one | `_set_connection` cancels the run; `_latch` → `FAULT_LATCHED`; `_on_hold` / `_on_torque_off` unchanged | The player only produces `requested`; clamp, send and telemetry stay M1's |
| End of behavior / Stop Behavior | internal call to the `_on_stop_to_rest` logic (factored out so it can be called from `PLAYING_BEHAVIOR`) | `RestTrajectory`, `_check_rest_arrival` | `Rest not reached` already exists (the M1 message) |
| Combined play confirmation | new `PlayBehavior(plan, preview_token, confirmed, enable_torque)` request | runs the same code as `_on_enable_torque` (when in `TORQUE_OFF`) and `_on_enable_movement` | No new hardware path (C12) |
| Preview | UI side: sampler + `QTimer` → `scene.set_logical_pose` | `JointMapping`, `PoseScene` | Allowed only when the connection is not in `MOTION`; shows a "PREVIEW — not the real arm" banner (C3, C4) |
| Telemetry | new tick fields (`behavior_id`, revision, segment, playhead, `timeline_delay_ms`) and `snapshot.extra["behavior"]` | `EventLog`, `Snapshot.extra` | requested / staged / sent / actual are unchanged |

### Priorities, handled entirely by existing M1 mechanisms

The queue is processed before the tick.

- **Emergency Hold, Torque Off, fault:** M1 handler → `_set_connection` → the run is cancelled. No rest.
- **Stop Behavior:** rest.
- **Player:** owns `requested`.
- **Sliders:** blocked, because `MOVE_SLIDERS` is not granted in `PLAYING_BEHAVIOR`.

`PLAYING_BEHAVIOR` must be added to the `EMERGENCY_HOLD`, `TORQUE_OFF`, `DISARM` and `DISCONNECT` action sets and to `HW_CONNECTED`.

## 4. Conflicts and mismatches found

| # | Finding | Evidence | Resolution proposed |
|---|---|---|---|
| **C1** | An M1 test forbids M2 by name | `tests/unit/test_scope_exclusions.py`: `"behaviour engine / recording": r"\b(behavio(?:u)?r_engine\|record_gesture\|replay_gesture)"` | Remove only that entry (deliberate, recorded in an ADR). Keep VR / IK / ground / agent / MCP / speech exclusions, which M2 still forbids |
| **C2** | No room for a `jsonschema` runtime dependency | `test_runtime_dependencies_are_minimal` pins runtime deps to `{mujoco, pyside6-essentials, numpy}`; `jsonschema` is in neither lock | Hand-written structural and semantic validator. `behavior-v1.schema.json` is kept as the contract document. No new dependency |
| **C3** | **The simulation bounds are narrower than the calibrated bounds**, so a preview through the normal `SIM_ONLY` path would not show the effective path | Simulation (MJCF −5°): shoulder_lift ±95, elbow ±91.83, wrist_flex ±90. Calibrated: shoulder_lift lower ≈ −99.88, elbow upper ≈ 92.32, wrist_flex lower ≈ −97.15. The saved rest pose lies **outside the simulation bounds** on those three joints | The effective clamp for real playback always uses the **calibrated** bounds, which requires a connection. Preview bypasses the coordinator's `SIM_ONLY` staging and drives the viewport directly. A preview made while disconnected is labelled provisional and cannot authorize a real run |
| **C4** | The display mapping clamps non-full-turn joints to the model range | `mapping.logical_to_model`: wrist_flex model range ±95 vs calibrated −97.15 | Preview at or near those limits is not pixel-exact. Show the existing `display_clamped` reasons during preview; do not widen the model |
| **C5** | "Preview the exact path" vs "entry starts from the fresh measured pose at play time" | The entry segment depends on a pose known only at Play | D3: preview the entry from the measured pose at preview time; Play refuses (re-preview required) if the fresh pose differs by more than ±3° arm / ±5 gripper |
| **C6** | The M1 rest trajectory is **linear and wall-clock driven** | `RestTrajectory.at(now)`: after a 350 ms stall the next target jumps ahead (up to 30°/s × 0.35 s ≈ 10.5°) | M2 entry and bridge transitions are new code (eased, stall-aware). Whether the M1 rest stays as-is is D4 |
| **C7** | `STOP_TO_REST` is only offered from `ARMED` | `available_actions()` | Factor `_start_rest(now)` out of `_on_stop_to_rest`; call it internally at the end of a behavior and from Stop Behavior in `PLAYING_BEHAVIOR`. M1 behavior unchanged |
| **C8** | The rest pose has **no calibration fingerprint**, only `robot_id` | After ADR-012 the file was shifted by hand ("wrist_roll shifted by −92.31 deg") and the robot id stayed the same | D5: behaviors carry a fingerprint (hash of the calibration motor entries + `JOINT_RANGE_LIMITS`). The rest-pose format is left alone in M2 unless approved |
| **C9** | Expect frequent `Rest not reached` at the end of playback | The rest pose was recorded clamped: shoulder_lift / elbow / wrist_flex physically rest 4.1–4.6° beyond the commandable bound. The elbow missed ±3° by 0.4° in M1 (accepted) | Not a bug; the spec requires reporting it. Called out for Gate B so it is not mistaken for an M2 regression |
| **C10** | The >250 ms gap rule vs observed stalls of up to 350 ms | M1 measurements: median 20 Hz, about 16 Hz average while armed | Some real takes will be rejected by design. Torque-off capture does reads only, so stalls should be rarer; measure in Gate B (B02) |
| **C11** | The gap rule is ambiguous for `sim_keyframes` | REQ-05 says "measured sample gap"; keyframes are seconds apart by nature | Apply the rule to `real_hand_guided` and `sim_sliders` only; keyframes are validated for ordering and finiteness |
| **C12** | The combined "confirm = enable + play" has no M1 equivalent | M1 needs three separate actions: `SIM_TO_REAL`, Enable torque, Enable movement | One confirmed `PlayBehavior` handler runs the existing torque-enable code (goal := present) and the arming code, then starts. Mode must already be `SIM_TO_REAL` (operator-selected) |
| **C13** | A mode change during playback does not rest | M1 `_on_set_mode` drops to `CONNECTED_READ_ONLY`, and the servo keeps its last goal | Matches the spec ("cancel under state-machine rules"). Document that it is a cancel, not a return to rest |
| **C14** | The handoff's hardware facts are older than the repo | The handoff says the wrist cause is "not fully established" | The repo has ADR-012: the seam was moved into the dead zone, giving a continuous arc of −168.4…+168.5 and a usable range of ±163. The attended-only rule still stands. `CLAUDE.md` still says "Gate B NOT RUN / Do not start M2"; update it when M2 is authorized |
| **C15** | Snapshot publishing is not a reliable recording source | The UI receives snapshots via Qt signals (coalescing is possible) | The recorder hooks `_record_actual` on the controller thread and stamps every frame with `JointFrame.timestamp_mono_s` |
| C16 | Stale docstring | `control/worker.py` still mentions "the step limiter" | Fix the wording only |

There is no conflict with ADR-010 itself. The M2 design adds no limiter, no `max_relative_target` and no automatic torque action. Timing smoothness lives in the generated trajectory, which is exactly what ADR-010 anticipated ("the M2 Behavior Engine will generate timed trajectories").

## 5. Decisions needed before coding (recommendations first)

- **D1 — Stall clock.** Recommendation: each tick advances the playhead by `min(dt since last tick, one nominal period 50 ms) × speed`, and only if the previous send succeeded. Any excess is logged as `timeline_delay_ms`.
  - This is the strict reading of A17: no grid point is ever skipped.
  - Consequence: at the measured ~16 Hz average, "1×" takes about 25% longer on the real arm.
  - Alternative: cap at 2 periods. Closer to real time, but one 50 ms sample can be stepped over after a stall.
- **D2 — Entry and loop-bridge shape.** Recommendation: a synchronized joint-space path with a quintic (minimum-jerk) time scaling.
  - Duration = `max(0.5 s, 1.875 × largest distance / 30)`, so the *peak* speed equals M1's 30°/s rest speed. The gripper uses 30 units/s.
  - These are fixed constants in `policy.py`, written down in an ADR and not configurable.
- **D3 — Previewing the entry segment.** Recommendation as in C5: preview from the current measured pose; Play requires a re-preview if the pose moved by more than ±3° / ±5.
- **D4 — M1 rest under stalls.** Recommendation: leave the M1 rest routine unchanged in M2, document the up-to-~10° target jump after a 350 ms stall, and revisit after Gate B.
  - Alternative: make `RestTrajectory` use the same stall-aware clock. That is a small change to M1 behavior and needs your explicit approval.
- **D5 — Fingerprint.** Recommendation:
  - Fingerprint = SHA-256 over the six calibration motor entries + `JOINT_RANGE_LIMITS`.
  - It is required for `real_hand_guided` playback on hardware. Simulation-authored behaviors have no fingerprint and are clamped against the current calibrated bounds at play time.
  - The rest-pose file is not changed in M2.
- **D6 — Where behaviors are stored.** Recommendation: `behaviors/<uuid>.json` at the repo root, **gitignored** (operator data, like the rest pose). Test fixtures live under `tests/fixtures/behaviors/`.

## 6. Implementation sequence (when you authorize it)

This is the handoff's P0–P7, with one change: **the coordinator integration (handoff P5) is moved before the UI (handoff P4)**, so the UI binds to an API that has already been tested. Each step ends with the full default suite and the render suite green.

1. **P0 — Decisions and guardrails** (M2-T001..003)
   - ADR-013 "M2 Behavior Engine integration", recording D1–D6.
   - Narrow the scope-exclusion test (C1).
   - Update `CLAUDE.md` / `IMPLEMENTATION_STATUS.md` pointers.
   - Add `behaviors/` to `.gitignore`.
2. **P1 — Pure data layer** (T004..006). In `behaviors/`:
   - `model.py`: source, revision and edits records;
   - `validate.py`: gap ≤ 0.250 inclusive, strictly increasing, finite, six joints, provenance rules, and a wrist-continuity check that rejects adjacent |Δ| > 180°;
   - `fingerprint.py`;
   - `repository.py`: atomic temp + rename, stable UUID, rename keeps the ID, friendly errors for corrupt files.
3. **P2 — Pure trajectory layer** (T011..015). In `behaviors/`:
   - `plan.py`: trim (boundary samples interpolated, source untouched) → calibrated clamp audit → speed 0.5–2 → repeats N/∞ → entry and bridges → `PlaybackPlan` with a hash;
   - `transitions.py` (D2);
   - `playhead.py` (D1);
   - tests: linear interpolation, speed, loops, 249/250/251/350 ms gaps, a wrist test that never takes the 360° path.
4. **P3 — Controller integration on fake hardware** (T007, T008, T019, T021..023)
   - `PLAYING_BEHAVIOR` state, `PlayBehavior` / `StopBehavior` / `StartRecording` / `StopRecording` requests.
   - Recorder hook; `_start_rest` factored out; snapshot `extra["behavior"]`; tick-event fields.
   - Tests with `FakeRobot` and `FakeClock`:
     - no `send()` while recording;
     - torque ON / UNKNOWN blocks recording;
     - 350 ms `write_latency_s` stalls give no skipped samples and no burst;
     - Hold / Torque Off / fault cancel without rest;
     - completion and Stop → rest → `Rest not reached` path;
     - sliders locked out;
     - re-enable seeds goals from the fresh pose.
5. **P4 — UI** (T009, T010, T016..018, T020)
   - A collapsible behavior panel in the existing window: library, record controls with REC indicator and timer, invalid-take report, trim, speed, repeats, keyframe table (simulation), scrub.
   - Preview player with banner and before/after clamp report.
   - Per-run confirmation dialog that states torque enable, the entry move, playback and the return to rest.
   - Playhead, loop count and delay display.
   - Offscreen GUI tests.
6. **P5 — Offline acceptance** (T024..026)
   - A01–A24 evidence table, ADRs, the operator runbook (adapted from `HARDWARE_PLAYBACK_LATER.md`) and the Gate A sign-off.
   - Gate B items are listed as pending.
7. **P6 (= handoff P7) — Supervised physical commissioning, only with separate per-session approval** (T027..030)
   - Before anything moves: a power-supply and alarm check, and an independent servo-power cut-off available.
   - Then: a one-joint torque-off take, a 0.5× low-amplitude playback, then loops / stop / Emergency Hold / Torque Off.

## 7. Blocking facts

None block offline work (Gate A). Gate B is blocked until:

- the 5 V supply / undervoltage question is settled;
- an independent servo-power cut-off is in reach;
- a fresh M1 check (connect, calibration match, rest) passes on the current setup.

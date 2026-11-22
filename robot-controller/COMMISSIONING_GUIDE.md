# SO-101 hardware commissioning guide (Gate B) — run this yourself, in order

**Status 2026-11-07:** supervised commissioning **done** except the USB-unplug test (B06) and the c04/c05
scripts. Normal Stop now exists (ADR-011): *Record rest pose…* then the blue **STOP** button. Results are recorded in
`IMPLEMENTATION_STATUS.md` ("Hardware commissioning results"). The control policy is now the
**fixed final M1 policy (ADR-010)**: targets are clamped to the calibrated bounds and sent directly,
with no step limiter and `max_relative_target=None`, so **every target moves at full servo speed**.
Use small slider moves, and test large movements in simulation first.

Run each step yourself in a terminal at the desk. Stop at the first failure. Record each outcome
(PASS / FAIL / NOT RUN plus notes) in `templates/HARDWARE_TEST_REPORT.md`.

**If Connect reports a missing motor:** run a read-only scan
(`python -c "from lerobot.motors.feetech import FeetechMotorsBus as B; print(B.scan_port('<port>'))"`).
If a motor replies `Overload error`, support the arm, power-cycle the servo supply and re-scan (ADR-012).

**Wrist caution (ADR-009):** don't twist wrist_roll by hand through more than half a turn while
torque is off. That preceded a ~337° wrist runaway on 2026-11-07. Runaways are now *reported*
(orange warning, `tracking_divergence` event) but **not stopped automatically**. Use EMERGENCY HOLD
or Torque OFF.

Software that already passed offline (Gate A) is in `IMPLEMENTATION_STATUS.md`.

## 0. Before you start (once)

1. **Install the hardware driver** in the project venv. The default install does not include it.
   ```bash
   cd robot-controller
   uv pip sync --python .venv/bin/python --torch-backend cpu requirements/hardware-linux-x86_64-py312.lock
   uv pip install --python .venv/bin/python --no-deps -e .
   .venv/bin/python -m pytest -q tests/driver_contract     # expect 14 passed (real LeRobot 0.4.4 code, fake bus)
   ```
2. **Prepare the arm.** Clamp it to the desk and clear the space around it. Use the correct power
   supply. Plug the USB adapter of the **follower** arm in.
3. **Serial permissions.** Your user must be in the `dialout` group (`groups`). If it isn't, run
   `sudo usermod -aG dialout $USER`, then log out and back in.
4. **Find your existing calibration.** It's a LeRobot calibration file, usually
   `~/.cache/huggingface/lerobot/calibration/robots/so_follower/<id>.json`. With SO-101-VR-Control it
   is `left_follower.json`. **M1 never calibrates.** If the file is missing, calibrate first with
   LeRobot's own supervised tool, outside this app.
5. **Find the stable port name.** `ls -l /dev/serial/by-id/` shows it. Avoid `/dev/ttyACM0`, because
   its number can change.
6. **Physical stop.** Keep a hand on the arm's power switch or plug during every test. Emergency
   Hold and Torque Off are best-effort software actions, not an emergency stop.

Every script needs `--operator-present`, refuses to run without an interactive terminal, and asks for
typed words before connecting (`CONNECT`), moving (`MOVE`) or removing torque (`TORQUE OFF`). Ctrl-C
during motion requests a best-effort Emergency Hold. Each run writes a JSON report and an event log to
`commissioning-reports/`.

Common arguments are `--port /dev/serial/by-id/usb-…` and `--calibration ~/.cache/…/<id>.json`. If you
omit them, the script asks. It lists candidates but never picks one for you.

## 1. Connect and read (B01) — no motion commands

```bash
.venv/bin/python scripts/commissioning/c01_connect_and_read.py --operator-present --port … --calibration …
```
**Expect:** connection CONNECTED_READ_ONLY, calibration VALID, six plausible values (arm in degrees
around 0 at mid-range, gripper 0–100), about 20 Hz reads, **0 commands**.
**If it fails:** `CALIBRATION_MISMATCH` means the wrong file or that the arm was re-calibrated. Stop
there. For a connect error, check the port, power and cable. Nothing will have moved.
*Note:* LeRobot `configure()` switches torque off and back on while connecting. Support the arm.

## 2. Real-to-sim mirror (B02) — visual

```bash
.venv/bin/python scripts/commissioning/c02_mirror_gui.py --operator-present --port … --calibration …
```
This opens the normal window with the port and calibration fields filled in. Follow the printed
checklist: Connect → REAL_TO_SIM → Torque OFF while you support the arm → move one joint at a time by
hand → Enable torque → Disconnect.
**Expect:** the 3D model follows the **measured** pose of every joint, and SENT stays N/A.
**If a joint goes the wrong way or sits offset:** note it, then set
`[simulation] joint_sign` / `joint_offset_deg` (and `logical_gripper_0_maps_to` for the gripper) in
your own config file. Use it with `--config my.toml` and repeat this step. The official model files
always stay unchanged.

## 3. One joint at a time (B03) — first real motion

```bash
for j in shoulder_pan shoulder_lift elbow_flex wrist_flex wrist_roll; do
  .venv/bin/python scripts/commissioning/c03_one_joint.py --operator-present --port … --calibration … \
      --joint $j --delta 5
done
```
**Expect:** a small move of only that joint, arriving within ±1.5°. Requested = staged = sent (forwarded
directly). The joint moves in the direction you expected, then returns to the start pose and holds.
**If it fails:** if the direction is wrong, stop and fix the sign (step 2) before anything else. If it
doesn't arrive or a FAULT appears, keep the printed telemetry for the report.

## 4. Gripper (B05 in the matrix)

```bash
.venv/bin/python scripts/commissioning/c04_gripper.py --operator-present --port … --calibration …
```
Keep the jaws empty and your fingers clear. **Expect:** a 15-unit move, `sent == staged` (forwarded
directly), and LeRobot's gripper current/torque limits stay active. The script records whether an increasing value **opens** the gripper and recommends a value
for `logical_gripper_0_maps_to`.

## 5. Limits and clipping (B04)

```bash
.venv/bin/python scripts/commissioning/c05_limits_and_clipping.py --operator-present --port … --calibration … \
    --joint elbow_flex --side upper            # repeat per joint / side you care about
```
**Expect:** staged stops exactly at the allowed limit (calibrated range minus 5°) and the joint stops
before its mechanical end stop. The joint travels there at **servo speed** (no limiter), so start
with the joint already near that limit.
**If the joint touches a mechanical stop:** FAIL. The calibration doesn't match the arm.

## 6. Full synchronisation and timing (B09, then B05 visual)

```bash
.venv/bin/python scripts/commissioning/c06_full_sync_and_timing.py --operator-present --port … --calibration …
```
**Phase 1** measures the achieved controller Hz and cycle times on the real bus: read-only, then armed
while holding the measured pose, with no motion intended. Record the numbers. 20 Hz is a target, not
a guarantee.
**Phase 2** opens the window for a SIM_TO_REAL check. Move sliders slowly; the arm follows. Press
EMERGENCY HOLD once while it's moving.

## 7. Record the physical rest pose (preparation for B08) — read-only

```bash
.venv/bin/python scripts/commissioning/c07_record_rest_pose.py --operator-present --port … --calibration …
```
Optionally turn torque off and place the arm by hand (support it). Then enable torque so it holds
there; the script saves `commissioning-reports/rest_pose.candidate.<time>.json`.
**This does not create a Stop-to-Rest button.** Normal Stop-to-Rest is a separate task, T-039, that
needs your approval of the trajectory, speed, arrival tolerance, timeout and holding behaviour.

## 8. Emergency Hold, Torque Off, fault latch (B07, B06)

```bash
.venv/bin/python scripts/commissioning/c08_hold_torque_fault.py --operator-present --port … --calibration …
```
**Expect:**
- HOLD mid-motion: the state becomes HOLDING, no further sends, and the arm stops.
- TORQUE OFF (you support the arm): the arm goes limp and the mirror still follows.
- Enable torque: the arm holds without jumping.
- Unplug the USB cable while in read-only mirror: FAULT_LATCHED within a fraction of a second, torque
  shows UNKNOWN, and nothing happens on its own.
- After replugging: still latched. Recovery is Disconnect, then a fresh Connect (step 1).
- **Normal Stop:** NOT RUN. The feature doesn't exist until T-039.

## After commissioning

- Put the sign, offset and gripper results into your config, and keep it under version control.
- Fill in `templates/HARDWARE_TEST_REPORT.md` and attach the `commissioning-reports/` files.
- If any safety check failed, don't start behaviour work (M2). Report the failure first.

---

# Milestone 2 — supervised behavior validation (Gate B, M2-B01…B10)

**Only with the owner's explicit authorization for that session.** Software (Gate A) passed offline;
nothing below has been run on the arm yet. Specs: `planning/so101-milestone2-handoff/`; design:
ADR-013 and ADR-014. Record outcomes in `planning/so101-milestone2-handoff/milestone-2/tests/acceptance-matrix.md`.

**Preconditions (all of them, every session)**
- You are at the arm the whole time. Playback is **never unattended**.
- The servo-power cut-off is within reach. **USB removal is not an emergency stop:** the servos keep their last target.
- The arm is clamped to the desk, and the workspace is clear through the full range.
- No active voltage, overload or current alarm. The 5 V supply once caused a gripper "Input voltage error": if Connect reports one, stop.
- No unexplained wrist change since ADR-012 (wrist range ±163°). If the wrist was recalibrated, stop: the mapping, the rest pose and every recorded behavior must be re-checked (they will be refused automatically).
- **Abort** on any of these: an unexpected large wrist movement, a power or overload warning, stale or jumping readings, a calibration mismatch, uncontrolled acceleration, or a communication loss. Use EMERGENCY HOLD, Torque OFF or the power cut-off, and do not continue until the cause is understood.

**Steps, in order (stop at the first failure)**

0. **Launch and check M1** (no motion).
   1. Start the app with `--config config/local.toml` and Connect.
   2. Select REAL_TO_SIM. The 3D arm should mirror the real one.
   3. Rest line, first session after the update: it shows **"Rest pose NOT usable … Adopt rest pose…"** (ADR-014). Then either:
      - press *Adopt rest pose…* and check the displayed pose is the arm's real rest pose (the file is backed up first); or
      - support the arm in its rest pose and use *Record rest pose…*.
1. **B01 Torque-off hand guidance**
   1. Support the arm, press **Torque OFF** yourself, then **● Record hand-guided**.
   2. Make a small, slow gesture of 2–4 s (e.g. elbow ±10°, a little gripper). Avoid large wrist rotations.
   3. Press **■ Stop recording** and name it.
   - Expect: REC shows the time and samples; the torque stays OFF; the event log shows no `send`.
2. **B02 Gap quality.** If a take is reported "NOT playable … ms gap", that is the agreed rule working (>250 ms). Note how often it happens, then re-record.
3. **B03 Preview** (optional since owner decision D7, 2026-11-07; recommended for every new behavior or new settings). Preview is locked while movement is enabled: press **Disarm** first. PLAY works without Enable movement.
   1. Select **SIM_TO_REAL**. Keep the torque off, or turn it on with *Enable torque…*.
   2. Select the behavior and set speed **0.5×**, repeat **1**.
   3. Press **▶ Preview**. Watch the entry move, the gesture and the planned return to rest in the 3D view only (banner "PREVIEW — not the real arm").
   - Expect: "Preview complete — authorizes ONE real run". Check the clamp report and any "drawn at …" display notes.
4. **B04 First real run**
   1. Support the arm.
   2. Press **PLAY on real arm…** and read the dialog. If the torque was off, it says the motors will be energized.
   3. Confirm.
   - Expect: no jump at enable; a smooth entry (short since D8, within the per-joint limits of ADR-015) into the gesture; then the return to rest and a hold.
   - If the elbow misses ±3°, it shows `REST NOT REACHED` and holds. That is not a failure of playback.
5. **B05 Fidelity.** Export the log. Compare the requested / staged / sent / actual traces for direction and relative motion. Expect some lag at 0.5×.
6. **B06 Gripper and wrist.** Make a short take with a gripper open/close and a *small* wrist roll, then preview it and play it at 0.5×. Expect correct directions and no seam travel. Stop if anything is abnormal.
7. **B07 Looping.** Set repeat 2, preview (the bridge is visible), then play. Expect a smooth bridge, no abrupt return to the first frame, then rest.
8. **B08 Interruption.** Set "until stopped", preview, play, then press **Stop behavior → rest** (or the blue STOP) mid-gesture. Expect the M1 return to rest; any ±3° failure is visible.
9. **B09 Emergency controls**, each with its own preview and play, with the arm supported:
   - **EMERGENCY HOLD** mid-play: the arm holds and there is **no** return to rest.
   - **Torque OFF** mid-play: the arm goes limp (support it) and there is no return to rest.
   - Expect: the panel shows "CANCELLED by …" and nothing restarts.
10. **B10 Faults.** Do **not** hot-unplug during motion. The offline tests cover the latch. If a fault happens naturally, verify FAULT_LATCHED, no new sends and no auto-restart.
11. **Optional:** 1× and then 2×, only after stable 0.5× runs, with good tracking and no power warnings. Fast takes may exceed what the servos can do: watch the divergence warning.

**Session log**
- **2026-11-07, first M2 session:** B01–B05 and B07 passed; 0.5× not yet tried (both runs were at 1×).
- **Still to do:** B06, B08, B09, B10.
- **Before the next session:** check how the arm is resting before Connect. If it is far from the rest pose, don't use STOP from a folded side position without first checking the path. Fold it by hand with Torque OFF instead.

**B11 — Responsive transitions (D8 / ADR-015). Only with the owner's approval.** Do one step at a time,
with the arm clamped and the power cut-off in reach. Stop at the first voltage or overload message,
abnormal noise or unexpected wrist motion.
1. **Small entry.** Start from the rest pose with a gesture that starts close to it (under 30°) at 0.5×,
   repeat 1. Expect an entry of about 0.5 s and no warning.
2. **Medium entry.** Use a gesture that starts 60–90° away, at 1×, repeat 1. Expect an entry of about
   1.5–2 s.
3. **Bridged loop.** Repeat 3 at 1×. Expect bridges of about 0.3–0.6 s, with no pause or hold between
   repetitions.
4. **Seamless loop.** Record a gesture that ends where it starts, and play it with repeat 3. The
   panel/log shows `seamless_loop: true`; expect no bridge.
5. **Large entry.** Use the full fold-to-reach gesture (about 160° on the elbow) at 1×. Expect about
   2.7 s. Watch shoulder_lift and the power supply.
6. Export the log and compare requested / sent / actual for each transition. The shoulder_lift and
   elbow limits are raised only if tracking is good and no warning appeared.

**Studio UI (ADR-016): where the old controls went**
- **Mode:** the radio buttons became the **Control mode** dropdown.
  - *Real → Simulation* connects if needed and turns torque OFF (support the arm).
  - *Simulation → Real* connects if needed, then reads, energizes holding the present pose, and enables
    movement.
- **STOP** is now **Go Rest**. From a still arm it asks first.
- **PLAY on real arm** is now **Play** in Simulation → Real. In the other modes, Play is the 3D
  playback.
- **Stop behavior** is now **Go Rest** while playing. **Pause** holds the arm in place; Resume rejoins
  smoothly.
- **Connect/Disconnect, Enable movement/Disarm, Torque OFF/Enable torque, Recover, Record/Adopt rest
  pose, sliders, telemetry and the log** are all in **Advanced settings & diagnostics**.

**Named poses and endings (ADR-017).** Not yet checked on the arm; this needs the owner's authorization.
1. In Simulation → Real, press **Poses → Save current**, then rename the pose.
2. Press **Go** from another position. Expect a smooth move, then a hold.
3. Set **After behavior → Hold last position**, play with repeat 2, and expect the arm to stay at the
   last pose.
4. Set **After behavior → Move to saved pose**, play, and expect a smooth move to the pose, then a hold.
5. **Emergency Hold** during the ending move: expect an immediate hold.
6. **Go Rest** during the ending move: expect a return to the default rest pose.

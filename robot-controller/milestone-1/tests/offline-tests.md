# Gate A offline tests — detailed cases and oracle

## Unit / validation

1. Exactly six allowed key names, complete numeric frame. Reject omitted gripper, `shoulder_pan_xyz`, duplicate/extra names, NaN/Inf, wrong data types or incorrectly tagged units.
2. Degrees-to-radians uses `pi/180` for each of the five arm joints. Find coordinates using MuJoCo joint names; fail on a missing/mismatched name.
3. Gripper normalized endpoints 0, 100 and midpoint 50 map deterministically into the reported official MJCF range, with a documented provisional direction. Do not pretend actual real direction was tested.
4. With permitted angular range [-90,90] and margin 5°, input 88° is clamped to 85°; gripper 105 →100 and -1→0. Reject inverted bounds (minimum+margin > maximum-margin).
5. With staged 0° and continuous desired 90°, 10 units/tick gives 10,20,30 after 3 **actual** control cycles. If a frame arrives late, still at most one 10-unit advancement for that emitted tick; no 30-degree jump to 'catch up'.
6. Fake LeRobot with actual 5° and staged 40°, cap 20° returns sent 25°; record requested/staged/sent distinctly. Gripper 0 measured, 80 desired after sufficient staging, cap 100 does not further clip.

## State / fault injection

7. On `SIM_ONLY` startup: fake observe/send/initialize counters all 0; UI launch must not instantiate or probe serial devices.
8. Connect confirmation is required; `connect(calibrate=False)` mocks indicate configuration may temporarily disable torque. Without calibration valid, no actual `ARMED` state.
9. `REAL_TO_SIM` produces zero hardware writes even with slider updates; simulated pose comes from actual observation.
10. `SIM_TO_REAL` refuses sends unless connected + armed + calibration valid; mode exit stops target progression.
11. On fake hardware read exception after 4 sends, future 100 UI slider changes produce zero more sends; fault latched and visible; cannot clear by merely receiving one good packet.
12. On fake hardware write exception, sent telemetry remains previous timestamp/marked stale; actual not fabricated from target.
13. Emergency Hold healthy: read actual, send holding target and stop new requested progression. Faulted bus: show HOLD FAILED and latch fault, no claim of guaranteed stop. Torque Off: explicit warning/confirmation, fake API called only on confirmation.
14. On app quit/disconnect, show torque-configuration caveat; no blind exception-handler torque toggle. No automatic rest motion.

## GUI/render integration

15. Under Ubuntu 24.04 desktop, one main window contains all controls and 3D. Six sliders move the preview; 3D never launches detached viewer. Screenshot/log artifact retained by implementing agent (not part of initial spec ZIP).
16. Simulated 1s hardware call does not freeze entire UI. Rendering context owned/used consistently on one thread; backend error shown as failure, not silent blank preview.
17. Rendering screenshot shows the official SO-101 model and no missing meshes; changing named joint changes expected visible part. Record GL backend, desktop session, actual fps.
18. CI runs all core tests without a graphical display or serial port; GUI smoke test optional separately or run with verified offscreen backend.

## Suggested commands (after implementation, illustrative)

```bash
python -m pytest -q tests/unit tests/integration
python -m pytest -q tests/gui -m 'not hardware'
python -m so101_companion --mode sim-only
```

The CLI/package do not exist yet; coding agent must update commands to those actually implemented and record test outputs. Automated test suite must fail if serial open occurs unexpectedly.

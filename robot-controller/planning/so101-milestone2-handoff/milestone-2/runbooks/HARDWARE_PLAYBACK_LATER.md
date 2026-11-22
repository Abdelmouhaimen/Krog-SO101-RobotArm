# Supervised M2 hardware-commissioning runbook — execute later, not by this package

## Preconditions
- Operator physically present and prepared to support arm; desk clamp secure; people and objects clear from full range.
- Independent servo-power isolation available; **USB unplug is not a guaranteed emergency stop**.
- No persistent voltage/current/overload alarm; verify appropriate power supply and wiring before moving.
- M1 comms/torque/rest/calibration verified for current setup; no unexplained new wrist-angle change. If wrist re-calibrated, recheck mapping and all recorded behavior compatibility.
- Use small, low-energy gestures first; don't assume fast 2x playback is safe just because preview looks right.

## Suggested order
1. Launch single-window app and inspect current M1 feedback/mapping with **no motor commands**.
2. Read-only/torque-off confirmation, support arm, manually move a small range; record at 20Hz nominal and check timestamps, gripper, sample-gap quality.
3. Save and preview the exact effective clamped trajectory in MuJoCo; verify wrist and gripper direction, loop bridge, return-to-rest.
4. Operator approves play; dialog makes motor-enabling consequence explicit. Confirm no jump at initial enable; enter start pose gradually.
5. Execute **one short gesture at 0.5x** with no aggressive wrist motion; compare requested/effective/sent/actual.
6. Check normal completion/rest; if elbow misses ±3° rest tolerance, show `Rest not reached` and hold rather than retry silently.
7. Try short finite loop with transition, stop behavior mid-play, confirm return-to-rest; separately verify Emergency Hold and Torque Off with physical support.
8. After stable behavior, test nominal 1x and 2x only if observed tracking and power allow it; verify stalls don't skip trajectory time.
9. Review logs and acceptance matrix; leave unresolved failure items unapproved.

## Abort conditions
Unexpected large wrist movement, active power/overload warning, stale/unreliable encoder readings, calibration mismatch, uncontrolled physical acceleration, communication loss, or inability to stop: cease trial, use accessible safe hardware isolation if necessary, and do not continue until root cause investigated.

## Safety semantics
Normal Stop means **return to rest**. Emergency Hold means attempt to hold current measured pose; it is not a hard power cut. Torque Off makes the arm limp and must be supported. Communication loss can leave last servo target in effect. Never claim stopping completed if no bus acknowledgement.

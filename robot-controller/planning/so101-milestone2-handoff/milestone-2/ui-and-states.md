# UI workflow and state transitions

## One window, minimal MVP
Keep the existing MuJoCo 3D viewer, six joint sliders and requested/staged/sent/actual table. Add a collapsible behavior panel with library, name/description, Record, Stop Recording, Trim start/end, speed 0.5–2.0, repeat N / until stopped, basic keyframe insertion/editing for sim source, scrub bar, Preview, Play on Robot, Stop Behavior, and clear status/error/log messages. No joint-curve editor.

## User workflows
### Real hand-guided recording
1. Connect/validate M1 calibration. **User manually selects Torque Off**, supports arm; app verifies torque status OFF (unknown is not enough).
2. Press Record; show REC and timestamp/source; robot is read-only during capture.
3. Hand-guide all six joints; stop recording. Verify sample quality (gap <=250ms, valid positions) and show error intervals if invalid.
4. Save as new behavior or discard. Never fabricate `requested`/`sent` when torque is off.

### Simulation authoring
- `SIM_ONLY`: either drag sliders with recording active (timestamped) or insert/edit explicit keyframes with their times. No servo access; preview and edit all six joints.

### Preview, approval, real playback
1. Select exact behavior revision and playback settings; clamp out-of-bound frames with visible before/after report based on current **physical** calibrated bounds.
2. Preview effective trajectory in MuJoCo without sending hardware commands. Preview invalidated by any edit, settings change or calibration change.
3. `Play on real arm` only if operator present, current calibration valid, no fault, coherent wrist mapping, and confirmed rest pose available.
4. **One confirmation may also enable torque/movement:** dialog explicitly states that motors will energize and arm will move to starting pose, then replay, then return to rest; no silent arming.
5. Read fresh actual pose; initialize servo goal to measured position before torque enabling as M1 does. Start distance-dependent entry transition, then behavior, bridges, rest.
6. Show requested/effective/sent/actual and playback clock; never treat requested pose as measured.

## Precedence and exclusive authority
`Torque Off / Emergency Hold / fault` > `user Stop Behavior` > `behavior player` > `manual sliders`. Emergency Hold and Torque Off are manual overrides, never auto-route to rest. User Stop Behavior initiates normal rest. On error/connection loss, latch fault and stop new sends; do not promise a physical hold if bus is disconnected. No automatic reconnection or replay.

## Recorder vs player
- Recorder cannot be active when physical motor movement is enabled.
- Player cannot run when recorder is active or manual sliders own hardware authority.
- Real-to-sim mirror can operate during torque-off physical record; simulation authoring uses sim-only mode.
- If user changes modes/disconnects mid-play, cancel playback under state-machine rules before performing mode action.

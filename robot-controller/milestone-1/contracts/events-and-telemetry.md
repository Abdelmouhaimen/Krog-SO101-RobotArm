# Event and telemetry contract

Each controller tick emits a local structured record; log files are exportable from the UI but must not contain serial device secrets or user-specific information unrelated to robot telemetry.

```json
{
  "kind": "control_tick",
  "seq": 17,
  "t_monotonic_seconds": 42.500,
  "mode": "SIM_TO_REAL",
  "authority": "ARMED",
  "requested": {"shoulder_pan": 80.0, "shoulder_lift": 10.0, "elbow_flex": 0.0, "wrist_flex": 0.0, "wrist_roll": 0.0, "gripper": 50.0},
  "staged": {"shoulder_pan": 20.0, "shoulder_lift": 10.0, "elbow_flex": 0.0, "wrist_flex": 0.0, "wrist_roll": 0.0, "gripper": 50.0},
  "sent": null,
  "actual": null,
  "displayed": null,
  "clamp_reasons": ["shoulder_pan:step_limit"],
  "last_observation_age_seconds": null,
  "cycle_duration_ms": 8.0
}
```

The example values are illustration only; null fields mean *not yet available*, not zero. Other events: `startup`, `calibration_validated`, `connect_confirmation`, `hardware_connected`, `movement_enabled`, `command_sent`, `observation_received`, `fault_latched`, `hold_requested`, `torque_off_confirmed`, `mode_switched`, `disconnect_requested`, `render_fault`.

Failure handling: no fabricated `actual` field; error events are retained with a human-readable cause and timestamp. The fault path flushes pending intents; UI must visibly show stale actual data. A `sent` field can only be updated from LeRobot's return value after a successful call, or fake adapter equivalent in tests.

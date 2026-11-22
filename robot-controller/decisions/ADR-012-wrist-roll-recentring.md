# ADR-012 — Re-centring the wrist_roll calibration (motor 5) so its usable arc is continuous

**Status:** Done 2026-11-07, owner-approved and supervised. This is a **motor configuration
change**; it was declined earlier in the day and approved later.

## Problem

- LeRobot calibrates wrist_roll as a full-turn motor (range 0…4095 → ±180°). With the original
  calibration (Homing_Offset −669), the encoder seam (±180°) fell **inside** the wrist's usable arc.
- The arc ran −76 → 0 → +180 | −180 → −99, with a mechanical dead zone between −99.2° and −76.1°
  (measured).
- A position-mode servo cannot move across its seam. Commands between the two sides would turn the
  long way into the end stop (consistent with the ADR-009 runaway).

## Procedure (all scripts read-only except one register write)

1. **Read-only sweep:** 45 s at 20 Hz while the operator turned the limp wrist from stop to stop.
   Largest unvisited gap: raw 919…1182 = 23.1°, centre raw 1050.5.
2. **Backup:** `left_follower.json.bak-20261107-before-wrist-recenter` (same folder).
3. **Write** (torque already off): motor 5 `Homing_Offset` −669 → **381** (= −669 + 1050). LeRobot
   0.4.4 Feetech rule: Present = Actual − Homing_Offset (mod 4096). Readback 381; Present moved
   904 → 3950, exactly as predicted. Min/Max limits unchanged (0/4095).
4. **Calibration file:** only `wrist_roll.homing_offset` changed to 381. The app's register-vs-file
   check passes.
5. **Stale goals:** Goal_Position was synced to Present on all six motors (RAM, torque off, no motion).
   They differed by up to 130° (elbow), left over from the last commanded targets. No connect-time
   jump was observed by the operator; recorded as risk R15.

## Result

| | Before | After |
|---|---|---|
| Usable arc (logical) | −76…+180 ∪ −180…−99 | **−168.4 … +168.5** (continuous) |
| Allowed range (`policy.JOINT_RANGE_LIMITS`) | −75…+175 (one side only) | **±163°** (stops − 5° margin) |
| 3D display offset (`config/local.toml`) | −95° | **−2.7°** |
| Saved rest pose wrist_roll | +88.9° | −3.4° (same physical pose) |

Re-measure and update these values after any future recalibration of the wrist.

## Follow-up: wrist servo overload (same day)

- **What happened:** after the re-centring, three Connect attempts failed with "missing motor 5".
  A read-only scan showed motor 5 replying with **`[RxPacketError] Overload error`** (latched
  protection), so LeRobot's handshake treated it as absent.
- **Fix:** a power cycle of the servo supply cleared it. Afterwards all six motors answered,
  Status 0, 33 °C. `Homing_Offset` 381 persisted, and the file still matches the motors.
- **Cause: not proven.** The most plausible one is the previous session's −75° wrist limit: the
  owner's figure was used without the 5° margin, so it sat only 1.1° from the −76.1° end stop.
  The wrist was held there for 23.5 s (median tracking error 0.04°), possibly against cable/stop
  load at 5 V. The current range (±163°) keeps the full 5° margin from the measured stops.
- **Observation:** after power-up, every Goal_Position register reads 0. The day's first connect
  (also after power-up) produced no motion, which suggests the servos do not drive to the stored
  goal when torque is enabled (R15). Goals were synced to Present anyway.
- **Operator note:** if a connect reports a missing motor, run a read-only scan; an `Overload error`
  reply means: support the arm, power-cycle the servo supply, re-scan.

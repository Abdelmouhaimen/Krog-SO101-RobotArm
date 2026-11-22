# Fixed M1 control policy and M2 playback preconditions

## Normative — do not change M1
- Physical joint commands are **direct calibrated target positions**, after existing permitted-range clamp and 5° margin.
- **No 10°/tick limit**; LeRobot `max_relative_target=None`. Do not sneak back earlier per-command limit via animation engine.
- Gripper use known logical 0–100 convention and current physical/mapped bounds.
- No ground protection, IK, VR, auto torque gains or electrical-fault suppression.
- M1's communication fault remains latched; keep manual Emergency Hold and explicit Torque Off.
- Tracking-divergence is diagnostic/warning under the approved fixed policy; do not assert automatic prevention of runaway.

## Added behavior workflow protections (not low-level motor limiters)
- Every **real** run must have prior matching MuJoCo preview and a fresh, explicit action confirmation. If confirmation is also an enable operation, show full implications and first initialize the motor goals to current measured pose.
- Physical play requires calibrated bounds, matching joint mapping/calibration fingerprint, valid rest pose, known connection/torque state, valid behavior, supported arm and operator present.
- Clamp invalid keyframes to bounds in a separate effective revision; show original vs result for changed joints and preview result. Re-clamp on calibration change; never assume old preview still valid.
- A 337° unexpected wrist motion occurred previously, with cause not fully established; **no unattended playback**. Require human observation and an accessible independent servo-power isolation method before hardware run. USB removal is not guaranteed to cut servo power.
- Existing undervoltage alarm/5 V supply concerns must be surfaced; do not bypass servo electrical protections. Decline physical testing if active alarm persists.
- Motion authoring at 2x can ask for speeds beyond the motor's ability; display live tracking error, allow operator interruption. Do not promise time-accurate physical motion.

## Stop and faults
- Normal completed/interrupt -> **existing saved-rest routine**, hold torque on. If rest cannot reach ±3° tolerance: report `Rest not reached`, hold reached pose as M1 does, never claim success.
- Emergency Hold -> cancel behavior immediately, issue M1 hold semantics, **do not start rest**.
- Torque Off -> cancel behavior, torque off, arm may fall; support it.
- Communication fault -> latch fault, no new movement, no silent resume/reconnect. If communication lost, do not claim stop/hold command reached motor.
- Re-enable after torque-off must update target from fresh actual encoder feedback to avoid stale-servo-target jump.

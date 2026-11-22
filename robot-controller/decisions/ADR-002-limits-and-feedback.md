# ADR-002 — Reuse limits, no new torque controller

> **Partly SUPERSEDED 2026-11-07 by ADR-010** (owner decision after hardware commissioning): the 10-unit step limiter and the `max_relative_target` dictionary are removed (`max_relative_target=None`). Calibrated bounds with the 5° margin, fail-closed calibration, and "no torque controller / no ground protection" remain.

**Status:** Accepted / implementation conversion needs proof.

Use VR source's 10 logical units per *issued controller tick* limiter for all six joints. Keep per-joint joint-limit enforcement with 5° margin on arm angular axes only. Require verified physical calibration before real writes (unlike original VR source's URDF fallback). Use existing LeRobot `max_relative_target` dict: 20° five arm joints, 100 normalized gripper. No custom measured-position limiter and no invented force/voltage control. The source's raw calibration midpoint-to-degrees calculation must be proven compatible with LeRobot normalization before being adapted. No ground protection.

Why: Preserve established source approach, avoid mysterious tracking changes, reflect original user's preferences and allow configurable 20° cap. Do not conflate 10/step with 20° relative to measured.

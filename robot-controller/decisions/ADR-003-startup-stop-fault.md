# ADR-003 — Explicit movement authority, stopping and recovery

**Status:** Accepted 2026-11-06.

App boots in sim-only mode, no serial open. Hardware connection/configuration requires explicit warning/confirmation because LeRobot may disable torque/configure servos. Separate movement enable. USB errors latch fault, stop further sends and require manual recovery; never auto resume; do not claim torque state can be preserved after link failure. Provide best-effort Emergency Hold and explicitly confirmed Torque Off, neither physical E-stop. No automatic calibration. **No normal Stop-to-Rest functionality until physical arm is present and a real measured pose can be recorded**, despite earlier desired future behavior; do not provide sim rest pose before that time.

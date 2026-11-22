# SO-101 Companion — Milestone 2 Spec-Driven Handoff

**Status:** Requirements approved after four interview rounds; **no Milestone 2 implementation has been performed by this package**.

**Purpose:** Extend the current, hardware-tested SO-101 ↔ MuJoCo application with a behavior recorder, editable local library, simulation preview, and supervised real-arm playback. The future Valorant agent, MCP, speech synchronization, VR, and IK are excluded.

## Read order for Claude Code / Codex
1. `SOURCE_OF_TRUTH.md` — current repository is authoritative, not this package's old baseline.
2. `DECISIONS.md` — approved user decisions.
3. `milestone-2/spec.md` — normative requirements.
4. `milestone-2/contracts/behavior-format.md` and `trajectory-clock.md` — data and timing contract.
5. `milestone-2/architecture.md` and `ui-and-states.md` — integration design.
6. `milestone-2/safety-and-faults.md` — protected behavior and known risks.
7. `milestone-2/implementation-plan.md`, `tasks.md`, `tests/acceptance-matrix.md`.
8. `CLAUDE_CODE_HANDOFF.md` — staged handoff prompt and stop conditions.

## Gate model
- **Gate A (offline)**: implementation and automated tests can run without physical motors.
- **Gate B (supervised physical)**: requires user's explicit permission for each movement, a supported arm, available servo power isolation, and captured evidence. May remain pending independently.

The current robot project was reported at local commit `4f1c372` after Milestone 1; verify the actual working tree and newer commits. **Do not reset, replace, or overwrite current source code with anything from the old spec archive.**

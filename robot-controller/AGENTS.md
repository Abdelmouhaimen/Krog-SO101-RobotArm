# Instructions for Claude Code, Codex, or any coding agent

> **Status 2026-11-07:** M1 done; M2 Behavior Engine implemented (ADR-013/014), Gate A PASSED offline, Gate B NOT RUN. See `IMPLEMENTATION_STATUS.md` and `planning/so101-milestone2-handoff/` (owner decisions D1–D6) before changing anything. M3 is out of scope.

You are implementing **ONLY Milestone 1**. Read `README.md`, `PROJECT_CONTEXT.md`, `CONSTITUTION.md`, `research/SOURCES_AND_FINDINGS.md`, `milestone-1/spec.md`, `milestone-1/implementation-plan.md`, `milestone-1/tasks.md` in that order. Do not ask the user to repeat answered questions. All requirements are in these files. Honor last-round decisions over earlier versions.

Execute tasks in dependency order, one phase at a time. Start with **P0 native single-window MuJoCo rendering feasibility**. Maintain `IMPLEMENTATION_STATUS.md` after each phase: what exists, command/test results, risk changes and unverified hardware steps. No real-hardware tests now, because the arm is currently unavailable.

Before coding any LeRobot adapter: inspect installed package API and version, fetch and pin exact upstream versions/commits, confirm six motor names, calibration path semantics and per-motor `max_relative_target` support. Use existing LeRobot safety implementation, not a rewritten copy. Inspect VR source for calibration margins; do not blindly copy raw-encoder conversion into normalized joint degrees. Include source/commit/license attribution for any incorporated source or model assets.

Don't expand scope into VR, IK, ground protection, games or MCP. (Behaviors were added by the owner-approved M2, ADR-013.) The single-window GUI is essential. Interactions must not implicitly connect/enable hardware. Keep REST disabled until actual measured physical pose capture is authorized and feasible.

When tests need a servo, replace it with a **fake adapter** that mimics observations, clipping, read failures and write failures. Use test markers so all unit tests can run without `ttyUSB*`, physical robot, or graphics display. Use a real renderer only for a labeled optional smoke test.

Any implementation choices not expressly approved must be logged in `decisions/` with alternatives and consequences. No silently invented hard-coded joint zero offsets or gripper direction. Separate proven facts from provisional mapping and actual hardware verification.

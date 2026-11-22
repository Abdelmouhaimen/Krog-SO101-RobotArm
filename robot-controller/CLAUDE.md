# Claude Code entrypoint

> **Status 2026-11-07:** M1 done (ADR-010 fixed policy). **M2 Behavior Engine implemented (ADR-013/014/015); Studio UI redesign (ADR-016), named poses (ADR-017). M2.5 reactive HTTP API (ADR-018) and M2.6 Follow Me + Talk Mode overlays (ADR-019) implemented offline (`docs/HTTP_API.md`); M2.6 partially tried on the real arm in supervised Studio sessions; real-arm API tests NOT RUN.** Read `IMPLEMENTATION_STATUS.md` first. M2 specs: `planning/so101-milestone2-handoff/`. Never move the arm without the owner's explicit per-session authorization. Do not implement M3 (agent/MCP/speech/game).

Read `AGENTS.md`, then `README.md` and `milestone-1/implementation-plan.md`. Treat these files as the complete context from four requirement interviews. Implement M1 **P0 first**, with no hardware access or serial attempts. Update `IMPLEMENTATION_STATUS.md`. M2 is implemented (ADR-013/014); do not implement M3 — it is an interview plan only.

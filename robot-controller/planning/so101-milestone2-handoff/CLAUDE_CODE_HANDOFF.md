# Message to paste into Claude Code

> We have finalized the **Milestone 2 Behavior Engine** requirements through four interview rounds. Read this package starting with `README.md`, `SOURCE_OF_TRUTH.md`, `DECISIONS.md`, `milestone-2/spec.md`, `architecture.md`, `contracts/*`, `safety-and-faults.md`, the implementation plan, tasks and acceptance matrix.
>
> **Do not implement anything yet unless I separately authorize implementation.** First inspect the actual current SO-101 Companion repository, report its current SHA and any mismatches against the M2 design, especially the fixed M1 direct-target policy, wrist calibration, current rest procedure, and active hardware fault state. Then present your specific change plan and identify any genuinely blocking facts.
>
> When implementation is authorized, extend the existing single-window codebase in phases P0–P6 and follow tasks M2-T001..026. Do not overwrite old M1 hardware fixes or private local configurations. Keep normal tests fully headless, use fake hardware/offscreen sim by default, and produce evidence for Gate A. **Never move physical motors or run commissioning steps without my explicit, per-session approval.** Physical acceptance P7 remains a separate supervised stage.
>
> Implement the agreed M2 behavior recorder, editable local library, linear trajectory player, stall-aware clock, smooth entry/loop transitions, and exact MuJoCo preview with each real-play confirmation. Preserve fixed M1 safety and direct-position policy: no reintroduced 10°/tick or 20° relative clipping, no VR, IK, ground checks, agent, MCP or Valorant code. Keep Emergency Hold / Torque Off and fault latching unchanged.
>
> Any change to the approved requirements must be surfaced as a decision request. Track tests honestly; never infer hardware verification from mocked tests.

## Review checkpoints
- Before coding: repo audit and component mapping.
- After offline implementation: evidence per M2-A01..A24.
- Before any real motion: operator-approved gate and scripted, attended testing.

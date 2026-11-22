# Spec-driven AI coding workflow (agent-neutral)

The approved artifacts are already written. Implement them directly or optionally initialize [GitHub Spec Kit](https://github.github.com/spec-kit/) so that work products map to its `Specify → Plan → Tasks → Implement → Converge` phases. **Never run an automatic spec generator that overwrites interview decisions.**

## Suggested coding cycle

1. **Inspect**: read `AGENTS.md`; capture exact upstream SHAs/APIs and license information to `SOURCE_LOCK.md`.
2. **Specify validation**: compare `milestone-1/spec.md` against user decisions, risk list and test matrix; record any incompatibility as an issue/ADR.
3. **Plan**: follow `milestone-1/implementation-plan.md`, and begin P0 embedded rendering feasibility.
4. **Task-by-task red/green**: implement one checkable item from `milestone-1/tasks.md`; write tests before or alongside code; run targeted test and lint; add evidence to `IMPLEMENTATION_STATUS.md`.
5. **Converge**: cross-check requirements traceability, safety/USB state transitions, UI and offline test results; call Gate A complete only when all acceptance checks pass.
6. **Commission later**: use hardware runbooks and obtain user approval at each step. Never auto-run any physical test due to an agent tool or CI job.

## Spec Kit (OPTIONAL)

Read the current [CLI documentation](https://github.github.com/spec-kit/reference/core.html) and [integration management](https://github.github.com/spec-kit/integrations/cli.html). In a **new empty repo**, `specify init --here --integration claude` or `--integration codex` may be possible, but CLI syntax and safety should be checked against the installed version; instead of overwriting user documents, copy or link them into the generated specs structure. Codex's command names may be skills (`$speckit-specify`) rather than slash commands; prefer versioned docs. Alternatively use **no tooling**: this package's `spec.md`, `architecture.md`, `tasks.md`, test matrix and constitution already implement SDD discipline.

## Requirements traceability

`milestone-1/tests/acceptance-matrix.md` maps each FR/SR/UX criterion to mock/GUI/hardware tests and a readiness gate. A requirement is not complete simply because a UI button exists; verify meaningful state changes and observable behavior with tests.

## Suggested ready-to-paste first message

> Read `AGENTS.md` and all M1 specs. Do not call or interact with physical hardware. Begin with P0: verify the official SO-101 MJCF and its assets render inside a single PySide6 Ubuntu window with six joint sliders. Create a minimal reproducible render test, record source versions and results in `IMPLEMENTATION_STATUS.md`, then stop for review before P1.

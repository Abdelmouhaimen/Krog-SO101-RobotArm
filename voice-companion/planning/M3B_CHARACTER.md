# M3B: AI character, robot tools and expressive behavior control

**Baseline:** `be6bd73` (M3A.1), plus the uncommitted 2-hour session limit.
**Rule:** the LLM decides what to express, the companion coordinates, and the robot API executes.
The robot repository is unchanged.

## Phase 1: what the robot API really does (verified 2026-11-08)

Checked in `docs/HTTP_API.md`, `api/server.py` and `api/service.py`, then probed live against the fake arm.

| Question | Answer | Consequence |
|---|---|---|
| Library | `GET /behaviors` gives `id`, `name`, `duration_s` and `saved_settings{repeat_count (null = until stopped), after.action, trim_*, speed}`. `GET /poses` gives `id`, `name` and `usable_on_connected_arm`. | Kinds come from the saved repetitions, ids from the response. Nothing is hardcoded. |
| Play defaults | `PlayBody.repeat`/`speed` default to `None`, and `_settings()` keeps the **saved** values. | The client sends only explicit overrides; an omitted field means the saved setting. Verified: `wavy` played once, `idle_curious` looped. |
| Completion | `GET /commands/{id}` gives `queued → running → ending → completed`, or `superseded`/`cancelled`/`failed`/`rejected`, with `superseded_by`. | Continuations are driven by the ledger. Measured: `wavy` (2.1 s nominal) completed after 3.2–4.9 s. |
| Supersession | A new play, pose or rest supersedes, with no rest in between. | Reaction → idle is one direct command. |
| Idempotency | The same `Idempotency-Key` gives `200` + `idempotent_replay`. | The tool call id is the key, and timeouts are retried with the same key. |
| Safety | `status.fault`, `emergency_hold_latched`, `ready.<action>`, `robot.driver` (fake: `FakeRobot (offline test double)`). | The companion gates on all of these, and refuses real-arm motion unless `allow_physical`. |
| Library | 15 behaviors exactly as in the owner's table. **No named poses exist.** | The pose tools work but report "none". |
| Fake arm | Reads `artifacts/fake-hardware-behaviors`, and **rejects** the real recordings (calibration `db56…` ≠ fake `c727…`). | The test fixture temporarily copies them into that scratch folder marked simulation-authored (`requested = actual`), then removes them. Real files are untouched. |

## Phase 2–5: modules (voice repository only)

| Module | Purpose |
|---|---|
| `robot/motion.py` | Play, pose and rest over the API, with `Idempotency-Key`, a same-key retry and ledger reconciliation. |
| `character/library.py` | Dynamic library: refresh, name → id, idle/reaction kinds, missing and duplicate reporting. |
| `character/persona.py` | Personality plus a gesture catalog built from the **live** library. |
| `character/director.py` | Priorities, reaction → completion → planned idle, epochs, suspension, automatic phase idles, safety gate. |
| `tools/robot_tools.py` | Six typed tools, with schema enums from the live library. |
| `app.py` | Wiring: conversation phases feed the director, and the persona becomes the LLM instructions. |

## Phase 6–7: validation

See `docs/ARCHITECTURE.md` §11 and `tests/test_character*.py`.

- **Offline:** 158 tests pass.
- **Fake arm on the real API:** 3 tests pass, including the full expressive-conversation demo
  (`scripts/character_demo.py`).
- **Not run:** paid provider sessions and the real arm.

## Live session findings (2026-11-08)

See `docs/ARCHITECTURE.md` §12 for:
- the turn-latency trace;
- the 20 ms buffer driver issue (fixed: 40 ms);
- echo-canceller recovery after an audio-service restart;
- the voice change to `aura-2-aries-en`.

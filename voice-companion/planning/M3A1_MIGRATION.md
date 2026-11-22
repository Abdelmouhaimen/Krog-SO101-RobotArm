# M3A.1 — Migrate from Deepgram Voice Agent to a custom STT → LLM → TTS pipeline

**Rollback checkpoint:** `6c3fd49` (M3A, live-validated Deepgram Voice Agent pipeline).
To roll back: `git checkout 6c3fd49 -- .`, or branch from it.

**Target pipeline:** microphone → Deepgram Flux STT (`/v2/listen`) → OpenAI Responses API (`gpt-6-luna`,
streaming) → Deepgram Aura-2 TTS (WebSocket `/v1/speak`) → speaker.

There is one pipeline only: no backend switch, no fallback, no compatibility shims.

## Module inventory (before → after)

| Module | Voice-Agent-specific? | Action |
|---|---|---|
| `deepgram/protocol.py`, `deepgram/connection.py` | yes (Settings, agent events) | **delete** |
| `session.py` (`VoiceSession`) | yes (agent event loop, agent audio gate) | **delete**; its patterns are reused in `conversation.py` |
| `config.py` `DeepgramConfig` | yes (agent `think`/`speak`/`listen` blocks) | **replace** with `[stt]`, `[llm]`, `[tts]`, `[input]`, `[cost]` |
| `tests/fake_deepgram.py`, `test_session.py`, `live_support.py`, `test_live_deepgram.py`, `test_live_talk_mode.py` | yes | **delete**; new fakes and tests per provider |
| `audio/devices.py` (miniaudio) | no | keep |
| `audio/playback.py` (`PlaybackBuffer`) | no | keep; add timeline markers (segment start, gestures) |
| `motion/speech_motion.py` (Talk Mode) | no (`AgentAudioDone` only in docs) | keep; docstring updated |
| `robot/client.py`, `robot/actions.py` | no | keep |
| `tools/registry.py` | partly (`defer_until_eot`, Deepgram `function_definitions`) | keep; output OpenAI function tools; drop `defer_until_eot` (the LLM now only runs on a confirmed `EndOfTurn`) |
| `tools/builtin.py`, `perception/` | no | keep |
| `ui/tui.py`, `__main__.py`, `app.py` | thin wiring | adapt to the new coordinator; add a cost dashboard |

## New modules

| Module | Responsibility |
|---|---|
| `stt/flux.py` | Flux WebSocket: connect, stream 80 ms chunks, parse `TurnInfo`, close, reconnect, meter audio |
| `audio/gate.py` | Cost-aware mic input: continuous, push-to-talk, or experimental energy VAD, with pre-roll/post-roll |
| `llm/responses.py` | OpenAI Responses API streaming over SSE (httpx): text deltas, function calls, usage, cancel |
| `llm/history.py` | Conversation items, trimming, recording only the text that was actually played after an interruption |
| `tts/aura.py` | Aura-2 WebSocket: `Speak`/`Flush`/`Clear`, sequence tracking, a stale-audio gate, metering |
| `tts/chunker.py` | Sentence/phrase chunking of streamed LLM text |
| `cost.py` | Price table (with sources and verification date), per-provider ledger, budget, persisted cumulative total |
| `choreography/` | Typed speak/behavior/pose action contract, plus a mock scheduler bound to the playback timeline |
| `conversation.py` | **The** coordinator: phases, turn and generation ids, barge-in, latency, budget enforcement |

## Order of work
1. Plan (this file) and provider API verification (`docs/ARCHITECTURE.md` §1).
2. Provider-independent pieces: cost ledger, chunker, playback markers, mic gate, choreography.
3. Flux client, Aura client and Responses client, each with its own fake server and tests.
4. Coordinator, then app/TUI/CLI wiring.
5. Delete the old pipeline, its config and its tests. Update the docs.
6. Lint, format, `mypy --strict`, the full offline suite, and an offscreen smoke test. **No paid calls**:
   live tests exist but are opt-in (`-m live`) and were not run without the owner's authorization.

## Outcome (2026-11-08)

All phases are done. The old pipeline is deleted: `deepgram/`, `session.py`, the `[deepgram]` config,
`defer_until_eot`, the Voice Agent fakes and the live tests.

**Offline validation:**
- `pytest`: 106 passed (stable over repeated runs).
- `ruff check` and `ruff format --check` are clean, and `mypy --strict` passes on all source.
- The fake-arm integration with the real SO-101 service passes.
- The real mic and speaker smoke check (`scripts/audio_check.py --tone`) is OK.

**Not done (needs owner authorization):** a live paid session with Deepgram and OpenAI. `OPENAI_API_KEY`
is not configured yet, so `gpt-6-luna` account access is unverified (`so101-voice --check-model`).

## Addendum: room audio and echo cancellation (same day)

**Requirement:** an open microphone and room speakers, with no headphones.

**Implemented:**
- `audio/aec.py`: an app-owned PipeWire WebRTC echo-cancel process, in monitor topology by default;
- `[audio] mode = aec | raw | half_duplex`, with an explicit `on_failure` policy;
- half-duplex turn blocking;
- the text echo guard as a second layer;
- `--audio-diagnostics`, `scripts/aec_check.py` and `scripts/aec_pipeline_check.py`.

**Measured locally**, with no paid calls (`docs/ARCHITECTURE.md` §10):
- about 25–30 dB less assistant echo and game audio in the STT stream;
- your voice undistorted when the assistant is silent;
- 8–20 dB attenuation during double talk on the laptop speaker;
- Talk Mode timing unchanged.

**Open:**
- a human double-talk test (`scripts/aec_check.py --interactive`);
- live Flux false-interruption counts, which need authorization.

## Owner validation (2026-11-08)

The owner ran the real app live (Deepgram + OpenAI) on room speakers and reported:
- echo cancellation good;
- voice interruption working well;
- Talk Mode working on the real SO-101 gripper (owner-supervised).

Fixed during these sessions:
- empty Flux `Update` events cancelled every reply;
- the model imitated the inline "…" interruption marker;
- the push-to-talk button showed in continuous mode.

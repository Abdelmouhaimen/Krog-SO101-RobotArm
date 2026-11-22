> **Screen observation update:** [SCREEN_OBSERVATION.md](SCREEN_OBSERVATION.md) describes the current
> OFF/ON consumption mode, per-user-turn screenshots and idle vision requests in the same coordinator.
> Keyword retrieval below applies only to manual snapshots while Screen is OFF.

> **Krog / Flux Cliff update, 2026-11-11:** the active TTS implementation is `tts/flux.py`,
> `flux-cliff-en` on `/v2/speak`, raw mono linear16 at 24 kHz. See [SOL_FLUX.md](SOL_FLUX.md)
> for current character, streaming, interruption, billing and two-hour connection renewal details.
> Older dated live measurements below used Aura and do not establish Cliff performance.

> **2026-11-11 response-path update:** the sequential function-call feedback loop described in older
> sections below has been replaced by one streamed structured speech/actions response per user turn.
> See [Single-response speech + actions](../planning/SINGLE_RESPONSE.md) for the current contract,
> fresh state/image preparation, dispatch and validation results. Historical latency tables below
> describe the previous implementation.

# Architecture — SO-101 voice companion (M3A.1)

Status 2026-11-08: the custom pipeline is implemented, tested offline (123 tests), and **tested live by
the owner**: open microphone and room speakers with PipeWire AEC, natural voice interruption, and Talk
Mode on the real SO-101 gripper, in an owner-supervised session. Still unmeasured: quantitative live
latency and false-interruption counts. Physical motion beyond Talk Mode is **not** exposed.

```
microphone ─► PipeWire WebRTC AEC (reference: speaker monitor) ─► MicGate ─► FluxSTT ══ wss /v2/listen ══ Deepgram Flux (turn detection)
   (miniaudio thread)        │ EndOfTurn (once per turn)
                             ▼
                 ConversationCoordinator ── SSE POST /v1/responses ──► OpenAI gpt-6-luna
                  turn & generation ids       text deltas, function calls, usage
                             │ sentences (TextChunker)
                             ▼
                          FluxTTS ══ wss /v2/speak ══ Deepgram Flux Cliff
                             │ raw audio per speech turn
                             ▼
speaker ◄── PlaybackBuffer (markers, real playback state) ──► SpeechMotionCoordinator ─HTTP─► SO-101 /talk/*
                             │
                        CostLedger (STT · LLM · TTS, budget)   ToolRegistry   Choreography (mock)
```

There is exactly one pipeline: no provider switch and no fallback. The Deepgram Voice Agent (M3A)
was removed; see §9.

## 1. Provider APIs, as verified on 2026-11-08 against the official docs

### Deepgram Flux STT

**Docs:** <https://developers.deepgram.com/reference/speech-to-text/listen-flux> and
<https://developers.deepgram.com/docs/flux/quickstart>.

| Topic | Verified | Used |
|---|---|---|
| **Endpoint and auth** | `wss://api.deepgram.com/v2/listen` with `Authorization: Token <key>`. `/v1/listen` does not work for Flux. | `stt/flux.py` |
| **Parameters** | `model=flux-general-en`, `encoding=linear16`, `sample_rate`: 8000, 16000, 24000, 44100 or 48000.<br>`eot_threshold` 0.5–1 (default 0.7). `eager_eot_threshold` 0.3–0.9 (unset means eager mode off). `eot_timeout_ms` 500–60000 (default 5000). `keyterm` is repeatable. | 16 kHz linear16. No eager threshold, so no speculative events. |
| **Chunks** | "80 ms audio chunks strongly recommended": 2560 bytes. | `send_chunk_ms = 80` |
| **Server messages** | `Connected`, `TurnInfo{event: StartOfTurn, Update, EagerEndOfTurn, TurnResumed, EndOfTurn; turn_index, transcript, audio_window_*, end_of_turn_confidence, trigger}`, `ConfigureSuccess/Failure`, `Warning`, `Error` (fatal). | StartOfTurn means barge-in. Update gives the partial transcript. EndOfTurn means exactly one LLM request. |
| **Client messages** | `CloseStream`, `ForceEndTurn`, `Configure`. **There is no KeepAlive**, and an unknown message closes the stream. | `CloseStream` on idle. `ForceEndTurn` at the end of push-to-talk or VAD turns. |
| **Price** | $0.0077/min pay-as-you-go, billed per second. Keyterm prompting is a +$0.0013/min add-on. | Keyterms are off by default. |

### OpenAI Responses API with gpt-6-luna

**Docs:** <https://developers.openai.com/api/docs/models/gpt-6-luna>,
<https://developers.openai.com/api/reference/resources/responses/streaming-events>.

| Topic | Verified | Used |
|---|---|---|
| **Model** | `gpt-6-luna` was released on 2026-09-22. It supports the Responses API, streaming, function calling and prompt caching. Its context is 1.05M tokens. | Default `llm.model`. It is never substituted silently. |
| **Reasoning** | `reasoning.effort` accepts none, low, medium (the default), high, xhigh and max. `temperature` is only allowed with `none`. | `effort: "none"` is sent explicitly. Temperature is unset, and the config rejects it with any other effort. |
| **Request** | `instructions`, `input` items, `tools` (`{"type":"function",name,description,parameters,strict}`), `stream: true`, `store` (default **true**), `max_output_tokens`, `text.verbosity`, `prompt_cache_key`. | `store: false`, `verbosity: low`, `max_output_tokens: 400`, `parallel_tool_calls: false` |
| **Stream** | SSE events: `response.output_text.delta`, `response.output_item.done` (a complete `function_call` with `call_id`), `response.completed` / `incomplete` (with `usage`: cached and cache-write tokens, reasoning tokens), `response.failed`, `error`. | Arguments are used only from `output_item.done`, then validated by pydantic. |
| **Cancel** | Close the HTTP stream. No usage is reported for an aborted stream, but generated tokens are still billed. | The coordinator cancels the task. The ledger estimates usage at about 4 characters per token. |
| **Price** | Per 1M tokens: input $0.10, cached input $0.01, 30-minute cache write $0.125, output $0.50. Reasoning tokens are billed as output. | `[cost.prices]` |

### Deepgram Flux TTS streaming (verified 2026-11-11)

[Official v2 reference](https://developers.deepgram.com/reference/text-to-speech/speak-flux).
The client sends natural text phrases as `Speak`; audio streams immediately when the provider has
enough text. A single final `Flush` closes the reply. `SpeechStarted` and `SpeechMetadata` delimit
binary audio; `Flushed` is not completion. `Interrupt` cancels an active turn. Stale audio remains
blocked until `SpeechInterrupted`, or a fresh connection after timeout. There is no Aura fallback.

The client rotates between replies at 55 minutes, keeps idle sockets alive with WebSocket pings,
and never resends interrupted or disconnected text. Unexpected mid-turn failures cancel the current
reply; the companion stays running and the next reply reconnects. Expressivity is fixed at 0 per
connection; only speed is supported by `Configure`. See [SOL_FLUX.md](SOL_FLUX.md) for limitations.

## 2. Modules

| Module | Responsibility |
|---|---|
| `conversation.py` | **The** coordinator: phases, turn and generation ids, barge-in, speech output policy, latency, budget enforcement. |
| `stt/flux.py` | Flux WebSocket: chunking, metering, TurnInfo parsing, EndOfTurn latency, reconnect with backoff, idle close. |
| `llm/responses.py` | Responses API over SSE (httpx): request builder, events, usage, error classification, `retrieve_model`. |
| `llm/history.py` | Conversation items. After a barge-in it records only the text that was heard. Trimming keeps tool pairs together. |
| `tts/flux.py` | Flux v2 Speak/Flush/Interrupt, speech IDs, metadata, stale-audio gate, renewal and billing. |
| `tts/chunker.py` | Sentence and phrase chunking of streamed text. It handles abbreviations and numbers, and strips markdown. |
| `audio/aec.py` | App-owned PipeWire WebRTC echo canceller (monitor or sink topology), probe, node discovery, lifecycle (§10). |
| `audio/gate.py` | Cost-aware input: continuous, push-to-talk, or energy VAD, with pre-roll and post-roll. |
| `audio/playback.py` | The truth for "assistant audible". It has an underrun hold and timeline markers. |
| `audio/devices.py` | miniaudio microphone and speaker (PipeWire/Pulse/ALSA). Unchanged. |
| `motion/speech_motion.py` | Talk Mode leases, driven by playback. Unchanged logic. |
| `character/` (library, persona, director), `robot/motion.py`, `tools/robot_tools.py` | M3B: dynamic behavior library, character prompt, behavior director (priorities, reaction → planned idle), arm motion over the API, LLM robot tools (§11). |
| `robot/client.py`, `robot/actions.py` | SO-101 HTTP client (`/talk/*` plus reads), and the planned physical-action contract. |
| `tools/registry.py`, `tools/builtin.py` | Typed tools that output OpenAI function tools. The harmless `get_local_time` is the only tool. |
| `cost.py` | Prices with sources, per-provider ledger, budget, persisted cumulative total, sliding windows. |
| `choreography/` | Typed speak/behavior/pose contract, plus a mock scheduler bound to playback markers. |
| `perception/` | Interfaces only (M3A). |
| `app.py`, `ui/tui.py`, `__main__.py` | Composition root, terminal UI with a cost bar, and the CLI. |

## 3. Conversation flow and phases

`IDLE → CONNECTING → LISTENING → USER_SPEAKING → THINKING → SPEAKING → LISTENING …`. A barge-in passes
through `INTERRUPTED`. Fatal problems (bad key, model unavailable, devices) go to `ERROR`.

1. **Flux `StartOfTurn`** sets USER_SPEAKING. If a reply is active, this is a barge-in (§4).
2. **Flux `EndOfTurn`** is deduplicated by (connection, `turn_index`). It records the user message and
   starts generation *n*, then the phase is THINKING.
3. **Speech deltas** from one structured Luna response go through the existing natural phrase chunker
   to Flux `Speak`, preserving separators. Audio can arrive before Luna completes the response.
4. A single final `Flush` ends that speech turn. The client does not use Aura's flush-rate windows.
5. Binary audio is accepted only between `SpeechStarted` and matching `SpeechMetadata`, for the
   current generation. Odd byte boundaries are joined before delivering linear16 samples.
6. Actual playback reports PLAYING, so SPEAKING and Talk Mode start. The provider's events do not
   start the gripper. Local buffering stays 40 ms at 24 kHz.
7. `SpeechMetadata` marks synthesis finished. Local drain and output latency determine when Talk
   Mode stops and the answer is committed; generated audio is not the same as played audio.

**Actions:** one structured response carries speech and optional action instructions. The existing
`ToolRegistry` validates and deduplicates actions; execution runs independently of speech. Results
stay local, with no model continuation. Fresh state and requested images are prepared before the
single request. See [the current response contract](../planning/SINGLE_RESPONSE.md).

## 4. Barge-in and stale-output guarantees

On `StartOfTurn`, a push-to-talk press or a new `EndOfTurn` while generation *n* is active:

1. Generation *n* is invalidated. Every later callback for *n* is dropped by id.
2. `PlaybackBuffer.flush()`: queued audio is gone at once, and pending markers are dropped.
3. `SpeechMotionCoordinator.force_off()`: Talk OFF with no grace.
4. `FluxTTS.cancel(n)` blocks audio immediately, then sends `Interrupt` with a session-wide local
   playback offset where available. No new `Speak` is sent until acknowledgement or socket replacement.
5. The LLM stream and turn-bound tools are cancelled. There is no second model request for feedback.
6. With a valid local offset, `SpeechInterrupted.text_spoken` refines the interrupted history slot.
   Without that alignment (including already-completed synthesis with buffered playback), exact words
   remain unknown; history contains an interruption note rather than claiming the full response was heard.
7. Submitted characters stay metered. Provider input/billable quantities replace the reported portion
   of estimates, and session summaries replace totals rather than adding duplicate charges.

Fake providers inject late audio with distinct PCM values; tests inspect actual local playback.

## 5. Talk Mode (unchanged contract)

The gripper animates only while assistant audio actually plays:
- **Start:** the PLAYING event from the buffer.
- **Leases:** 2 s, renewed every 0.8 s only while audible.
- **Stop:** after the final played sample and the 0.25 s grace.
- **Immediate stop:** barge-in, TTS connection loss, session stop, app exit (best effort), robot lease
  expiry on a crash.
- **Robot API down:** the conversation continues and the status reads "robot animation disconnected".
- **Never:** while the user speaks or the LLM thinks.

## 6. Cost model and budget

**Estimated cost per conversation minute (Flux TTS price checked 2026-11-11):**

| Component | New pipeline | Basis |
|---|---|---|
| STT (Flux, continuous) | $0.0077 | all audio streamed |
| STT (push-to-talk) | ≈ $0.0077 × the fraction of time talking | only utterances ± pre-roll and post-roll |
| LLM (gpt-6-luna) | ≈ $0.0005–0.001 | about 6 turns × (~1–2k input tokens at $0.10/M + ~50 output tokens at $0.50/M) |
| TTS (Flux Cliff) | ≈ $0.014–0.020 | 35–50% speaking at ~15 chars/s; $0.045 per 1k characters |
| **Total, active chat** | **≈ $0.022–0.029/min** | |
| **Total, idle listening** | $0.0077/min continuous; ≈ $0 push-to-talk | |
| Previous Deepgram Voice Agent (Standard) | $0.075/min of connection | billed for all connected time |

Idle listening remains $0.0077/min in continuous mode, or nearly nothing with push-to-talk. TTS is now the dominant cost: shorter answers save the most money, which
is why the prompt asks for 1–3 sentences and `max_output_tokens` is 400.

**Ledger and budget:**
- `CostLedger` meters each provider separately:
  - STT: audio seconds and connection seconds.
  - LLM: input, cached, cache-write, output and reasoning tokens, from provider usage when reported,
    otherwise estimated and flagged.
  - TTS: characters, segments and discarded characters.
- The TUI shows the session cost per provider and the cumulative cost, persisted in
  `logs/cost-ledger.jsonl`.
- Above `warn_at_fraction` it warns. At `max_session_estimated_usd` it cancels the active reply, closes
  the STT stream and starts no new request.
- All figures are labelled as estimates, not invoices.

## 7. Cost-aware input

| Mode | What is sent and billed | Turn end |
|---|---|---|
| `continuous` (default) | everything | Flux's model |
| `push_to_talk` (space toggles) | the utterance + 300 ms pre-roll + 800 ms post-roll | `ForceEndTurn` on release. The stream closes after 4 s idle and reopens on the next press, with audio buffered while it connects. A press also interrupts the assistant at once. |
| `vad` (experimental) | from a local energy onset (with pre-roll) to post-roll | `ForceEndTurn` when the gate closes. Unverified with Flux. It relies on AEC, because echo would otherwise open the gate. |

Muting closes the STT stream, so silence is not billed.

## 8. Choreography foundation (M3A.1, mock only)

`choreography/contracts.py` defines `ChoreographyPlan{actions: [speak | behavior | pose]}`. Gesture
anchors are `after_previous`, `with_next` and `immediate`, and IDs are validated. Raw joint values are
impossible to express. `ChoreographyScheduler` turns a plan into speech segments with marker tags:
- **Firing:** gestures fire when the marker's sample plays, never when their text was generated.
- **Supersession:** a newer gesture supersedes the running one.
- **Cancellation:** cancelling a generation drops its pending gestures and ignores late markers.

Only `MockGestureExecutor` exists. Physical tools stay refused by the registry.

**Next steps (future milestones):**
1. A real `GestureExecutor` over `RobotActions`, using the HTTP API with an idempotency key per gesture.
2. Letting the LLM emit plans through a structured output or tool, with `allow_physical_tools` behind
   an explicit flag plus the robot's `allow_physical_control`.
3. Testing on the fake arm, then in simulation, then supervised on the real arm.

The perception plan from M3A (ManySight, TrackingAdapter, VLM) is unchanged and not started.

## 9. Migration notes (M3A → M3A.1)

- **Rollback checkpoint:** `6c3fd49`. The plan is in `planning/M3A1_MIGRATION.md`.
- **Removed:**
  - `deepgram/protocol.py` and `deepgram/connection.py` (Voice Agent Settings and events);
  - `session.py` (`VoiceSession`);
  - the `[deepgram]` config section (it is now rejected with guidance);
  - `defer_until_eot` in the tool registry;
  - the Voice Agent fakes and live tests.
- **Kept and reused:** miniaudio devices, `PlaybackBuffer` (plus markers), Talk Mode coordinator, robot
  client, tool registry, perception interfaces, the TUI shell, and the fake-arm integration test.
- **Lessons from the M3A live run, now handled locally:**
  - TTS arrives sentence by sentence with 20–40 ms gaps (the underrun hold).
  - The network's "audio done" signal comes 1.6–2.1 s before the last sample plays (Talk Mode follows
    the buffer).

## 10. Room audio: acoustic echo cancellation (open microphone, room speakers)

**Requirement:** no headphones. The companion talks through normal speakers while you play, hears
you, ignores its own voice and the game, and lets you interrupt by voice.

**Design** (`audio/aec.py`):
- **What runs:** for as long as the app runs, it starts its own `pipewire -c <generated config>`
  process. That process loads `libpipewire-module-echo-cancel` with the WebRTC backend:
  `extended_filter`, `delay_agnostic`, high-pass and noise suppression on, AGC off.
- **Nothing is left behind:** the virtual nodes vanish with the process. System default devices and
  volumes are never changed.
- **Topology `monitor` (default):** the reference is the speaker sink's monitor. Everything audible
  from the speakers (TTS, game, notifications) is cancelled. The app plays to the normal speaker and
  reads the "SO-101 Echo-Cancelled Mic".
- **Topology `sink`:** a virtual "SO-101 Echo-Cancel Speaker" carries only the TTS. Game audio is not
  cancelled.
- **Alignment and sample rates:** WebRTC estimates the speaker→mic delay itself (measured 40–60 ms
  here). It runs at 48 kHz mono, and PipeWire converts to the app's 16 kHz capture and 24 kHz playback.
- **Full duplex:** the microphone is never muted while the assistant speaks. Flux `StartOfTurn` still
  drives barge-in.
- **Text echo guard (second layer):** a user turn whose words mostly repeat what the assistant is
  saying, or said within 2 s, neither interrupts nor gets answered.
- **Audio modes:** `aec`, `raw` (diagnostics) and `half_duplex` (fallback: turns are ignored while the
  assistant is audible, plus a 300 ms tail). If AEC fails, the app refuses to start unless
  `on_failure` explicitly selects `raw` or `half_duplex`, which then shows a warning.

### Measured on this laptop

**Setup:**
- Hardware: HP laptop, sof-hda-dsp built-in speaker and digital mic. Speaker volume 0.71, mic gain
  0.61, unchanged.
- Signals: synthetic speech-like sounds.
- Tools: `scripts/aec_check.py` and `scripts/aec_pipeline_check.py`.
- Reports: `logs/aec-report.json` and `logs/aec-pipeline-report*.json`.
- **Levels are speech-frame dBFS.**
- **"Onsets"** are a local energy detector at noise floor + 15 dB. That is a proxy, not Flux.

| Measurement | Raw mic | AEC |
|---|---|---|
| Assistant echo, user silent (monitor) | -57 | **-86** (≈29 dB less) |
| Assistant echo at stream level 0.15 / 0.4 / 0.8 (sink) | -65 / -57 / -51 | -90 / -88 / -88 |
| Game-like audio on the speakers: monitor / sink | -54 / -53 | **-86** / -54 (not in the reference) |
| **Real app path:** the assistant's voice inside the bytes streamed to STT | -60.7 | **-85.2** (5 dB above the AEC noise floor) |
| **Real app path, with game audio:** game alone / game + assistant | -52.9 / -52.7 | **-79.5 / -81.4** |
| Real app path: speech-like onsets while the assistant talks, with game | **13** | **0** |
| User alone, no assistant (distortion check) | -56.4 | -56.7 (−0.3 dB, envelope correlation 0.69) |
| Double talk, simulated user at the echo's level | | user ≈20 dB attenuated, detected 0.6 s later than in raw |
| Double talk, simulated user 6 dB louder | | user ≈8 dB attenuated |
| WebRTC AGC on (rejected) | | +8 dB speech, but residual echo lifted 4–18 dB above the floor, varying by run |
| Talk Mode on the real app path (aec / raw) | ON +10…36 ms after playback starts, OFF +258…267 ms after the last sample, in both modes | |

**Conclusions:**
- **Works:** AEC removes about 25–30 dB of the assistant's voice and (in monitor topology) of game
  audio from what Flux receives. Your voice passes undistorted when the assistant is silent. Talk Mode
  timing is unchanged.
- **Still unproven:**
  - Residual echo bursts sometimes exceed the local proxy threshold, especially in the first seconds
    after start (3–4 per 10 s). Whether Flux would turn them into `StartOfTurn` needs a live test; the
    text echo guard covers the case where it transcribes them as the assistant's words.
  - **Interrupting while it talks is the weak point on a laptop**, where the speaker is centimetres from
    the mic. Your voice is attenuated 8–20 dB during double talk, depending on how much louder than the
    speaker you are.
  - **Improving it:** speak up, lower the speaker volume, or move external speakers away from the mic.
    A USB speakerphone with hardware AEC is the robust option.
- **Not yet tested:** real human speech (run `scripts/aec_check.py --interactive`), and live Flux false
  interruptions (paid; needs authorization).

## 11. Character, robot tools and expressive behaviors (M3B)

**Rule:** the LLM decides what to express, the `BehaviorDirector` coordinates, and the SO-101 API
executes. The robot repository is unchanged: no robot-side idle manager and no new behavior engine.

### Behavior library (dynamic)
- **Source:** `GET /api/v1/behaviors` and `/poses` at start, every `library_refresh_s`, with the TUI
  **R** key, or with the LLM's `list_robot_behaviors(refresh=true)`.
- **Kind:** from the saved repetitions. `repeat_count: null` is a looping **idle**, a finite count is a
  one-shot **reaction**. A behavior recorded later in the Studio appears with its kind and description.
- **Names to ids:** resolved from the live response. Unknown names are refused before any request is
  sent, and missing expected behaviors are reported and never offered (they are dropped from the tool
  enums and the prompt).
- **Saved settings are inherited:** the play request omits `repeat` and `speed` unless the LLM
  overrides them, and the robot then applies the saved values (verified in its code and live).
  Overrides apply to one run only.

### Tools (`tools/robot_tools.py`)

| Tool | Arguments | Effect |
|---|---|---|
| `list_robot_behaviors` | `refresh?: bool` | read |
| `play_robot_behavior` | `behavior` (enum of the live library), `next_idle?` (enum of live idles; reactions only), `speed?` 0.5–2, `repeat?` 1–10 or `until_stopped` | physical, committed turn only |
| `list_robot_poses` | | read |
| `go_to_robot_pose` | `pose` (enum), `transition?` | physical, committed turn only |
| `robot_go_rest` | | physical, committed turn only |
| `get_robot_status` | | read |

**Never exposed:** joints, calibration, torque, serial, recording or editing, Follow Me, Emergency Hold
and Talk Mode.

**Safety of tool calls:**
- **Validation:** pydantic schema, then library resolution. Invalid input sends nothing.
- **Duplicates:** a duplicate is the same call id *with* the same tool and arguments. It is answered
  from cache, and its robot `Idempotency-Key` is `call-<id>-<args hash>`, so the robot replays rather
  than repeats.
- **Interruptions:** a barge-in before the response is committed means the tool never runs. Once the
  robot has accepted a command it is an external fact: it is replaced by a newer command, not
  "cancelled".

### Priorities and the reaction → idle rule (`character/director.py`)
1. **Safety (not a level):** fault, Emergency Hold, readiness, and the real-arm switch
   (`[robot_tools] allow_physical`: true by default, the owner's choice on 2026-11-08;
   `--no-physical-robot` or `false` refuses non-fake drivers).
2. **`COMMAND`:** pose or Go Rest. These pause automatic idles ("resting", "holding pose").
3. **`REACTION`:** a deliberate one-shot. Automatic idles never replace it; for example `wavy` keeps
   playing when speech starts.
4. **`AGENT`:** an idle the LLM chose (a mood) or a reaction's continuation. It yields when the user
   starts the next turn.
5. **`AUTO`:** listening while you talk, talking while it speaks, and a calm idle after speaking
   (`calm_idles` variety, or the current mood). These are debounced (0.4–1.2 s) and rate-limited
   (`min_switch_interval_s`), so short gaps never flap the arm.

**Reaction → planned idle:**
- **Recording:** `play(wavy, next_idle=idle_curious)` records the command id, the planned idle and a
  new **epoch**.
- **Completion:** the monitor polls `GET /commands/{id}`. On the robot's `completed`, never a timer,
  it sends `idle_curious`, but only if:
  - the epoch is unchanged (no newer action);
  - automation is not suspended;
  - the gate passes.
- **Key:** the continuation's key is derived from the completed command (`next-<id>`), so it cannot be
  sent twice.
- **Dropped continuations:** superseded, cancelled or failed reactions drop theirs.
- **Operator takeover:** when the robot ledger shows a Studio command took over, automation pauses
  ("operator").

**Session:**
- **Start:** `wake_up`, then `default_idle`.
- **End and app exit:** Go Rest (`rest_on_stop`), because idles loop forever.

### Talk Mode
It is unchanged and independent. The gripper overlay follows the real audio playback and coexists
with any arm behavior (tested against the real API: Talk `ON` while `idle_curious` runs). The director
never touches the gripper.

### Validated
- **Offline (fake API):** library, kinds, missing and changed behaviors, saved settings, loops,
  completion, continuation, no rest in between, supersession, stale-continuation fencing, Go Rest,
  status-driven completion, priorities versus speech phases, moods, schemas, invalid arguments,
  duplicates and retries, Emergency Hold and faults, operator takeover, the real-arm gate, robot down,
  concurrent requests, poses.
- **Through the voice pipeline:** LLM tool call → reaction → idle with Talk Mode; tool results back to
  the model; barge-in before dispatch; robot down; tools disabled.
- **Real SO-101 API, fake arm:** the director workflow, and `scripts/character_demo.py`, a five-turn
  expressive conversation through the whole app. Robot ledger:
  `wake_up → idle_watching → wavy → idle_curious → comic_rage → idle_sad → high_five_offer → idle_hyped
  → idle_talking → sleepy → idle_watching → go_rest`.

### Real-arm validation still required (supervised; needs the owner's authorization)
1. **The real calibration:** the real recordings are bound to it, and fake-arm runs used
   simulation-marked copies.
2. **Motion quality:** idle-to-idle and reaction-to-idle transitions on hardware, including
   speed/entry presets and serial stalls.
3. **Settings:** `allow_physical` is on by default (owner's choice), and the Studio's own
   `--api-allow-physical-control` is the robot-side gate.
4. **Gestures in live use:** how often the LLM gestures, and whether the chosen gestures fit the
   moment. This needs a paid live session.

## 12. Live findings (2026-11-08, owner session, real arm)

### 12.1 Turn latency trace ("Good night" with a gesture)

The per-turn timeline is logged by `so101_voice.trace` in `logs/so101-voice.log`, in ms since `EndOfTurn`.

| ms | Step |
|---|---|
| −195 | Flux end-of-turn detection (after the user stopped speaking) |
| 0 | `EndOfTurn` received; model request 1 sent at +4 |
| **+2021** | GPT-6 Luna's first output is the **function call only** (no text): `play_robot_behavior(sleepy, next_idle=idle_sad)` |
| +2027 → +2061 | Tool: robot status 14 ms, play accepted 19 ms (`running`); result returned in 34 ms |
| +2068 | **Model request 2** (with the tool result) |
| **+2734** | First text "Good…" (request 2 time to first token: 666 ms) |
| +2764 / +2928 | Aura-2 Speak + Flush / first audio |
| **+2964** | First sample handed to the speaker (+~60–120 ms device latency) |
| +8750 | `sleepy` completes on the robot; `idle_sad` starts by itself |

**Findings:**
- **The tool does not wait for the gesture.** It returns when the robot API accepts the command
  (34 ms); completion is tracked separately by the director.
- **Speech comes after the tool result.** The model's first response held only the function call, so a
  second request was needed for the words. That costs about 0.9 s per gesture turn.
- **The biggest part is the first response, ~2.0 s.** It took 1.99 s on a turn without any tool call
  too. Prompt caching worked (4,100 of 4,260 input tokens cached). Before M3B, first words came after
  0.65–1.0 s. The likely cause is the larger persona plus 6 tool definitions, but that is **not yet
  verified**.
- **Possible improvements (not implemented):**
  - speak first and gesture in the same response, then skip the second request for gesture-only calls;
  - test a short persona, and gestures on vs off, against first-response latency.

### 12.2 Sound driver: "Broken pipe" loop caused by 20 ms device buffers

- **Symptom:** the greeting started, then the speaker stopped being serviced, and the mic went deaf.
- **System log:** PipeWire kept logging `spa.alsa: hw:sofhdadspp: snd_pcm_avail after recover: Broken
  pipe` (hundreds per few seconds) from the moment the app opened its streams.
- **Measured, real devices with fake providers:**

| Device period | Complete runs | Sound-card errors |
|---|---|---|
| 20 ms (old default) | 1 / 4 | 4 |
| 40 ms (new default) | 10 / 10 | 0 |
| 60 ms | 4 / 4 | 0 |

- **Fix:** the defaults are now `input_period_ms = output_period_ms = 40` (and `MiniaudioIO`'s own
  default), measured again in the owner's live session with 0 errors.
- **Earlier wrong guesses:** a stuck card, and the countdown timer. Both were caused by this, not the
  cause of it.

### 12.3 The audio service restarted under the app
Restarting PipeWire removes the app's echo-cancel nodes, and the next Start failed with "no input
device matches 'SO-101 Echo-Cancelled Mic'". Before every Start, the app now checks for its nodes and
recreates the canceller if they are gone (`Companion.prepare_audio`, tested).

### 12.4 Voice
Historical voice choice before the Krog/Flux migration: `aura-2-aries-en`. All Aura-2 voices have the same price;
`[cost.prices] tts_model` must name the configured voice.

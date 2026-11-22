# Single-response speech + actions

Implemented 2026-11-11. No commits, paid provider calls or physical robot movements were performed
by this implementation/testing work. The running application was not restarted; reload it when ready
to test the new conversation flow.

## One request per confirmed user turn

The conversation no longer runs a sequential function-calling loop. It prepares fresh local context,
then makes one streaming GPT-6 Luna Responses request with strict `text.format` JSON schema. Model,
reasoning settings, voice, STT, AEC, playback buffering, Talk Mode, cost accounting/session limits,
Follow Me semantics, BehaviorDirector, robot recordings/API and LAN receiver are unchanged.

The response contract is:

```json
{
  "speech": "Ha! There you are!",
  "actions": [
    {"tool": "set_follow_me", "arguments_json": "{\"enabled\":true}"},
    {"tool": "play_robot_behavior", "arguments_json": "{\"behavior\":\"idle_curious\"}"}
  ]
}
```

`speech` is first in the schema so its text can stream immediately. `actions` contains at most eight
instructions. `arguments_json` is a JSON-encoded argument object: this keeps the outer strict schema
small while the existing ToolRegistry remains the single authority for each tool's argument schema,
validation, timeouts, idempotency and physical safety rules. The allowed mutation/action names are
derived from the current registry. There are no native `tools`, `function_call_output` messages,
model continuations, repair requests, result-reporting requests or automatic model retries.
`max_tool_rounds` is removed because there are no tool rounds.

Official sources verified before implementation:

- [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna): streaming, images and
  structured outputs supported.
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses):
  strict JSON schemas, schema key ordering and streamed structured data.
- [Responses image input/output shapes](https://developers.openai.com/api/reference/resources/responses/methods/create).

The existing SSE client is retained. `pydantic-core` (already installed through Pydantic) incrementally
parses partial JSON strings, including split escapes and Unicode; only the growing `speech` value
reaches the original sentence chunker/TTS pipeline. JSON keys, action names and argument payloads do
not enter speech. A full strict Pydantic validation is required before any action dispatch.

## Actions and interruption

Actions are scheduled independently of speech after the completed response validates. They do not
block TTS or wait for playback to drain. Independent action lanes run concurrently; all arm commands
share an ordered lane, all Follow Me requests share an ordered lane. Go Rest and named poses act as
ordering barriers, preserving their existing safety/release semantics. Reactions with `next_idle`
still use the unchanged BehaviorDirector continuation logic rather than separate timed actions.

Duplicate tool/argument instructions are canonicalized and deduplicated within a response. Stable
per-generation call IDs also preserve ToolRegistry deduplication and robot idempotency. Each queued
action checks the current turn token before dispatch. Beginning a new spoken or push-to-talk turn
invalidates queued older actions even when the previous speech has already finished. Existing
committed-turn cancellation still protects in-flight physical requests. Stop cancels background
work and follows the existing shutdown path. A request already accepted by the robot service remains
subject to its normal command/behavior semantics; cancellation cannot undo a movement already made.

Action results stay local: ToolActivity events, diagnostics and `last_action_error` report failures.
No detailed action result is sent to the model or permanently added to history. Spoken history still
contains only actual speech, with the existing interruption handling. The prompt explicitly forbids
claiming requested physical actions have already succeeded. Structured schema validation cannot
prove the truth of arbitrary natural-language claims; current state and the explicit prompt provide
that grounding, while failures remain visible locally.

## Fresh state and images

Before each request, `WorldContext` concurrently reads actual Studio status/Follow Me state and LAN
perception status. Its compact snapshot contains current behavior name/kind/status, actual overlay
state, connection/fault state, screen/face availability/age/bearing, relevant local authorization and
calibration flags, and current local time. A running reaction is reported as that reaction, not its
planned next idle. Completed motions are not described as currently playing. Unavailable APIs and
unknown overlay state are represented explicitly, without substituting the last requested action.

State is a transient developer input for this turn, not appended to persistent conversation history.
The existing prompt still supplies the discovered behavior/pose catalog. Read-only tools remain
registered for local/debug use, but the model's post-response action catalog contains only mutations;
reads needed to form an answer must happen before inference. Time/date information is in the snapshot.
Additional future data dependencies would need a local preparation adapter, not another model round.

For explicit screen/game visual questions, local preparation retrieves the current JPEG before the
single request and includes its actual base64 `input_image`. No screenshots are sent on a timer or on
ordinary conversation turns. Missing/expired frames yield `screen_available=false`, `image_included=false`
and no image. Frames and snapshots do not persist in history. The English topic/visual-intent matcher
covers explicit screen, monitor, screenshot, game/gameplay, Valorant, aim and crosshair questions;
ambiguous references or other languages may need clearer wording. This is request routing, not game
recognition or an animation mapping.

## Streaming limitations

- Speech can start before the complete JSON response arrives; actions wait for full validation. A
  model that spends a long time generating its speech can therefore delay action dispatch. Robot
  execution never delays speech.
- A truncated, invalid, refused or interrupted response executes no newly parsed actions. Some
  speech may already have been heard before final validation fails. There is no paid repair retry.
- One request removes the tool feedback round trips, not the model's own inference/network latency.
  Existing output-token limits still apply to the entire structured response, including actions.
- Luna cannot inspect results of newly requested actions in the same answer. It sees actual state
  on the next user turn. Actions requiring runtime result-dependent branching are not supported by
  this contract; the existing arm/follow order and Go Rest/pose barriers cover current motion tools.

## Changed files

Production:

- `llm/reply.py`: strict contract, dynamic schema/instructions and streamed speech extraction.
- `turn_context.py`: fresh read-only state and on-demand first-request screenshot preparation.
- `conversation.py`: single request, independent action dispatch/order/deduplication, local errors,
  and stale-action cancellation; existing speech/AEC/TTS/playback components are reused.
- `llm/responses.py`: optional structured `text.format` in request construction.
- `tools/registry.py`: action catalog derived from existing mutation tools.
- `app.py`: context composition and injectable fake transports; fresh context replaces stale readiness text.
- `character/persona.py`, `tools/perception_tools.py`: protocol/state guidance, same character behavior meanings.
- `config.py`, `config/default.toml`: remove obsolete `max_tool_rounds`.
- `__main__.py`: `--print-request` shows the new response format without making a request.

Tests/helpers: `tests/test_single_response.py`, `tests/test_character_voice.py`,
`tests/test_conversation.py`, `tests/test_perception_tools.py`, `tests/fake_providers.py`,
`tests/fakes.py`, `tests/rig.py`, and the existing fake conversation script `scripts/character_demo.py`.
Tests of the old feedback loop now assert the new single-response behavior instead. Existing low-level
Responses client tests still cover native function-call event decoding; the conversation does not use it.

## Validation

Run from `voice-companion`:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
SO101_REPO=../robot-controller \
  .venv/bin/python -m pytest -q -s -m robot_fake
```

Final results: **200 offline tests passed, 5 deselected** (43.76 s); **4 real-API/fake-arm
integration tests passed** (77.85 s). Ruff, strict mypy (47 source files), and whitespace checks pass.
The strengthened post-speech interruption regression also passes.

The real API integrations use explicit fake-hardware/fake-arm flags, temporary test credentials and
scratch behavior copies, never the physical arm. Studio source and real recordings are untouched.
The tests cover simple/follow/behavior/multiple actions with one model request, actual JPEG bytes in
the first request, no result feedback/history pollution, fresh reaction/idle state, failures, duplicated
instructions, concurrent independent actions, pending ordered-action cancellation after speech ends,
Talk Mode and reaction-to-idle continuity, and the existing voice/AEC/budget regression suite.

`SINGLE_RESPONSE_MEASUREMENTS.json` records four controlled local-fake scenarios (20 ms initial and
30 ms subsequent synthetic model delays). These are not real Luna, Ethernet, acoustic or servo timings.
`first_audio_ms` is the fake playback start point. `decision_to_dispatch_ms` is measured from completed
response validation to calling the registry, including event-loop scheduling/dependency waits. Existing
trace logs include the same per-turn speech/audio and action timing markers for subsequent supervised
real use. No paid latency experiment was made.


Latest controlled mock timing summary (all four cases used exactly one Luna request per user turn):

| Case | First speech text | Fake playback start | Decision → dispatch |
| --- | ---: | ---: | ---: |
| Conversation | 24 ms | 36 ms | No actions |
| Follow Me + speech | 26 ms | 36 ms | 4.20 ms |
| Behavior + speech | 26 ms | 37 ms | 5.40 ms |
| Multiple actions + speech | 33 ms | 37 ms | 5.71 / 5.85 ms |

These values use synthetic streamed model output and a fake audio device; they do not predict actual
GPT-6 Luna response time or real speaker latency. The measured structural improvement is one model
request for each tested user turn and no wait for action execution before speech can proceed.

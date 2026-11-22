# Krog character and Flux Cliff voice (2026-11-11)

> Renamed 2026-11-12: the character was called "Sol" until then; it is now **Krog**. File names keep the old name.

Implementation is offline validated, pending the owner's listening test. No paid calls, physical
robot movements or commits are part of this change. The existing robot API, recordings, calibration,
face-angle mathematics, screen transport, Flux STT and Luna model remain in use.

## Character and sources

Krog is a tiny cyber dragon in a desk-clamped SO-101 arm. He considers himself a battle-hardened dragon
and legendary tactical coach, knows he is small, and acts larger. Proud, mischievous, observant and
competitive, he has dry humor, loyalty and genuine warmth. He can be skeptical, shy, sleepy, sad,
affectionate or quietly focused. He drops jokes during serious conversations, owns mistakes, and
can hang out without turning everything into Valorant coaching. Usually one or two spoken sentences;
longer explanations when useful. No forced catchphrase, question ending, service formula, action
narration, Markdown or emotion tags. Examples guide tone rather than trigger scripted replies.

Character-affecting sources changed:

| Source | Effect |
|---|---|
| `character/persona.py` | Central identity, relationship, delivery, truthful observations, body vocabulary, optional examples and fallback persona. |
| `tools/robot_tools.py` | Meanings of expressive animation, saved static pose and actual rest; argument validation unchanged. |
| `tools/perception_tools.py` | Natural focus choices, screen truthfulness, independent Follow Me tool description. |
| `llm/reply.py` | Retains truthfulness without encouraging repetitive “I'll try”; one response with speech/actions. |
| `config.py`, `config/default.toml` | Fixed configurable greeting; prompt-source diagnostics; Flux Cliff defaults. |
| `app.py` | Assembles the effective prompt, reports override source, adds truthful no-perception context. |
| `character/director.py` | Existing chosen-mood timeout now also protects against speaking/listening automatic replacements. No director redesign. |
| `llm/history.py`, `conversation.py` | Interrupted speech is reconciled from local playback alignment instead of assuming generated text was heard. |

Reviewed without changing their behavior: dynamic library discovery, current robot/perception snapshots,
Follow Me controller, single-response validation/deduplication/cancellation, speech-driven Talk Mode,
AEC and miniaudio. Existing default wake-up and reaction-to-idle continuation stay in place.

## Acting vocabulary

Names and recorded settings are unchanged. Only available API-discovered recordings are offered.

| Recorded name | Meaning |
|---|---|
| `idle_watching` | Calm vigilance, studying something, quietly present. |
| `idle_curious` | Investigative, alert, interested, slightly suspicious. |
| `idle_talking` | Lively conversational body language. |
| `idle_listening` | Attentive, listening closely, taking someone seriously. |
| `idle_hyped` | Proud excitement, contained anticipation, celebratory energy. |
| `idle_sad` | Disappointed, deflated, sympathetic or quietly reflective. |
| `wake_up` | Waking with personality, stretching into awareness. |
| `curious_peak` | Sudden curiosity, surprise, inspecting something unexpected. |
| `thinking` | Deliberate contemplation or comic deep thought; long, not for every question. |
| `acknowledge` | Small nod-like acknowledgment, recognition or agreement. |
| `sleepy` | Drowsiness, reluctant bedtime, winding down. |
| `wavy` | Friendly, playful greeting or farewell. |
| `high_five_offer` | Proud celebration or inviting an air high-five; no physical contact. |
| `shy_reaction` | Embarrassment, being flattered or briefly losing composure. |
| `comic_rage` | Exaggerated, playful indignation, never hostility. |

Idles are moving animations. All six can be chosen independently of Follow Me. Reactions keep the
existing `next_idle` mechanism; examples include high-five to either hype or curiosity, not a required
pair. A bedtime reaction should wind down. Small mood shifts can be frequent; dramatic reactions
should be earned. Do not move just to fill silence or force a gesture per reply. Intentional moods
remain through ordinary speech phases for the existing `mood_ttl_s` (120 seconds by default), while
reaction priorities, operator takeover, faults, Go Rest and shutdown still take precedence.

## Tool descriptions and perception

- `play_robot_behavior`: express a reaction or mood with an available recording. Idles loop; a reaction
  finishes before its selected next idle. Choose by intention and emotion, not every sentence.
- `set_follow_me`: turn toward and track the human, or focus elsewhere such as the game. Independent
  of all animations; existing readiness, calibration and physical authorization still apply.
- `go_to_robot_pose`: an intentional saved static pose, not a replacement for moving idles.
- `robot_go_rest`: actual rest when intentionally ending activity or explicitly requested, not a mood.

A supplied screenshot is Krog's game view. Without one he must not invent what he sees. User-reported
events are legitimate conversation evidence, distinguished from witnessed events. Facing the user
or game should be a natural choice with the existing Follow Me tool, without explaining camera
mechanisms or asking permission for ordinary focus changes. No SCREEN/USER states or idle mappings.
A missing arm, empty library, disabled actions or missing perception does not erase Krog's character.

## Effective configuration

Precedence stays `config/default.toml` → `config/local.toml` → optional `--config FILE`. At inspection,
local configuration contained no persona, greeting, audio, cost or TTS override. Its existing LAN,
calibration and physical-authorization settings are preserved. Default source is the built-in Krog
persona and discovered library. An intentional `llm.prompt` still replaces that persona; startup logs
its source path and a warning, without logging the prompt contents. Programmatic overrides are named
as such. Thus an override cannot silently conceal the built-in character change.

Fixed greeting: **“Hmph. There you are. I was getting bored.”** It uses normal TTS, playback and Talk
Mode, with no Luna request. `llm.greeting` remains configurable. Voice is `flux-cliff-en` on
`wss://api.deepgram.com/v2/speak`, raw mono linear16 at 24 kHz. Speaker prebuffer and device period
are 40 ms. STT stays `flux-general-en`; LLM stays `gpt-6-luna`; session limit stays 120 minutes.
Old Aura models/endpoints/control settings now fail configuration validation instead of silently
running an unused alternate pipeline. Production `tts/aura.py` was removed.

## Verified Flux TTS v2 contract

Official documentation, read 2026-11-11:

- [WebSocket v2 reference](https://developers.deepgram.com/reference/text-to-speech/speak-flux)
- [Getting started](https://developers.deepgram.com/docs/flux-tts/quickstart)
- [Client messages](https://developers.deepgram.com/docs/flux-tts/client-messages)
- [Server messages and errors](https://developers.deepgram.com/docs/flux-tts/server-messages)
- [Speech lifecycle](https://developers.deepgram.com/docs/flux-tts/state)
- [Interrupt alignment](https://developers.deepgram.com/docs/flux-tts/interrupt-handling)
- [Voice catalog](https://developers.deepgram.com/docs/flux-tts/voices)
- [Expressivity](https://developers.deepgram.com/docs/tts-expressivity)
- [Pricing](https://deepgram.com/pricing)

The voice catalog identifies Cliff as a mature American male voice, deep, calm and raspy. Connection
uses Token authentication and explicit model, encoding, sample rate and expressivity query parameters.
`Connected` identifies the request. `SpeechStarted` gives a speech ID; raw binary audio follows. The
client sends natural phrases with separators (the server adds none). Flux generates before Flush;
one final `Flush` ends the whole assistant reply. `Flushed` is only an acknowledgement. Matching
`SpeechMetadata` means no more audio for that turn. Partial PCM sample boundaries are reassembled.
Warnings are surfaced; fatal errors abort the current response, not the whole voice session.

Aura's Clear/Cleared controls, early-Flush policy and Flush/character rate windows are removed.
Expressivity is a connection-level integer from -2 to +2, default 0; it cannot be set by `Configure`.
The supported mid-session control is speed. Per-turn expressivity would need a different connection
strategy and listening validation; it is not added. No emotion-classifier or unsupported tags.

## Interruption, local playback and renewal

Barge-in instantly flushes local playback and forces Talk Mode off, cancels the Luna stream and
pending actions, then sends Flux `Interrupt` for an active synthesis turn. Its optional time offset
comes from cumulative LOCAL played PCM, subtracting estimated output latency; it is session-wide,
not reset each turn. Offsets must advance. Non-advancing offsets replace the connection rather than
inventing progress. Audio arriving before `SpeechInterrupted` stays blocked. Missing/ignored
acknowledgements cause socket replacement after `interrupt_timeout_s` (1 second by default).

Only a response to an Interrupt carrying that local offset can refine history from `text_spoken`.
That reconciliation is awaited before constructing the next single Luna request, not sent as
same-turn action feedback. Generated audio and provider duration are never treated as proof that
samples were heard. When synthesis already finished but playback was still buffered, there is no
active synthesis turn to align; the client replaces that socket and records an interruption note
with unknown exact heard words instead of copying the whole response into history. This is
conservative and may omit some actually heard words. Local playback remains the authority for
Talk Mode start/stop; AEC, miniaudio and SpeechMotionCoordinator logic are unchanged.

The provider's session maximum is one hour; idle limit is 60 seconds. WebSocket pings every 20 seconds
keep idle connections alive. Before a new reply, sockets aged 55 minutes are closed and renewed,
with fresh session accounting and no replay. A two-hour companion session can span multiple sockets.
An unexpected mid-turn loss aborts that reply and the next reply reconnects; it never regenerates or
replays the old speech automatically. Prosody context naturally resets on a new connection.

## Costs

Published pay-as-you-go TTS changed from Aura-2 **$0.030** to Flux TTS **$0.045 per 1,000 characters**
(+50%). The **$0.25 session cap is unchanged**. STT and LLM rates/models are unchanged. Every submitted
character, including inserted phrase spaces and text cancelled before playback, remains counted.
A send with uncertain delivery is conservatively counted. Speech/interruption metadata supplies
provider input and billable counts; repeated reports and final session totals replace counts rather
than double charge. Unreported text stays estimated. This is a local list-price estimate, not a
provider invoice; zero discarded playback does not imply free synthesis.

Illustrative active chat at 35–50% speaking, ~15 characters/second: TTS $0.014–0.020/min, total about
$0.022–0.029/min with continuous STT. Actual usage varies. Continuous idle listening remains
$0.0077/min; a two-hour time limit does not override the budget. UI model label reads `flux-cliff-en`.

## Offline evaluation and acceptance

`tests/fixtures/sol_character.json` contains 20 repeatable situations: greeting, casual chat, long
absence, look-at-me, game focus, personal re-engagement, reported ace, lost clutch, poor aim, tactics,
teasing, compliment, correction, sleepiness, good night, serious conversation, missing screenshot,
unavailable arm, long silence and interruption. Mock speech is synthetic test input, not target
catchphrases in the production prompt. Human review should assess consistency, varied delivery,
earned gestures, quiet moods, truthfulness, warmth and lack of tool narration or service language.
Mocks establish plumbing/constraints only; they cannot prove Luna's actual personality quality.

Technical checks include v2 query/control messages, pre-Flush streaming, speech IDs and completion,
odd-byte PCM, local-offset interrupts, stale audio, missing acknowledgements, disconnect recovery,
two-hour-session renewal with an advanced TTS clock, usage reconciliation, fixed greeting without
Luna, configuration precedence, dynamic library and fallbacks, chosen-mood retention, Follow Me
independence, action deduplication/cancellation, fresh snapshots, one response per turn, playback,
Talk Mode and existing AEC tests. See the final implementation report for counts.

## Launch and listen

Quit any old companion instance, then run:

```bash
cd voice-companion
./start_companion.sh
```

Press **s / Start**. The app is not auto-launched by this implementation. This normal launch uses the
owner's existing physical authorization; listening and robot testing are the owner's next step.
To explicitly disable arm behaviors and Follow Me movement for a voice trial:

```bash
./start_companion.sh --no-physical-robot
```

That existing flag does not disable speech-driven gripper Talk Mode; `--no-robot` disables robot use.
Do not run live provider tests without separate authorization. Offline commands:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/mypy
SO101_REPO=../robot-controller .venv/bin/python -m pytest -m robot_fake
```

Remaining listening questions: account access to Cliff, actual latency/prosody, intelligibility,
barge-in alignment versus measured device latency, voice continuity after renewal, and whether Krog's
humor/emotion balance stays enjoyable over long sessions. No live result is claimed for these.

## Validation results (offline, 2026-11-11)

- Full default suite: **237 passed, 5 deselected** (live/provider and separately selected fake-API tests).
- Real Studio API with fake hardware: **4 passed**, using `-m robot_fake`; no physical arm commands.
- Focused final protocol/conversation checks: **72 passed** before the final full-suite rerun.
- `ruff check src tests scripts`: passed.
- `mypy`: passed, strict, 47 source modules.
- Formatting: all **27 changed Python files** checked passed. Repository-wide formatting still flags
  ten pre-existing files outside this overhaul; these were left alone to avoid unrelated cosmetic
  changes: `scripts/perception_check.py`, `perception/follow.py`, `perception/lan.py`, `readiness.py`,
  `robot/follow.py`, `tools/registry.py`, `turn_context.py`, `ui/tui.py`, `tests/fakes.py`,
  `tests/test_lan_so101_fake.py` (source paths are under `src/so101_voice/` unless prefixed otherwise).
- `git diff --check`: passed. Offline `--print-request` confirms Krog and the unchanged Luna model.
- No paid calls, physical robot movements, application launch or commit. Listening remains pending.

## Interruption wire-type correction (2026-11-11)

The owner's listening run exposed `MESSAGE-0000` immediately after each Interrupt. The first
implementation serialized `playback_offset.value` as a JSON float, including whole-valued `40.0`.
Deepgram's [official SDK wire type](https://github.com/deepgram/deepgram-go-sdk/blob/main/pkg/api/speak/v2/websocket/interfaces/types.go)
defines that field as an integer. The client now sends nonnegative integer milliseconds, truncating
after output-latency subtraction and before the advancing-offset check. The fake v2 server now rejects
float offsets with `MESSAGE-0000`, and regression tests assert the actual JSON value type and
fractional-millisecond truncation. The stricter fake reproduced the failure before the fix; no paid
provider call was used to validate the correction. Live confirmation still needs an app restart.

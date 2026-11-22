# Voice companion: Krog's voice and brain (`so101-voice`)

Krog is a proud, mischievous, loyal tiny cyber dragon in a desk-clamped robot arm: a gaming teammate,
rival and friend, with dry humor and room for quiet warmth. The pipeline is:

**microphone → Deepgram Flux (speech-to-text) → OpenAI `gpt-6-luna` (streaming LLM) → Deepgram Flux TTS Cliff
(`flux-cliff-en`, text-to-speech) → speaker**

While the assistant's voice is actually coming out of the speaker, the SO-101's gripper animates
through the robot's existing **Talk Mode** HTTP API.

- **Low latency:** the first sentence is spoken while the LLM is still writing the rest.
- **Barge-in:** interrupt at any time. Playback, synthesis, generation and Talk Mode stop at once, and
  stale audio can never resume.
- **Cost control:** a live cost bar shows STT, LLM and TTS estimates, with a hard session budget
  (default $0.25). Push-to-talk input bills only what you say.
- **Robot repo stays in charge:** this repository never imports robot code, opens a serial port or
  sends motor commands. It uses the existing Talk Mode and validated behavior/pose/Follow Me APIs. Physical authorization remains enforced.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers:
- the verified provider APIs;
- barge-in guarantees;
- the cost model;
- the choreography foundation;
- migration notes. The previous Deepgram Voice Agent pipeline was removed in M3A.1; its rollback
  checkpoint is `6c3fd49`.

See [Krog character and Flux Cliff migration](docs/SOL_FLUX.md) for the exact acting vocabulary,
prompt sources/overrides, v2 interruption and renewal behavior, costs, offline evaluation and launch.
TTS costs $0.045 per 1,000 submitted characters (provider billable counts override estimates),
while the session cap remains $0.25. No live listening results are claimed for the new voice.

## Setup (Ubuntu 24.04)

```bash
uv venv --python /usr/bin/python3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

**Credentials** go in `.env`, which is git-ignored, or in the environment:

```
DEEPGRAM_API_KEY=...   # Flux STT + Flux TTS
OPENAI_API_KEY=...     # gpt-6-luna
SO101_API_TOKEN=...    # only for robot animation (see below)
```

**Check your OpenAI access:** run `.venv/bin/so101-voice --check-model`. It asks OpenAI whether
`gpt-6-luna` is available to your key. If it is not, nothing else is used silently: change `llm.model`
**and** `[cost.prices]` yourself.

**Audio: open microphone and room speakers, no headphones needed.** The app starts PipeWire's WebRTC
echo canceller for as long as it runs (`[audio] mode = "aec"`). It never changes your default
devices. In the default `monitor` topology, everything playing on the speakers is removed from what
speech recognition hears: the assistant's voice, game audio and system sounds. So you can play with
the companion talking out loud and still interrupt it by voice.

```bash
.venv/bin/so101-voice --audio-diagnostics     # silent: are the PipeWire AEC pieces there, do the nodes come up?
python scripts/aec_check.py                   # plays test sounds: raw vs echo-cancelled mic, echo reduction, levels
python scripts/aec_check.py --interactive     # also records YOU talking over the assistant (listen to logs/aec/)
python scripts/aec_pipeline_check.py --game   # the real app audio path with game audio, local fake providers
```

**Audio modes** (`[audio] mode`, or `--audio-mode`):
- `aec` (default): full duplex.
- `raw`: diagnostics; the assistant can hear itself.
- `half_duplex`: fallback; your turns are ignored while it speaks.

**If AEC cannot start**, the app refuses to run (`[audio.aec] on_failure = "error"`) unless you
explicitly choose `raw` or `half_duplex`, and then it shows a warning. There is no silent downgrade.
`miniaudio` talks to PipeWire/PulseAudio or ALSA, so no PortAudio is needed.

## Run

```bash
.venv/bin/so101-voice --list-devices                  # find microphone / speaker names
.venv/bin/so101-voice                                 # terminal UI
.venv/bin/so101-voice --no-robot                      # voice only
.venv/bin/so101-voice --input-mode push_to_talk       # space = talk / release (cheapest)
.venv/bin/so101-voice --plain --duration 120          # console mode, prints events and costs
.venv/bin/so101-voice --print-request                 # the exact LLM request body (no network)
.venv/bin/so101-voice --cost-report                   # cumulative estimated cost across sessions
python scripts/audio_check.py --tone                  # mic level + a quiet beep (no network)
```

**TUI keys:**
- `s` Start
- `x` Stop
- `m` Mute (also closes the STT stream, so silence is not billed)
- `space` Talk (push-to-talk mode)
- `q` Quit
- `e` or the **+2h** button: extend the session by 2 hours
- `r` refresh the robot behavior library
- `v` toggle Screen ON/OFF (also a header button; default OFF)

**Session limit:** every session stops by itself after **2 hours**, so a forgotten one never keeps
listening and billing. The countdown is shown right of Quit, and a warning appears 5 minutes before
the end. Configure it in `[session]` (`max_minutes`, `extend_minutes`, `warn_minutes`).

**Status bar:**
- the phase: LISTENING, USER_SPEAKING, THINKING, SPEAKING, INTERRUPTED or ERROR;
- the mic;
- the robot link, including **"robot: DISCONNECTED"** (the voice keeps working);
- whether the gripper is talking;
- reply latency;
- the models.

The second line shows the estimated session cost per provider, the cap, and the all-session total.

### With the SO-101 (fake arm first)

```bash
# In the SO-101 repo (robot-controller):
.venv/bin/python -m so101_companion --create-api-token voice-companion   # → SO101_API_TOKEN in .env
.venv/bin/python -m so101_companion --headless --api --fake-hardware --fake-arm
# Here:
.venv/bin/so101-voice
```

Animating the **physical** gripper needs the robot service started with physical control enabled and
the owner's authorization for that session.

## The character (M3B): expressive SO-101 behaviors

Krog is a playful, supportive, slightly mischievous gaming teammate. It now expresses itself with your
recorded SO-101 animations, through the robot's existing API.

- **Reactions:** the LLM picks a reaction when it fits (e.g. `wavy` for hello, `comic_rage` for a lost
  round, `high_five_offer` for an ace). It also picks the looping idle that follows. When the robot
  reports the reaction `completed`, that idle starts directly, with no return to rest.
- **Automatic idles:** listening, talking and calm idles follow the conversation. They never interrupt
  a reaction. Talk Mode still moves the gripper with the actual audio.
- **Live library:** the behavior list comes from the robot (refreshed every 60 s, or with **R**).
  Behaviors you record later in the Studio are picked up; missing ones are never offered.
- **Safety:**
  - Faults and Emergency Hold always win.
  - After Go Rest or an operator takeover, nothing restarts by itself.
  - **The real arm moves by default** (`[robot_tools] allow_physical = true`, the owner's choice). The
    robot service must also allow physical control, which the Studio controls. Use `--no-physical-robot`
    for a session with voice and the gripper only.
- **Turning it off:** `[robot_tools] enabled = false` gives voice only, with Talk Mode still on.

```bash
SO101_REPO=../robot-controller .venv/bin/python scripts/character_demo.py   # 5-turn demo, real API, FAKE arm
```

## Configuration

**Load order:**
1. `config/default.toml`, which documents every key;
2. `config/local.toml`, git-ignored;
3. `--config FILE`.

**Obsolete settings:** an old `[deepgram]` section from the Voice Agent version is rejected with a
pointer to the new sections.

| Section | Key settings |
|---|---|
| `[stt]` | `model = "flux-general-en"`, `sample_rate = 16000`, `send_chunk_ms = 80`, optional `eot_threshold` / `eot_timeout_ms`. `keyterms` is a paid add-on. |
| `[llm]` | `model = "gpt-6-luna"`, `reasoning_effort = "none"`, `verbosity = "low"`, `max_output_tokens = 400`, `greeting`, `prompt`. |
| `[tts]` | `model = "flux-cliff-en"`, `url = "wss://api.deepgram.com/v2/speak"`, `sample_rate = 24000`, `expressivity = 0`, `max_connection_s = 3300`. |
| `[input]` | `mode = "continuous" \| "push_to_talk" \| "vad"` (experimental), `preroll_ms`, `postroll_ms`, `idle_close_s`. |
| `[audio]` | `mode = "aec" \| "raw" \| "half_duplex"`, `prebuffer_ms`, `output_latency_ms`. `input_device` / `output_device` apply only to raw and half_duplex. |
| `[audio.aec]` | `topology = "monitor"` (cancels everything on the speakers) or `"sink"` (assistant only), `mic`, `speaker`, `noise_suppression = true`, `gain_control = false`, `on_failure = "error"`. |
| `[robot]` | `enabled`, `base_url`, `talk_intensity`, `lease_s = 2.0`, `renew_interval_s = 0.8`, `stop_grace_s = 0.25`. |
| `[character]` | `name`, `default_idle`, `calm_idles`, `listening_idle`, `talking_idle`, `session_start_reaction`, `reactive_gestures`, `automatic_idles`, `rest_on_stop`. |
| `[robot_tools]` | `enabled`, `allow_physical = true` (real-arm behaviors; `--no-physical-robot` refuses them), `library_refresh_s`. |
| `[session]` | `max_minutes = 120` (automatic stop), `extend_minutes = 120`, `warn_minutes = 5`. |
| `[cost]` | `max_session_estimated_usd = 0.25`, `warn_at_fraction = 0.80`, `ledger_file`. `[cost.prices]` holds the list prices with their sources and verification date, and must match the configured models. |

**Typical cost:** about $0.022–0.029 per minute of active conversation, versus $0.075/min for the old managed
Voice Agent. Idle listening costs $0.0077/min, or ~$0 with push-to-talk. TTS characters dominate, so
short answers are cheap answers. All figures are estimates from list prices, not invoices.
An intentional `llm.prompt` overrides the built-in persona; its source is logged at startup with a warning.
Old Aura endpoints, voices and Flush/Clear configuration keys must be migrated; they fail validation.

## Tests

```bash
.venv/bin/python -m pytest                     # offline suite: fake Flux, fake Flux TTS, fake OpenAI, fake audio, fake SO-101
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts
.venv/bin/mypy                                 # strict
SO101_REPO=../robot-controller .venv/bin/python -m pytest -m robot_fake   # real SO-101 service, FAKE arm
.venv/bin/python -m pytest -m live -s          # PAID live run (Deepgram + OpenAI): only with explicit authorization
```

The offline suite never reaches a paid provider: `tests/conftest.py` blocks every non-loopback
connection and removes API keys from the environment.

**Coverage includes:**
- STT, LLM and TTS protocols;
- exactly one LLM request per `EndOfTurn`;
- context across turns;
- speech starting before the LLM finishes;
- Talk Mode following real playback;
- barge-in with stale audio injected by the fake TTS after `Interrupt`;
- rapid interruptions;
- provider failures and disconnects;
- the robot API down;
- cost metering and budget caps;
- the Flush rate limit;
- tools;
- choreography timing and cancellation;
- the repository-separation and no-credential checks;
- an offscreen TUI run.

LAN webcam/screen perception tools, independent Follow Me and startup checks: see
[the M3C setup, calibration and validation handoff](planning/M3C_LAN_PERCEPTION.md).
Physical following defaults off until targeting is checked and separately authorized.

Use `./start_companion.sh` to launch with normal terminal colors when starting from an agent shell.
The window waits for **Start** before opening paid voice services. Readiness updates show actual
STT/TTS connections and whether an OpenAI reply has been received.

The conversation now uses [one streamed speech/actions response per user turn](planning/SINGLE_RESPONSE.md).
Actions execute through the existing registry without a model feedback loop. Current state and any
explicitly requested screen image are supplied before the single model request.


## Screen observation

The header distinguishes **Screen feed** availability from the **Screen: OFF / ON / UNAVAILABLE**
AI-consumption toggle. Screen ON supplies a fresh image to every conversational Luna request and,
after Start, analyzes changed fresh images about every three seconds while idle. Krog decides whether
to comment or stay silent based on the screen, context and what he has recently said. Screen OFF stops periodic requests, while explicit manual
snapshots still work. Follow Me and animations are independent. The $0.25 cap still applies;
watching can add up to 1,200 model requests/hour. See [configuration, costs and mock UI demo](docs/SCREEN_OBSERVATION.md).

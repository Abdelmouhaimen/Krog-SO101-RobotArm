# Screen ON/OFF and continuous observation

The header has a compact **Screen: OFF / ON / UNAVAILABLE** button, also toggled with **V**.
The readiness row separately reports **Screen feed: Available / Unavailable**. The button controls
AI image consumption, not the Windows sender. Default is OFF. It is independent of Follow Me,
animations, poses and robot authorization; all four Screen/Follow combinations remain possible.

With Screen ON, every user-driven Luna request receives the newest valid JPEG in that initial
request, without requiring any special words. A user turn still has exactly one streamed speech/action
response. Images are transient inputs, never accumulated in conversation history. A missing,
malformed or stale frame produces an ordinary voice request without an image and shows UNAVAILABLE.
Freshness is checked again at the actual request boundary, including time spent preparing robot state.

With Screen OFF, ordinary conversation has no automatic images or periodic model requests. An explicit
manual request, such as “take a screenshot” or “look at my screen,” still attaches a one-off image to
the first request; it does not permanently enable watching. The manual-only detector is not used to
decide image retrieval while Screen is ON.

## Periodic watching

After Start, Screen ON enables an approximately three-second idle watcher within the existing
ConversationCoordinator. It does not start another agent or inference framework. It waits while the
user is speaking, responding, playing speech or dispatching user actions. User speech cancels an
in-flight observation and takes priority; the model-stream lock prevents overlapping Luna requests.
The timer is deferred after user activity, visual requests and completed replies. Missed ticks do not
queue. Identical JPEG content is not reanalyzed in the background, even if sender sequence numbers
change. User-driven requests with Screen ON still receive the image even when it is unchanged.
Unavailable images never start a background model request.

The background request includes the current Krog persona, relevant conversation history, actual world
state and latest image. Its structured reply retains `speech` and `actions`, plus an internal
`observation` field for a brief meaningful visual note. Krog chooses whether a short comment, coaching, reaction or question adds value, or whether to stay silent.
There is no mandatory silence or compulsory commentary. Actions remain optional. This is one
response, with no tool-result feedback or second inference. Empty speech sends no Speak/Flush to
TTS and does not start Talk Mode. A meaningful reaction can use normal streamed speech and validated
actions; there are no fixed animation choices for screen states.

Only one latest meaningful visual note is kept in RAM as dated historical context, bounded to 400
characters. Empty/repeated notes do not grow history; raw images and repetitive synthetic user turns
are never stored there. Meaningful spoken reactions enter normal history. Previous visual notes do
not prove the current screen or unseen events between samples. Menus/tutorials must be described as
such, and an isolated frame is not proof of an ace, clutch or an entire match outcome.

Turning OFF cancels the watcher and any current background generation, including queued actions from
that generation. It does not interrupt an ordinary user response already in progress. Stop/Quit ends
the watcher and clears its visual memory. Turning ON before Start selects the mode but makes no model
requests; voice sessions still require Start. Default OFF is restored on a fresh app launch unless
explicitly configured otherwise.

## Configuration and costs

```toml
[screen]
enabled = false
interval_s = 3.0
detail = "low"                # low | auto | high
max_age_s = 5.0
estimated_image_tokens = 2000
```

`detail = "low"` is the default cost-conscious policy. Use `high` or `auto` if small HUD text needs
more detail. The receiver/client's existing 4 MB JPEG bound remains; this change does not resize or
reconfigure Windows captures. The foreground/background request includes only one image, freshly
validated with receiver age, transport elapsed time, JPEG markers and metadata validation.

Three seconds permits **up to 1,200 additional model requests/hour**, before user conversation. Real
provider-reported input tokens include vision usage and are metered in the existing ledger. Each
background request logs usage/status in the existing diagnostic log. Silent output still costs input
and output tokens. Screenshots are not assumed free to repeat. The unchanged **$0.25 session budget**
and **two-hour session limit** also govern the watcher. The cap can stop observation long before two
hours, and an already in-flight request can report usage that crosses the threshold.

If a request is aborted or usage is absent, the ledger estimates textual input separately from images:
it excludes base64 bytes from text-token estimates and adds configurable `estimated_image_tokens`
per image. The 2,000-token fallback is a conservative local assumption, not a guaranteed Luna image
charge or provider invoice. Provider usage wins when available. No new model or pricing is introduced.

## Validation and demonstration

Offline tests cover initial-request image attachment without keywords, OFF/manual behavior, idle
timing, silence/no TTS/no Talk Mode, meaningful visual memory, unchanged-frame skipping, user priority,
non-overlapping streams, timer deferral, invalid/stale/missing images, final-boundary expiry, OFF
cancellation, budget/session limits and aborted-image cost estimates. Existing voice, AEC, Follow Me,
BehaviorDirector and single-response tests remain in the full suite.

Run the repeatable mocked UI demo:

```bash
.venv/bin/python scripts/screen_demo.py
```

It uses fake voice providers, a fake LAN receiver, fake audio and a fake arm. It produces:

- [Mock demonstration report](artifacts/screen-observation/report.json)
- [Screen ON UI](artifacts/screen-observation/screen-on.svg)
- [Screen OFF UI](artifacts/screen-observation/screen-off.svg)
- [Screen UNAVAILABLE UI](artifacts/screen-observation/screen-unavailable.svg)

The demo's accelerated interval is 0.08 seconds; the production default is 3 seconds. Its
observations are deterministic mock outputs, not a claim of live visual understanding. The demo
verifies no request before Start, one silent background request, then image counts **1 / 0 / 1** for
background / ordinary Screen OFF conversation / ordinary Screen ON conversation, no image history,
and no change to Follow Me from toggling.

The real startup check is read-only and recorded separately in
[the startup report](artifacts/screen-observation/startup.json). Launch normally:

```bash
./start_companion.sh
```

The app opens idle. Start connects paid voice providers; enabling Screen in an active voice session
also enables billable periodic vision requests. No live model request or robot movement is part of
this implementation's verification. Nothing is committed until the owner tests the interaction.

## Results, 2026-11-11

- Full offline suite: **254 passed, 5 deselected**.
- Real Studio API / fake-arm LAN integration: **1 passed**.
- Lint and strict mypy: passed; formatting of all 11 Python files changed for this feature passed.
- `git diff --check`: passed.
- Mock UI demo: passed, with image counts **[1, 0, 1]**, one silent background request,
  zero paid requests, zero physical movements and no image history.
- Real application launched in a desktop terminal and visually inspected in **IDLE**, **Screen OFF**;
  no Start press or paid provider connection. [Startup window](artifacts/screen-observation/startup.png).
- Read-only startup: **Arm Connected**, **Screen feed Available**, **Face angle Unavailable**,
  receiver authenticated/reachable and a fresh JPEG available. No robot commands were sent.
- No commit. Live model vision quality and reaction timing remain for the owner's actual test.

## Speech choice and shorter interval, 2026-11-12

The default idle delay is now **3 seconds**, in both the model defaults and default TOML; the local
configuration has no interval override. As before, timing defers around user speech, model requests
and playback, with no overlapping or queued observations. This is not a promise to speak every three
seconds. Krog now explicitly chooses whether a brief comment, coaching, playful reaction, relevant
question or silence adds value. He need not wait for a dramatic event or direct user question.
The prior instruction to usually stay silent is removed. Recent conversation and visual evidence
inform that decision; repetition, unsupported claims and commentary on every sample remain discouraged.
No additional decision model, tool or feedback request is added.

Validation: **256 offline tests passed, 5 deselected**. Speaking and silent observation paths, the
three-second timer boundary and the mock UI demonstration passed; lint, strict typing, changed-file
formatting and whitespace checks passed. Up to 1,200 additional requests/hour are possible before
latency/defer/deduplication; the $0.25 cap is unchanged. No paid calls, physical movements, restart
of the owner's running app or commit. Restart the companion to load the updated settings and prompt.

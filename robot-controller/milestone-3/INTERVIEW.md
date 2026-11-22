# Milestone 3 — Valorant companion AI + tool/MCP interview plan (NOT yet answered)

**Not authorized for implementation yet.** Interview the user only after Milestone 2 designs/replay interface are established. The AI should choose named validated behaviors and speech, not send arbitrary motor targets. Any live game and speech tool integrations are future design decisions.

## Context for new assistant

The goal: an embodied companion watching live Valorant gameplay, talking, celebrating or reacting emotionally through the physical SO-101. M1 is a position-based LeRobot/MuJoCo foundation; M2 adds recorded gestures. M3 feeds live game context to an agent, exposes a small high-level set of behavior tools (possibly MCP), produces spoken lines and synchronizes talking-gripper animation. Later, monitor-to-robot 3D registration will enable screen pointing, but is NOT M3 by default. Avoid unnecessary VR and inverse kinematics.

## Round 1 — Personality and perceptual inputs

1. Which gaming capture source? Entire screen, game capture, window stream, periodic screenshots, or OBS virtual feed? Single local desktop or split machines?
2. Does the companion need **continuous video**, screenshots sampled at intervals, or event detections + occasional images? Target acceptable latency/bandwidth/cost?
3. Should it listen to player microphone/team voice/game audio? User privacy/consent implications and what is permissible to stream to an AI service?
4. Persona: enthusiastic, snarky, comforting, coaching, restrained? User-set profanity/tone and frequency?
5. Voice output: TTS provider, local vs cloud, voice style, playback hardware/speaker placement, push-to-talk/interrupt?
6. Which game reactions matter first: round won/lost, clutch, ace, death, spike planted, quiet filler, spectator conversations?
7. How often may it talk or move? Cooldowns, during combat silence, mic mute, manual quiet mode?
8. Must all inference stay local or can game video/audio go to hosted service? Desired retention, cost and data handling?

**Decision gate:** perception path and minimum useful emotional response set. Do not implement full computer vision if event input is enough.

## Round 2 — Agent autonomy / behavior tool contract

9. Which agent runtime/model/tool calling framework? Real-time voice mode, textual agent with separate TTS, local model, or undecided?
10. MCP server or direct in-process function calls? Is any network boundary/multi-process architecture actually needed?
11. Tool list: `play_behavior(name, intensity)`, `stop_behavior`, `speak(text, emotion)`, `set_mute`, `get_robot_status`; exact contracts/argument bounds.
12. Which physical actions require human approval vs automatic reaction? Should AI ever call Torque Off or Emergency Hold (likely no)?
13. Priority/queue for overlapping requests: interrupt previous gesture, enqueue, merge gripper talking with arm gesture, ignore if busy?
14. How to reconcile TTS timing/visemes with gripper motion? Energy envelope, audio amplitude, phonemes or simple timed open/close?
15. What if visual interpretation is wrong/low confidence? Explain uncertainty, stay idle, or ask player? Avoid fabricated game events.
16. Can agent choose behavior intensity/speed? What hard caps and cooldowns keep physical robot safe?
17. How to handle forbidden/rejected tool calls and offline robot? Speak-only or simulation-only fallback, user status indicator?
18. Logging/observability: event trigger, AI decision, tool call, safety-limited execution, latency and user feedback.

**Decision gate:** narrow behavior-level permission boundary, no raw joint targets or arbitrary Python from agent.

## Round 3 — Deployment and acceptance

19. One Ubuntu desktop or separate PC for game/capture and robot? Network latency requirements and security.
20. Voice mixing/mic feedback/OBS audio: how prevent TTS hearing itself and triggering loop?
21. User controls: mute, pause reactions, disable movement, kill hardware commands, rate/volume sliders, dashboard needed?
22. Safety around robot near screen/player and when unattended? Is autonomy permitted without operator at desk?
23. Test suite: prerecorded game clips and event fixtures, simulated behaviors, fake TTS, mock agent tools, then live opt-in gaming.
24. Target end-to-end reaction latency and acceptable hallucinations/false positive rate; objective test scenarios?
25. Privacy/data retention/log deletion and cloud credentials storage. Who may access game stream? Do not embed tokens in files.
26. Cost/compute budgets, offline fallback, rate limits and crash restart policy; never auto reconnect motors and resume gestures.
27. Future features NOT in this milestone: screen 3D localization, pointing and direct spatial interaction; decide separately.

## M3 after-interview package

Create `m3/spec.md`, `m3/agent-boundary.md`, `m3/perception-pipeline.md`, `m3/speech-and-gripper-sync.md`, `m3/tool-schemas/`, `m3/safety-and-permissions.md`, `m3/latency-budget.md`, `m3/tasks.md`, `m3/acceptance-tests.md`. Inspect current APIs and service capabilities when that future implementation is authorized; do not choose specific providers now.

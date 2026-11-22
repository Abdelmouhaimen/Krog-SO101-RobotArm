# Risks, blockers and mitigations

| ID | Risk | Likelihood / impact | Plan | Blocker? |
|---|---|---|---|---|
| R01 | User has no physical SO-101 now | Certain / only Gate B | Build mock-backed Gate A; prepare progressive user-run scripts | **Hardware Gate B only** |
| R02 | Single-window offscreen MuJoCo render fails under Ubuntu GL environment | Medium / high | P0 embed-and-render spike, test X11/Wayland/EGL/OSMesa and documented fallback INSIDE window | **Gate A until solved** |
| R03 | Current LeRobot API differs from source `main` | Medium / high | Pin known working version; inspect `SOFollower`, calibration, dictionary clip and connect behaviors before coding | Gate A adapter |
| R04 | Source's 5° raw calibration margin mismatches normalized coordinates | Medium / high | Pure conversion tests; no raw formula blind copy; require validated hardware before physical commands | Gate B |
| R05 | No calibration file now | Certain or unknown / high | Sim uses model bounds only, hardware remains locked; verify existing path when available | Gate B |
| R06 | MuJoCo gripper 0–100 mapping may be visually inverted | Medium / medium | Explicit provisional mapping, test endpoints with rendered poses, user visual validation | Gate A visual; Gate B realism |
| R07 | `connect` disables torque; disconnect may disable it too | Known / high | Confirmation, support arm, don't promise read-only hardware connect or torque preservation | Gate B |
| R08 | UI sliders run faster than serial writes; 20 Hz not achieved | Medium / medium | Single controller scheduler, monotonic timestamps, coalescing, diagnostics with actual command/read rates; no catch-up burst | Gate A mocks, Gate B actual timing |
| R09 | Fault while moving; no measured position available for hold | Medium / high | Latch fault; stop progression; never claim hold succeeded; expose unknown torque state | Gate A mock, Gate B real |
| R10 | Real Stop requested without physical rest pose | Certain until hardware / medium | Do not expose normal Stop-to-Rest until pose recorded. Use Pause/Hold for offline testing. | Not M1 Gate A |
| R11 | Unknown model assets license/revision | Medium / medium | Track exact commit SHA and upstream license, retain attribution; don't bundle original upstream files in spec package | Gate A distribution |
| R12 | Hidden collision/ground features introduced by copied VR paths | Low / high scope creep | No IK/FK ground checking; inspect diff and test excluded features | Gate A |
| R13 | User wants only code now; coding agent may gain robot access | Known / high | No hardware interactions until explicit user confirmation, even if USB present | Gate B |

## Open facts requiring no new user interview

- Exact Python / MuJoCo / PySide6 / LeRobot pins and dependencies: implementing agent resolves from working compatibility evidence (record chosen versions and commands).
- Which `/dev/serial/by-id/*` matches actual servo bus: later commissioning script discovers and user selects; never hard-code `/dev/ttyUSB0`.
- User-specific calibration file path, motor orientation, rest pose: later when hardware is present.
- Actual UI graphics backend on Ubuntu host: P0 test.

## Scope blockers deliberately NOT created

No need for VR, IK, floor checks, behavior engine, policies, MCP, gaze/screen calibration, physics-accurate controller or real arm to complete Gate A.

## Status after M1 implementation (2026-11-06)

| ID | Status | Evidence / remaining action |
|---|---|---|
| R01 | Open (Gate B only) | All hardware code + commissioning scripts ready; nothing run on hardware |
| R02 | **Mitigated** | Embedded EGL/GLFW rendering proven on the target Wayland desktop (P0); final build verified offscreen |
| R03 | **Mitigated** | LeRobot pinned to 0.4.4 (VR-Control's runtime); API diffed vs 0.7.0; 14 contract tests run the real `SOFollower` code; version allow-list refuses others |
| R04 | **Mitigated in software** | Margin formula proven equal to LeRobot DEGREES normalization (ADR-006); calibration register check fails closed; physical check B04 |
| R05 | Open (Gate B) | No calibration file on this machine; hardware locked without one (tested) |
| R06 | Partly mitigated | Official model: min = closed (offscreen render); conflicting VR config hint documented; c04 records the physical direction |
| R07 | Mitigated by UX | Connect/disconnect/quit warnings; torque tri-state; no blind torque writes after faults (ADR-007 §6); physical behaviour B01/B07 |
| R08 | Mitigated (offline) | Single scheduler, coalescing, skipped deadlines counted, achieved Hz displayed; real-bus rate measured by c06 |
| R09 | Mitigated (offline) | Fault latch, HOLD_FAILED, torque UNKNOWN; physical B06/B07 |
| R10 | Closed for M1 | No Stop-to-Rest exists; c07 only records a candidate pose (T-039 later) |
| R11 | Closed | Model Apache-2.0, pinned, hash-verified, not redistributed; VR MIT notice included |
| R12 | Closed | Scope-exclusion tests (A18) |
| R13 | Mitigated | Device guard until confirmed Connect; scripts require TTY + `--operator-present` + typed words |
| R14 (new) | Open | LeRobot `Robot.__del__` may disconnect (and disable torque) at interpreter exit if a session ends without an explicit disconnect; UI/scripts always ask for an explicit disconnect first |
| R15 (new) | Open, observed | Goal_Position registers were found up to 130° away from Present after torque-off sessions (2026-11-07). No jump was observed on connect (`configure()` torque re-enable), and "Enable torque" sets goal := present first. Not proven safe for every firmware: consider syncing goals before connect in M2 |
| R16 (new) | Low | GUI-thread rendering costs ~12–21 ms/frame at 640×480; controller and bus run on their own thread (A16 verified with a 1 s stall) |

## Milestone 2 additions (2026-11-07)

| ID | Status | Evidence / remaining action |
|---|---|---|
| R15 | Open, mitigated for M2 playback | The combined Play confirmation runs goal := fresh present position before torque-on (M2-A16). Physical confirmation in M2-B04 |
| R17 (new) | Accepted | Real playback runs longer than nominal when the bus stalls (stall-aware clock, D1). The delay is reported (`timeline_delay_ms`) and never compensated |
| R18 (new) | Open (Gate B) | Takes played at 2× can request more speed than the servos and the 5 V supply can deliver. The divergence warning is diagnostic only (ADR-010); the operator interrupts |
| R19 (new) | Accepted | The return to rest after a behavior may end in `REST NOT REACHED` (elbow 3.4° vs ±3° in M1). The arm holds; reported, not retried |
| R20 (new) | Open (Gate B) | The ADR-009 wrist runaway has no proven root cause. M2 playback is attended only; the power cut-off must be within reach |

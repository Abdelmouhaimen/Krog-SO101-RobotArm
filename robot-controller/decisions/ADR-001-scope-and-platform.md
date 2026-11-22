# ADR-001 — Scope and execution platform

**Status:** Accepted 2026-11-06 by user interviews.

Decision: Ubuntu 24.04, single follower SO-101, clean independent Python project, agent-neutral coding, official unchanged MuJoCo SO-101 model and **one integrated** desktop window. No VR, IK, ground protection or behavior/game/agent features in M1. Prior Windows choice superseded. Hardware currently unavailable; implement code plus offline tests only. The project can later be run by a local agent with USB access, but it must never exploit that capability without user confirmation.

Reason: Small, safe, testable foundation supporting later recorded gestures. Alternative of forking full VR application rejected by user. Alternative two-window viewer rejected by user. Physically precise actuator dynamics deferred.

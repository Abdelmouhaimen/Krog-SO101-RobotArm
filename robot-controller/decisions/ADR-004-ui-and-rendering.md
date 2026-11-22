# ADR-004 — Integrated UI render strategy, conditional on P0 proof

**Status:** Single-window requirement accepted; renderer implementation provisional until P0.

Use Qt/PySide6 main window with MuJoCo 3D rendered inside. One potential approach is MuJoCo `Renderer` offscreen RGB rendered to Qt image/QLabel, on the same context-owning thread; serial IO on worker. Must prove actual Ubuntu 24.04 desktop GL configuration and performance before further UI implementation. A native detached MuJoCo viewer is prohibited. Official source MJCF unchanged; nominal model mapping and calibrated gripper direction need visual verification, no mandatory alignment wizard.

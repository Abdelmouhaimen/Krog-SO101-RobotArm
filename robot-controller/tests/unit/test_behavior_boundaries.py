"""M2 architectural invariants (ADR-013): the Behavior Engine core is UI-independent and hardware-free,
and offers no raw joint-command path (future M3 callers refer to behavior IDs only)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from so101_companion.behaviors import service

PKG = Path(__file__).resolve().parents[2] / "src" / "so101_companion" / "behaviors"
FORBIDDEN_IMPORTS = (
    "PySide6",
    "mujoco",
    "lerobot",
    "serial",
    "so101_companion.ui",
    "so101_companion.devices.lerobot",
)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_behavior_core_has_no_ui_or_hardware_imports() -> None:
    for path in sorted(PKG.glob("*.py")):
        bad = [m for m in _imports(path) if m.startswith(FORBIDDEN_IMPORTS)]
        assert bad == [], f"{path.name} imports {bad}"


def test_service_never_sends_raw_joint_targets() -> None:
    source = inspect.getsource(service)
    assert "set_requested" not in source and ".send(" not in source
    public = {n for n in dir(service.BehaviorService) if not n.startswith("_")}
    assert public == {
        "create_keyframe_behavior", "delete", "duplicate", "hardware_context", "list_behaviors", "load",
        "pause", "play", "prepare_preview", "rename", "resume", "save_recording", "set_keyframes",
        "set_settings", "start_recording", "state", "stop", "stop_recording", "take_recording",
        # ADR-017: named poses + endings — by library id / snapshot only, never raw joint values
        "delete_pose", "list_poses", "load_pose", "move_to_pose", "rename_pose", "resolve_end_pose",
        "save_current_pose", "set_after",
        # ADR-019: derived Follow Me directions + the one play-request builder (no raw joint targets)
        "behavior_direction", "play_request", "pose_direction",
    }  # fmt: skip


def test_nothing_from_milestone_3_is_implemented() -> None:
    text = " ".join(p.read_text().lower() for p in PKG.glob("*.py"))
    for word in ("mcp", "valorant", "openai", "anthropic", "speech", "tts"):
        assert f"import {word}" not in text and f"{word}_" not in text

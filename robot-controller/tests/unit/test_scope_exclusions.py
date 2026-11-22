"""A18 / T-017 / FR-18 + M2-REQ-17/18: no VR, IK, ground protection, audio/speech, agent/MCP/game.

The M2 Behavior Engine (recording / playback) is in scope since the owner approved it (ADR-013);
its former exclusion entry was removed deliberately. M3 items (agent, MCP, speech, game) stay excluded.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODE = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))

EXCLUDED = {
    "VR / teleoperation": r"\b(vr_\w*|webxr|meta_quest|websockets?|teleop\w*)\b",
    "inverse kinematics / Cartesian": (
        r"\b(inverse_kinematics|solve_ik|ik_solver|IKSolver|pybullet|cartesian)"
    ),
    "ground / floor protection": (
        r"\b(ground_(?:check|protection|height)|floor_(?:height|check)|collision_check)"
    ),
    "audio / speech": r"\b(pyaudio|sounddevice|speech|tts|microphone)\b",
    "agent / MCP / game": r"\b(mcp|valorant|openai|anthropic|llm)\b",
    # Stop-to-Rest is now implemented (T-039, ADR-011); VR-Control's hard-coded "initial position" is not.
    "invented home pose": r"\b(initial_position|initial_left_arm|home_pose)\b",
    "physics torque model": r"\bmj_step\b",
}


def _docstring_lines(text: str) -> set[int]:
    import ast

    lines: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


# Owner decision D7 (Milestone 2.6, ADR-019): an authenticated WebSocket carries Follow Me ANGLE updates
# only. This is not VR/teleoperation: just the word "websocket" is allowed, and only in these two files
# (every other VR/teleop term stays banned everywhere; test_websocket_is_only_the_follow_target_stream
# pins the single route).
WEBSOCKET_ALLOWED = {"src/so101_companion/api/server.py", "src/so101_companion/api/runner.py"}
VR_WITHOUT_WEBSOCKET = r"\b(vr_\w*|webxr|meta_quest|teleop\w*)\b"


@pytest.mark.parametrize("label", sorted(EXCLUDED))
def test_excluded_feature_absent_from_code(label: str) -> None:
    pattern = re.compile(EXCLUDED[label], re.IGNORECASE)
    hits = []
    for path in CODE:
        if label == "VR / teleoperation" and str(path.relative_to(ROOT)) in WEBSOCKET_ALLOWED:
            pattern_for_file = re.compile(VR_WITHOUT_WEBSOCKET, re.IGNORECASE)
        else:
            pattern_for_file = pattern
        text = path.read_text()
        doc_lines = _docstring_lines(text)
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if i in doc_lines or stripped.startswith("#") or stripped.startswith(('"', "'")):
                continue  # documentation and user-facing text may NAME the exclusion
            if pattern_for_file.search(line.split("#", 1)[0]):
                hits.append(f"{path.relative_to(ROOT)}:{i}: {stripped}")
    assert hits == [], "\n".join(hits)


def test_runtime_dependencies_are_minimal() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = {re.split(r"[<>=\[ ]", d)[0].lower() for d in project["dependencies"]}
    assert names == {"mujoco", "pyside6-essentials", "numpy"}
    hardware = project["optional-dependencies"]["hardware"]
    assert hardware == ["lerobot[feetech]==0.4.4"]


def test_websocket_is_only_the_follow_target_stream() -> None:
    """D7: exactly one WebSocket route exists, the authenticated Follow Me angle stream."""
    from fastapi.routing import APIWebSocketRoute

    from so101_companion.api.auth import TokenVerifier
    from so101_companion.api.server import create_app

    app = create_app(object(), TokenVerifier(hashes=["sha256:" + "0" * 64]))  # type: ignore[arg-type]
    ws_routes = [r.path for r in app.routes if isinstance(r, APIWebSocketRoute)]
    assert ws_routes == ["/api/v1/follow/targets"]

import ast
import asyncio
from pathlib import Path

import httpx
import pytest

from so101_voice.perception.interfaces import (
    BoundingBox,
    CameraSnapshot,
    Detection,
    MockPerceptionProvider,
    NullPerceptionProvider,
    Observation,
    VLMConfig,
)
from so101_voice.robot.client import (
    RobotAPIClient,
    RobotAuthError,
    RobotRejected,
    RobotUnavailable,
)

from .fakes import TOKEN, FakeSO101API

SRC = Path(__file__).resolve().parents[1] / "src" / "so101_voice"


# ------------------------------------------------------------------ robot client
async def test_client_sends_bearer_token_and_maps_errors():
    api = FakeSO101API()
    c = api.client()
    r = await c.talk_start(lease_s=1.5, intensity="gentle")
    assert r.status == 202 and api.calls[-1] == (
        "/api/v1/talk/start",
        {"intensity": "gentle", "lease_s": 1.5},
    )
    assert (await c.talk_status())["state"] == "ON"
    await c.talk_stop()
    assert (await c.talk_status())["state"] == "OFF"
    api.reject_reason = "torque is OFF"
    with pytest.raises(RobotRejected, match="torque is OFF"):
        await c.talk_start(lease_s=1)
    api.down = True
    with pytest.raises(RobotUnavailable):
        await c.talk_stop()
    bad = RobotAPIClient("http://so101.test", "wrong", transport=FakeSO101API().transport())
    with pytest.raises(RobotAuthError):
        await bad.talk_status()
    with pytest.raises(ValueError):
        await c.talk_start(lease_s=0)
    with pytest.raises(ValueError):
        RobotAPIClient("http://x", None)
    await c.aclose()
    await bad.aclose()


async def test_client_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9")
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.host)
        return httpx.Response(200, json={"state": "OFF"})

    c = RobotAPIClient("http://127.0.0.1:8765", TOKEN, transport=httpx.MockTransport(handler))
    assert c._http._trust_env is False
    await c.talk_status()
    assert seen == ["127.0.0.1"]
    await c.aclose()


def test_client_exposes_no_motion_endpoints():
    names = {n for n in dir(RobotAPIClient) if not n.startswith("_")}
    assert names.isdisjoint({"play_behavior", "go_pose", "go_rest", "rest", "set_follow", "emergency_hold"})


# ------------------------------------------------------------------ perception
async def test_perception_interfaces():
    assert NullPerceptionProvider.available is False
    assert await NullPerceptionProvider().observe() is None
    snap = CameraSnapshot("desk", "2026-11-07T12:00:00Z", 1.0, 640, 480)
    obs = Observation(snap, (Detection("person", 0.9, BoundingBox(0.4, 0.2, 0.2, 0.5)),))
    assert obs.detections[0].bbox.center == pytest.approx((0.5, 0.45))
    mock = MockPerceptionProvider([obs])
    assert await mock.observe("desk") is obs and await mock.observe() is None
    with pytest.raises(ValueError):
        BoundingBox(0.9, 0, 0.5, 1.5)
    with pytest.raises(ValueError):
        Detection("person", 1.5)
    assert VLMConfig().enabled is False  # no OpenAI key needed until perception is enabled


# ------------------------------------------------------------------ repository separation
FORBIDDEN_IMPORTS = {"so101_companion", "lerobot", "serial", "feetech", "scservo_sdk", "mujoco"}


def test_no_robot_controller_serial_or_motor_imports():
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                assert m.split(".")[0] not in FORBIDDEN_IMPORTS, f"{path.name} imports {m}"


def test_no_physical_tools_registered_by_default():
    from so101_voice.tools.builtin import register_builtin_tools
    from so101_voice.tools.registry import Effect, ToolRegistry

    reg = ToolRegistry()
    register_builtin_tools(reg)
    assert all(reg._tools[n].effect in (Effect.NONE, Effect.READ) for n in reg.names)
    assert "talk" not in " ".join(reg.names)  # Talk Mode is driven by audio, never by the LLM


def test_robot_http_paths_used_are_talk_and_read_only():
    text = (SRC / "robot" / "client.py").read_text()
    posts = [line for line in text.splitlines() if '"POST"' in line]
    assert len(posts) == 2 and all("/talk/" in line for line in posts)


def test_dummy_event_loop_cleanup():
    asyncio.run(asyncio.sleep(0))


def test_motion_client_uses_only_play_pose_and_rest_routes():
    import re

    text = (SRC / "robot" / "motion.py").read_text()
    code = text.split('"""', 2)[-1]  # the code, not the module docstring
    paths = set(re.findall(r'"(/api/v1/[^"]*)"', code))
    assert paths == {"/api/v1/behaviors/{behavior_id}/play", "/api/v1/poses/{pose_id}/go", "/api/v1/rest"}
    for banned in ("emergency", "follow", "calibration", "torque", "record", "joint"):
        assert banned not in code.lower(), banned

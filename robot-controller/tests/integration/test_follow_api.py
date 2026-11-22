"""ADR-019 Follow Me / Talk Mode over the authenticated HTTP API and WebSocket (real controller thread,
FakeRobot, real FK metadata). Latest-value target updates never grow the command ledger."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from api_helpers import AUTH, TOKEN, LiveStack, wait_until
from so101_companion.api.runner import ApiServer
from so101_companion.config import ApiSection


@pytest.fixture
def stack(tmp_path: Path, calibration_file: Path) -> Iterator[LiveStack]:
    s = LiveStack(tmp_path, calibration_file)
    yield s
    s.close()


def follow(stack: LiveStack) -> dict:  # type: ignore[type-arg]
    return dict(stack.client.get("/api/v1/follow/status", headers=AUTH).json())


def test_new_routes_need_the_token(stack: LiveStack) -> None:
    for method, path, body in [
        ("post", "/api/v1/follow", {"enabled": True}),
        ("put", "/api/v1/follow/target", {"angle_deg": 10}),
        ("get", "/api/v1/follow/status", None),
        ("post", "/api/v1/talk/start", {}),
        ("post", "/api/v1/talk/stop", None),
        ("get", "/api/v1/talk/status", None),
    ]:
        r = (
            getattr(stack.client, method)(path, json=body)
            if body is not None
            else getattr(stack.client, method)(path)
        )
        assert r.status_code == 401, path
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Basic {TOKEN}"}):
        with (
            pytest.raises(WebSocketDisconnect) as err,
            stack.client.websocket_connect("/api/v1/follow/targets", headers=headers),
        ):
            pass
        assert err.value.code == 1008
    assert stack.commands.ledger.list() == [] and not follow(stack)["enabled"]


def test_follow_on_targets_status_and_no_ledger_growth(stack: LiveStack) -> None:
    r = stack.client.put("/api/v1/follow/target", headers=AUTH, json={"angle_deg": 10})
    assert r.status_code == 409  # Follow Me is off
    r = stack.client.post(
        "/api/v1/follow", headers=AUTH, json={"enabled": True, "target_angle_deg": 15, "transition": "fast"}
    )
    assert r.status_code == 202 and r.json()["state"] == "completed" and r.json()["kind"] == "follow"
    n_cmds = len(stack.commands.ledger.list(500))
    for i in range(40):  # ~20 Hz stream, latest value wins
        assert (
            stack.client.put("/api/v1/follow/target", headers=AUTH, json={"angle_deg": -20 + i}).status_code
            == 202
        )
        time.sleep(0.05)
    assert len(stack.commands.ledger.list(500)) == n_cmds  # target updates create no records
    assert wait_until(lambda: follow(stack)["target_deg"] == 19.0)
    assert wait_until(lambda: abs(follow(stack)["pan_command"] - follow(stack)["pan_target"]) < 0.05, 5.0)
    s = follow(stack)
    assert (
        s["state"] == "TRACKING"
        and s["reach"] == "REACHABLE"
        and s["reference"]["classification"] == "NORMAL"
    )
    assert s["ready"]["ok"] and s["owns"] == "shoulder_pan"
    assert stack.client.put("/api/v1/follow/target", headers=AUTH, json={"angle_deg": 270}).status_code == 422
    # stale input: no updates -> the last target is held, never reset to zero
    time.sleep(0.6)
    s = follow(stack)
    assert s["target_deg"] == 19.0 and s["target_age_s"] >= 0.5 and s["state"] == "TRACKING"
    r = stack.client.post("/api/v1/follow", headers=AUTH, json={"enabled": False})  # Stop following
    assert r.status_code == 202 and wait_until(lambda: follow(stack)["state"] == "OFF", 5.0)


def test_websocket_stream_latest_value_errors_and_disconnect(stack: LiveStack) -> None:
    stack.client.post(
        "/api/v1/follow", headers=AUTH, json={"enabled": True, "target_angle_deg": 0, "transition": "fast"}
    )
    # a COPY: starlette's TestClient writes its handshake headers into the dict it is given
    with stack.client.websocket_connect("/api/v1/follow/targets", headers=dict(AUTH)) as ws:
        replies = []
        for angle in range(0, 40, 2):
            ws.send_text(json.dumps({"angle_deg": angle}))
            replies.append(ws.receive_json())
        assert {r["type"] for r in replies} <= {"ack", "status"} and any(
            r["type"] == "status" for r in replies
        )
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        ws.send_text(json.dumps({"angle_deg": 999}))
        assert ws.receive_json()["type"] == "error"
    # disconnected: the last target (38°) is held
    assert wait_until(lambda: follow(stack)["target_deg"] == 38.0)
    assert wait_until(lambda: abs(follow(stack)["pan_command"] - follow(stack)["pan_target"]) < 0.05, 5.0)
    time.sleep(0.3)
    assert follow(stack)["target_deg"] == 38.0


def test_websocket_over_a_real_server(stack: LiveStack) -> None:
    from websockets.sync.client import connect

    server = ApiServer(stack.app, ApiSection(port=0))
    server.start()
    try:
        stack.client.post("/api/v1/follow", headers=AUTH, json={"enabled": True, "target_angle_deg": 0})
        url = server.url.replace("http://", "ws://") + "/api/v1/follow/targets"
        with connect(url, additional_headers=AUTH, proxy=None) as ws:  # never via an env proxy
            for a in (5, 10, 15):
                ws.send(json.dumps({"angle_deg": a}))
                assert json.loads(ws.recv())["type"] in ("ack", "status")
        assert wait_until(lambda: follow(stack)["target_deg"] == 15.0)
        from websockets.exceptions import InvalidStatus

        with pytest.raises(InvalidStatus), connect(url, proxy=None) as ws:  # no token: upgrade refused
            ws.recv(timeout=2)
    finally:
        server.stop()


def test_talk_over_the_api(stack: LiveStack) -> None:
    r = stack.client.post(
        "/api/v1/talk/start", headers=AUTH, json={"intensity": "excited", "lease_s": 5, "seed": 1}
    )
    assert r.status_code == 202 and r.json()["state"] == "completed"
    assert wait_until(lambda: stack.client.get("/api/v1/talk/status", headers=AUTH).json()["state"] == "ON")
    again = stack.client.post("/api/v1/talk/start", headers=AUTH, json={"intensity": "gentle", "lease_s": 5})
    assert "renewed" in again.json()["detail"]
    st = stack.client.get("/api/v1/talk/status", headers=AUTH).json()
    assert st["intensity"] == "gentle" and st["owns"] == "gripper" and 0 < st["lease_remaining_s"] <= 5
    assert stack.client.post("/api/v1/talk/start", headers=AUTH, json={"lease_s": 120}).status_code == 422
    assert stack.client.post("/api/v1/talk/stop", headers=AUTH).status_code == 202
    assert wait_until(
        lambda: stack.client.get("/api/v1/talk/status", headers=AUTH).json()["state"] == "OFF", 5.0
    )
    second = stack.client.post("/api/v1/talk/stop", headers=AUTH)  # harmless when already off
    assert second.status_code == 202 and "already off" in second.json()["detail"]


def test_emergency_latch_blocks_overlay_starts_but_not_stops(stack: LiveStack) -> None:
    stack.client.post("/api/v1/follow", headers=AUTH, json={"enabled": True, "target_angle_deg": 10})
    stack.client.post("/api/v1/talk/start", headers=AUTH, json={})
    assert stack.client.post("/api/v1/emergency-hold", headers=AUTH).status_code == 202
    assert wait_until(lambda: not follow(stack)["enabled"])
    r = stack.client.post("/api/v1/follow", headers=AUTH, json={"enabled": True})
    assert r.status_code == 409 and "Emergency Hold is latched" in r.json()["detail"]
    assert stack.client.post("/api/v1/talk/start", headers=AUTH, json={}).status_code == 409
    assert stack.client.post("/api/v1/talk/stop", headers=AUTH).status_code == 202
    assert stack.client.post("/api/v1/follow", headers=AUTH, json={"enabled": False}).status_code == 202


def test_physical_gate_applies_to_overlays(tmp_path: Path, calibration_file: Path) -> None:
    s = LiveStack(tmp_path, calibration_file, physical=False)
    try:
        r = s.client.post("/api/v1/follow", headers=AUTH, json={"enabled": True})
        assert r.status_code == 409 and "allow_physical_control" in r.json()["detail"]
        assert s.client.post("/api/v1/talk/start", headers=AUTH, json={}).status_code == 409
        assert not s.client.get("/api/v1/follow/status", headers=AUTH).json()["ready"]["ok"]
    finally:
        s.close()


def test_concurrent_target_writers_and_commands_one_bus_owner(stack: LiveStack) -> None:
    stack.client.post("/api/v1/follow", headers=AUTH, json={"enabled": True, "target_angle_deg": 0})
    loop = stack.behavior(reps=None)
    stack.client.post(f"/api/v1/behaviors/{loop.id}/play", headers=AUTH)

    def writer(base: int) -> None:
        for i in range(30):
            stack.client.put("/api/v1/follow/target", headers=AUTH, json={"angle_deg": (base + i) % 90})

    threads = [threading.Thread(target=writer, args=(k * 10,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.3)
    assert stack.robot.bus_threads == {"so101-controller"}
    s = follow(stack)
    assert s["enabled"] and 0 <= s["target_deg"] < 90

"""ADR-018 HTTP API end to end: FastAPI app + the REAL controller thread + a FakeRobot (no device).

Covers authentication (13), non-blocking POSTs (14), idempotent retries, concurrent requests (12),
single bus ownership, error mapping, the OpenAPI surface and a dispatch-latency measurement.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from api_helpers import AUTH, TOKEN, LiveStack, wait_until
from so101_companion.api.auth import TokenFileError, TokenVerifier, create_token, token_hash
from so101_companion.api.runner import ApiServer
from so101_companion.config import ApiSection, ConfigError, validate_api
from so101_companion.domain.state import Connection


@pytest.fixture
def stack(tmp_path: Path, calibration_file: Path) -> Iterator[LiveStack]:
    s = LiveStack(tmp_path, calibration_file)
    yield s
    s.close()


# ------------------------------------------------------------------ 13
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer "},
    ],
)
def test_13_invalid_tokens_get_401_and_execute_nothing(stack: LiveStack, headers: dict[str, str]) -> None:
    pose = stack.pose()
    b = stack.behavior()
    assert wait_until(lambda: any(c == "send" for c, _ in stack.robot.calls))
    held = [a for c, a in stack.robot.calls if c == "send"][-1]  # ARMED re-sends this target every tick
    n_calls = len(stack.robot.calls)
    for method, path in [
        ("post", f"/api/v1/behaviors/{b.id}/play"),
        ("post", f"/api/v1/poses/{pose.id}/go"),
        ("post", "/api/v1/rest"),
        ("post", "/api/v1/emergency-hold"),
        ("post", "/api/v1/playback/pause"),
        ("get", "/api/v1/status"),
        ("get", "/api/v1/behaviors"),
        ("get", "/api/v1/simulation/frame"),
    ]:
        r = getattr(stack.client, method)(path, headers=headers)
        assert r.status_code == 401, (path, r.status_code)
        assert r.headers["www-authenticate"].startswith("Bearer")
    time.sleep(0.2)
    assert stack.commands.ledger.list() == []  # nothing recorded ...
    assert stack.snap().connection == Connection.ARMED  # ... nothing dispatched
    assert stack.snap().extra["emergency_latched"] is None
    assert all(a == held for c, a in stack.robot.calls[n_calls:] if c == "send")  # no new target


def test_token_file_hashes_only_and_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "api-tokens.json"
    token = create_token(path, "agent")
    assert token not in path.read_text() and token_hash(token) in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    v = TokenVerifier(path=path)
    assert v.verify(token) == "agent" and v.verify(token + "x") is None and v.verify(None) is None
    path.chmod(0o644)
    path.write_text(path.read_text())  # changed + world-readable: refused, fail closed
    assert v.verify(token) is None
    with pytest.raises(TokenFileError):
        TokenVerifier(path=tmp_path / "missing.json")


def test_remote_binding_needs_explicit_opt_in() -> None:
    with pytest.raises(ConfigError, match="remote access is disabled"):
        validate_api(ApiSection(host="0.0.0.0"))
    validate_api(ApiSection(host="0.0.0.0", allow_remote=True))
    with pytest.raises(ConfigError):
        validate_api(ApiSection(stream_max_fps=10.0))  # the 5 FPS cap is not configurable upwards
    assert ApiSection().host == "127.0.0.1" and ApiSection().allow_physical_control is False


# ------------------------------------------------------------------ 14
def test_14_post_returns_before_the_motion_completes(stack: LiveStack) -> None:
    b = stack.behavior(reps=3, after="hold")  # ~3.5 s of motion
    t0 = time.monotonic()
    r = stack.client.post(f"/api/v1/behaviors/{b.id}/play", headers=AUTH, json={"repeat": 3})
    elapsed = time.monotonic() - t0
    body = r.json()
    assert r.status_code == 202 and body["state"] == "running" and body["kind"] == "play_behavior"
    assert elapsed < 0.6, elapsed  # the controller's verdict, never the motion
    assert body["links"]["self"] == f"/api/v1/commands/{body['id']}"
    mid = stack.client.get(body["links"]["self"], headers=AUTH).json()
    assert mid["state"] == "running" and mid["progress"]["behavior"]["name"] == "talk"
    done = stack.wait_command(body["id"], ("completed",))
    assert done["duration_s"] > 2.0


def test_errors_are_mapped_and_nothing_moves(stack: LiveStack) -> None:
    b = stack.behavior()
    assert stack.client.post("/api/v1/behaviors/nope/play", headers=AUTH).status_code == 404
    assert stack.client.post("/api/v1/poses/nope/go", headers=AUTH).status_code == 404
    r = stack.client.post(f"/api/v1/behaviors/{b.id}/play", headers=AUTH, json={"speed": 3.0})
    assert r.status_code == 422
    r = stack.client.post(f"/api/v1/behaviors/{b.id}/play", headers=AUTH, json={"raw_joints": [1, 2]})
    assert r.status_code == 422  # unknown fields are refused, not ignored
    r = stack.client.post(f"/api/v1/behaviors/{b.id}/play", headers=AUTH, json={"after_behavior": "pose"})
    assert r.status_code == 422 and "after_pose_id" in json.dumps(r.json())
    r = stack.client.post("/api/v1/playback/pause", headers=AUTH)  # nothing is playing
    assert r.status_code == 409 and r.json()["state"] == "rejected" and r.json()["detail"]
    assert stack.client.get("/api/v1/simulation/frame", headers=AUTH).status_code == 503  # no video here


def test_status_and_libraries(stack: LiveStack) -> None:
    b = stack.behavior()
    p = stack.pose()
    s = stack.client.get("/api/v1/status", headers=AUTH).json()
    assert s["mode"] == "SIM_TO_REAL" and s["controller"]["running"] and s["physical_control_enabled"]
    assert s["ready"]["play_behavior"] == {"ok": True, "reason": None}
    assert s["ready"]["pause"]["ok"] is False and s["emergency_hold_latched"] is None
    beh = stack.client.get("/api/v1/behaviors", headers=AUTH).json()["behaviors"]
    assert [x["id"] for x in beh] == [b.id] and beh[0]["saved_settings"]["after"]["action"] == "rest"
    poses = stack.client.get("/api/v1/poses", headers=AUTH).json()["poses"]
    assert poses[0]["id"] == p.id and poses[0]["usable_on_connected_arm"] is True


def test_openapi_exposes_only_the_allowed_operations(stack: LiveStack) -> None:
    spec = stack.client.get("/openapi.json").json()
    routes = {(m.upper(), path) for path, ops in spec["paths"].items() for m in ops}
    assert routes == {
        ("GET", "/api/v1/status"),
        ("GET", "/api/v1/behaviors"),
        ("GET", "/api/v1/poses"),
        ("GET", "/api/v1/commands"),
        ("GET", "/api/v1/commands/{command_id}"),
        ("POST", "/api/v1/behaviors/{behavior_id}/play"),
        ("POST", "/api/v1/poses/{pose_id}/go"),
        ("POST", "/api/v1/rest"),
        ("POST", "/api/v1/playback/pause"),
        ("POST", "/api/v1/playback/resume"),
        ("POST", "/api/v1/emergency-hold"),
        ("GET", "/api/v1/simulation/frame"),
        ("GET", "/api/v1/simulation/stream.mjpeg"),
        # M2.6 overlays (ADR-019): an angle and presets only — never joint targets
        ("POST", "/api/v1/follow"),
        ("PUT", "/api/v1/follow/target"),
        ("GET", "/api/v1/follow/status"),
        ("POST", "/api/v1/talk/start"),
        ("POST", "/api/v1/talk/stop"),
        ("GET", "/api/v1/talk/status"),
    }
    paths = " ".join(spec["paths"]).lower()
    for forbidden in ("record", "calibrat", "torque", "connect", "joint", "motor", "mode", "delete"):
        assert forbidden not in paths, forbidden
    for schema in ("PlayBody", "FollowBody", "FollowTargetBody", "TalkStartBody"):
        assert spec["components"]["schemas"][schema]["additionalProperties"] is False  # no hidden fields


# ------------------------------------------------------------------ idempotency
def test_idempotent_retries_do_not_dispatch_twice(stack: LiveStack) -> None:
    pose = stack.pose()
    h = AUTH | {"Idempotency-Key": "watch-123"}
    first = stack.client.post(f"/api/v1/poses/{pose.id}/go", headers=h, json={"transition": "fast"})
    retry = stack.client.post(f"/api/v1/poses/{pose.id}/go", headers=h, json={"transition": "fast"})
    assert first.status_code == 202 and retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"] and retry.json()["idempotent_replay"] is True
    clash = stack.client.post(f"/api/v1/poses/{pose.id}/go", headers=h, json={"transition": "gentle"})
    assert clash.status_code == 409 and clash.json()["detail"]["command_id"] == first.json()["id"]
    assert len([r for r in stack.commands.ledger.list() if r["kind"] == "go_pose"]) == 1


# ------------------------------------------------------------------ 12 over real HTTP, one bus owner
def test_12_concurrent_http_requests_one_motion_one_bus_owner(stack: LiveStack) -> None:
    server = ApiServer(stack.app, ApiSection(port=0))
    server.start()
    try:
        poses = [stack.pose(f"P{i}", pan=10.0 * (i + 1)) for i in range(3)]
        loops = [stack.behavior(f"L{i}", j, reps=None) for i, j in enumerate(["elbow_flex", "wrist_flex"])]
        paths = [f"/api/v1/poses/{p.id}/go" for p in poses] + [
            f"/api/v1/behaviors/{b.id}/play" for b in loops
        ]
        paths += ["/api/v1/rest"]
        codes: list[int] = []
        lock = threading.Lock()

        def fire(path: str) -> None:
            with httpx.Client(base_url=server.url, headers=AUTH, timeout=10.0) as c:
                r = c.post(path, json={})
                with lock:
                    codes.append(r.status_code)

        threads = [threading.Thread(target=fire, args=(paths[i % len(paths)],)) for i in range(36)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(codes) == 36 and set(codes) <= {202, 409}
        time.sleep(0.3)
        recs = stack.commands.ledger.list(100)
        active = [r for r in recs if r["state"] in ("running", "paused", "ending")]
        assert len(active) <= 1 and all(r["state"] != "queued" for r in recs)
        assert stack.robot.bus_threads == {"so101-controller"}  # the API never touches the bus
    finally:
        server.stop()


# ------------------------------------------------------------------ latency (measured, documented)
def test_dispatch_latency_with_fake_hardware(stack: LiveStack, tmp_path: Path) -> None:
    poses = [stack.pose("A", 20.0), stack.pose("B", -20.0)]
    dispatch, first_target, http = [], [], []
    for i in range(40):
        t0 = time.monotonic()
        r = stack.client.post(
            f"/api/v1/poses/{poses[i % 2].id}/go", headers=AUTH, json={"transition": "fast"}
        )
        http.append((time.monotonic() - t0) * 1000.0)
        assert r.status_code == 202
        rec = stack.wait_command(r.json()["id"], ("running", "completed", "superseded"), timeout=2.0)
        assert wait_until(
            lambda rid=rec["id"]: stack.commands.ledger.get(rid)["latency_ms"]["first_target"] is not None,
            1.0,
        )
        lat = stack.commands.ledger.get(rec["id"])["latency_ms"]
        dispatch.append(lat["dispatch"])
        first_target.append(lat["first_target"])
        time.sleep(0.07)
    report = {
        "samples": len(dispatch),
        "dispatch_ms": {"p50": statistics.median(dispatch), "max": max(dispatch)},
        "first_target_ms": {"p50": statistics.median(first_target), "max": max(first_target)},
        "http_roundtrip_ms": {"p50": statistics.median(http), "max": max(http)},
        "command_hz": 20,
    }
    print("\nAPI latency (fake hardware):", json.dumps(report))
    assert report["dispatch_ms"]["p50"] < 20.0  # submit() wakes the controller at once
    assert report["first_target_ms"]["p50"] <= 80.0  # next control tick (50 ms period)

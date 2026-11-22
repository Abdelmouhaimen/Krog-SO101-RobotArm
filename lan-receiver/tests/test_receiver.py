import os
import time
import pytest
from fastapi.testclient import TestClient
from lan_bridge import receiver


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    receiver.store = receiver.Store()
    monkeypatch.setenv("BRIDGE_TOKEN", "unit-test-secret")


@pytest.fixture
def client():
    return TestClient(receiver.app)


HEADERS = {"X-Bridge-Token": "unit-test-secret"}


def test_auth_required(client):
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"X-Bridge-Token": "wrong"}).status_code == 401
    assert client.get("/v1/status", headers=HEADERS).status_code == 200


def test_frame_receipt_and_cache(client):
    jpeg = b"\xff\xd8\xff" + b"fake jpeg" + b"\xff\xd9"
    headers = dict(HEADERS, **{"X-Session-Id": "session12345", "X-Sequence": "1", "Content-Type": "image/jpeg"})
    assert client.post("/v1/frame", headers=headers, content=jpeg).json()["accepted"]
    assert client.get("/v1/frame", headers=HEADERS).content == jpeg
    assert not client.post("/v1/frame", headers=headers, content=jpeg).json()["accepted"]
    assert client.get("/v1/status", headers=HEADERS).json()["screen"]["available"]
    assert client.post("/v1/frame", headers=headers, content=b"not jpeg").status_code == 400


def test_angle_lost_and_sequence(client):
    packet = {"session": "session12345", "seq": 1, "found": True,
              "angle_deg": 19.3, "camera_angle_deg": 19.3, "confidence": 0.9}
    assert client.post("/v1/angle", headers=HEADERS, json=packet).json()["accepted"]
    status = client.get("/v1/status", headers=HEADERS).json()
    assert status["face"]["angle_deg"] == pytest.approx(19.3)
    assert not status["robot_control_enabled"]
    assert not client.post("/v1/angle", headers=HEADERS, json=packet).json()["accepted"]
    lost = {"session": "session12345", "seq": 2, "found": False,
            "angle_deg": None, "camera_angle_deg": None, "confidence": None}
    assert client.post("/v1/angle", headers=HEADERS, json=lost).json()["accepted"]
    assert client.get("/v1/status", headers=HEADERS).json()["face"]["angle_deg"] is None


def test_stale_data_not_offered():
    receiver.update_frame(b"\xff\xd8\xffdata\xff\xd9", "session12345", 1, now=100)
    receiver.update_angle({"session": "session12345", "seq": 1, "found": True,
                           "angle_deg": 12.0, "camera_angle_deg": 12.0, "confidence": 0.9}, now=100)
    status = receiver.current_status(now=110)
    assert not status["screen"]["available"]
    assert not status["face"]["found"]
    assert status["face"]["angle_deg"] is None


def test_invalid_angle_rejected(client):
    packet = {"session": "session12345", "seq": 1, "found": True,
              "angle_deg": 500, "camera_angle_deg": 19, "confidence": 1}
    assert client.post("/v1/angle", headers=HEADERS, json=packet).status_code == 400
    packet["angle_deg"] = 19
    packet["found"] = False
    assert client.post("/v1/angle", headers=HEADERS, json=packet).status_code == 400


def test_independent_ttl_boundaries():
    receiver.update_frame(b'\xff\xd8\xfftest\xff\xd9', 'session12345', 1, now=100)
    receiver.update_angle(dict(session='session12345', seq=1, found=True, angle_deg=0,
                               camera_angle_deg=0, confidence=1), now=100)
    assert receiver.current_status(now=100.5)['face']['found']
    assert not receiver.current_status(now=100.501)['face']['found']
    assert receiver.current_status(now=105)['screen']['available']
    assert not receiver.current_status(now=105.001)['screen']['available']


def test_frame_metadata_matches_atomic_payload(client):
    jpeg = b'\xff\xd8\xffselected-monitor\xff\xd9'
    receiver.update_frame(jpeg, 'session12345', 7)
    response = client.get('/v1/frame', headers=HEADERS)
    assert response.content == jpeg
    assert response.headers['X-Session-Id'] == 'session12345'
    assert response.headers['X-Sequence'] == '7'
    assert 0 <= float(response.headers['X-Age-S']) < 1
    assert client.get('/v1/status', headers=HEADERS).json()['screen']['session'] == 'session12345'


def test_private_peer_filter(client, monkeypatch):
    monkeypatch.setenv('BRIDGE_ALLOWED_PEERS', '192.168.50.1,192.168.50.2')
    assert client.get('/health').status_code == 403

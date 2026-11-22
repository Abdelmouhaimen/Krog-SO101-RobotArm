"""Agent/robot PC receiver. Stores observations only; NEVER commands motors.

Start: BRIDGE_TOKEN='secret' python -m lan_bridge.receiver --host 192.168.50.1
All images are kept in RAM and require a token to read.
"""
from __future__ import annotations

import argparse
import hmac
import math
import os
from pathlib import Path
import threading
import time
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

MAX_JPEG = 4_000_000
FACE_TTL_S = 0.5
FRAME_TTL_S = 5.0


@dataclass
class FrameData:
    data: bytes = b""
    seen: float = 0.0
    seq: int = -1
    session: str = ""


@dataclass
class AngleData:
    found: bool = False
    degrees: float | None = None
    camera_degrees: float | None = None
    confidence: float | None = None
    seen: float = 0.0
    seq: int = -1
    session: str = ""


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = FrameData()
        self.angle = AngleData()


store = Store()
app = FastAPI(title="SO-101 LAN Perception Receiver", version="0.1.0")


@app.middleware("http")
async def restrict_peer(request: Request, call_next):
    # Optional defense in depth for the direct link; never trust forwarded client headers.
    peers = {p.strip() for p in os.environ.get("BRIDGE_ALLOWED_PEERS", "").split(",") if p.strip()}
    if peers and (request.client is None or request.client.host not in peers):
        return JSONResponse({"detail": "Peer not allowed"}, status_code=403)
    return await call_next(request)


def require_token(received: str | None) -> None:
    expected = os.environ.get("BRIDGE_TOKEN", "")
    if not expected:
        raise HTTPException(503, "BRIDGE_TOKEN is not configured on receiver")
    if received is None or not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Invalid token")


def update_frame(data: bytes, session: str, seq: int, *, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with store.lock:
        previous = store.frame
        if previous.session == session and seq <= previous.seq:
            return False
        store.frame = FrameData(data=data, seen=now, seq=seq, session=session)
    return True


def update_angle(body: dict, *, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    session = body["session"]
    seq = body["seq"]
    found = body["found"]
    angle = body.get("angle_deg") if found else None
    camera = body.get("camera_angle_deg") if found else None
    confidence = body.get("confidence") if found else None
    with store.lock:
        previous = store.angle
        if previous.session == session and seq <= previous.seq:
            return False
        store.angle = AngleData(
            found=found, degrees=angle, camera_degrees=camera,
            confidence=confidence, seen=now, seq=seq, session=session,
        )
    return True


def current_status(*, now: float | None = None) -> dict:
    now = time.monotonic() if now is None else now
    with store.lock:
        frame = store.frame
        angle = store.angle
        frame_age = max(0.0, now - frame.seen) if frame.seen else None
        face_age = max(0.0, now - angle.seen) if angle.seen else None
        fresh_frame = bool(frame.data) and frame_age is not None and frame_age <= FRAME_TTL_S
        fresh_face = angle.found and face_age is not None and face_age <= FACE_TTL_S
        return {
            "screen": {"available": fresh_frame, "age_s": frame_age,
                       "seq": frame.seq, "session": frame.session, "bytes": len(frame.data)},
            "face": {"found": bool(fresh_face), "age_s": face_age,
                     "angle_deg": angle.degrees if fresh_face else None,
                     "camera_angle_deg": angle.camera_degrees if fresh_face else None,
                     "confidence": angle.confidence if fresh_face else None,
                     "seq": angle.seq, "session": angle.session},
            "robot_control_enabled": False,
        }


@app.get("/health")
def health():
    return {"ok": True, "robot_control_enabled": False}


@app.post("/v1/frame")
async def receive_frame(
    request: Request,
    x_bridge_token: str | None = Header(default=None),
    x_session_id: str = Header(default=""),
    x_sequence: str = Header(default=""),
):
    require_token(x_bridge_token)
    if not 8 <= len(x_session_id) <= 80:
        raise HTTPException(400, "Invalid session id")
    try:
        seq = int(x_sequence)
        if seq < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Invalid frame sequence") from None
    if request.headers.get("content-type", "").split(";")[0] != "image/jpeg":
        raise HTTPException(415, "Only image/jpeg is accepted")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JPEG:
            raise HTTPException(413, "JPEG exceeds size limit")
        chunks.append(chunk)
    jpeg = b"".join(chunks)
    if not jpeg.startswith(b"\xff\xd8\xff") or not jpeg.endswith(b"\xff\xd9"):
        raise HTTPException(400, "Invalid JPEG boundary")
    accepted = update_frame(jpeg, x_session_id, seq)
    return {"accepted": accepted}


@app.get("/v1/frame")
def latest_frame(x_bridge_token: str | None = Header(default=None)):
    require_token(x_bridge_token)
    with store.lock:
        frame = store.frame
        if not frame.data or time.monotonic() - frame.seen > FRAME_TTL_S:
            raise HTTPException(404, "No fresh frame")
        data = frame.data
        headers = {"Cache-Control": "no-store", "X-Session-Id": frame.session,
                   "X-Sequence": str(frame.seq), "X-Age-S": str(time.monotonic() - frame.seen)}
    return Response(data, media_type="image/jpeg", headers=headers)


@app.post("/v1/angle")
async def receive_angle(request: Request, x_bridge_token: str | None = Header(default=None)):
    require_token(x_bridge_token)
    raw = await request.body()
    if len(raw) > 4_096:
        raise HTTPException(413, "Angle message too large")
    try:
        import json
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Not an object")
        session = body.get("session")
        seq = body.get("seq")
        found = body.get("found")
        if not isinstance(session, str) or not 8 <= len(session) <= 80:
            raise ValueError("session invalid")
        if type(seq) is not int or seq < 0:
            raise ValueError("seq invalid")
        if type(found) is not bool:
            raise ValueError("found invalid")
        if found:
            for key in ("angle_deg", "camera_angle_deg", "confidence"):
                value = body.get(key)
                if type(value) not in (float, int) or not math.isfinite(value):
                    raise ValueError(f"{key} invalid")
            if not -180 <= body["angle_deg"] <= 180 or not -180 <= body["camera_angle_deg"] <= 180:
                raise ValueError("Angle outside +-180")
            if not 0 <= body["confidence"] <= 1:
                raise ValueError("Confidence outside [0,1]")
        else:
            for key in ("angle_deg", "camera_angle_deg", "confidence"):
                if body.get(key) is not None:
                    raise ValueError("No-face message must not contain an angle")
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, f"Invalid angle packet: {exc}") from None
    return {"accepted": update_angle(body)}


@app.get("/v1/status")
def status(x_bridge_token: str | None = Header(default=None)):
    require_token(x_bridge_token)
    return JSONResponse(current_status(), headers={"Cache-Control": "no-store"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Receive gaming-PC screen snapshots and face bearings")
    parser.add_argument("--host", default="127.0.0.1", help="Bind to Ethernet adapter IP, not 0.0.0.0 if possible")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    token_file = Path(__file__).resolve().parents[1] / ".env"
    if not os.environ.get("BRIDGE_TOKEN") and token_file.exists():
        for line in token_file.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "BRIDGE_TOKEN":
                os.environ["BRIDGE_TOKEN"] = value.strip().strip("\"'")
    if not os.environ.get("BRIDGE_TOKEN", ""):
        parser.error("Set a nonempty BRIDGE_TOKEN environment variable first")
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()

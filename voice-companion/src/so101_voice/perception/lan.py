"""Authenticated RAM-only webcam/screen observations. No robot or provider calls."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field


class Face(BaseModel):
    found: bool = False
    age_s: float | None = Field(default=None, ge=0)
    angle_deg: float | None = Field(default=None, ge=-180, le=180)
    camera_angle_deg: float | None = Field(default=None, ge=-180, le=180)
    confidence: float | None = Field(default=None, ge=0, le=1)
    seq: int = -1
    session: str = ""


class Screen(BaseModel):
    available: bool = False
    age_s: float | None = Field(default=None, ge=0)
    seq: int = -1
    session: str = ""
    bytes: int = Field(default=0, ge=0, le=4_000_000)


class Status(BaseModel):
    face: Face = Field(default_factory=Face)
    screen: Screen = Field(default_factory=Screen)


@dataclass(frozen=True)
class ScreenObservation:
    jpeg: bytes
    session: str
    seq: int
    age_s: float
    received_mono: float

    def fresh_jpeg(self) -> bytes | None:
        """Reusable vision adapter; caller must separately authorize any paid image request."""
        return self.jpeg if self.age_s + time.monotonic() - self.received_mono <= 5 else None


class PerceptionClient:
    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout_s: float = 0.3,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            timeout=timeout_s,
            headers={"X-Bridge-Token": token} if token else {},
            trust_env=False,
            transport=transport,
        )
        self.reachable = False
        self.auth = "unchecked" if token else "not configured"
        self.connected = False
        self.error: str | None = None
        self.reconnections = 0
        self.generation = 0
        self.status = Status()
        self.received_mono = 0.0

    async def poll(self) -> Status:
        started = time.monotonic()
        try:
            r = await self._http.get("/v1/status")
            self.reachable = True
            if r.status_code in (401, 403):
                self.auth = "rejected"
                raise ValueError("authentication rejected")
            r.raise_for_status()
            self.auth = "accepted"
            status = Status.model_validate(r.json())
            if not self.connected:
                self.generation += 1
                self.reconnections += 1
            self.connected, self.error = True, None
            self.status, self.received_mono = status, started
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            if isinstance(exc, httpx.RequestError):
                self.reachable = False
            self.connected = False
            self.error = (
                "receiver authentication rejected"
                if self.auth == "rejected"
                else "receiver unavailable or invalid observation"
            )
            self.status = Status()
        return self.status

    def snapshot(self) -> Status:
        """Ages continue to advance even when no new poll has completed."""
        status = self.status.model_copy(deep=True)
        elapsed = max(0.0, time.monotonic() - self.received_mono)
        if status.face.age_s is not None:
            status.face.age_s += elapsed
        if status.screen.age_s is not None:
            status.screen.age_s += elapsed
        if not self.connected or status.face.age_s is None or status.face.age_s > 0.5:
            status.face.found = False
        if not status.face.found:
            status.face.angle_deg = status.face.camera_angle_deg = status.face.confidence = None
        if not self.connected or status.screen.age_s is None or status.screen.age_s > 5:
            status.screen.available = False
        return status

    def face_ready(self) -> bool:
        face = self.snapshot().face
        return face.found and face.angle_deg is not None

    async def screen(self) -> ScreenObservation | None:
        """On-demand capture retrieval only. Metadata and JPEG come from one atomic response."""
        started = time.monotonic()
        try:
            r = await self._http.get("/v1/frame")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            age = float(r.headers["X-Age-S"])
            if (
                len(r.content) > 4_000_000
                or not r.content.startswith(b"\xff\xd8\xff")
                or not r.content.endswith(b"\xff\xd9")
                or not math.isfinite(age)
                or age < 0
            ):
                return None
            return ScreenObservation(
                r.content, r.headers["X-Session-Id"], int(r.headers["X-Sequence"]), age, started
            )
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    async def aclose(self) -> None:
        await self._http.aclose()

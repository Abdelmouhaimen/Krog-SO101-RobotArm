"""Client for the existing SO-101 Studio HTTP API (``/api/v1``, ADR-018/019 in the robot repository).

The robot repository stays the single authority for movement: this client only speaks HTTP. It never
imports robot code, touches a serial port or sends joint targets.

Exposed here:
* Talk Mode overlay: ``talk_start`` / ``talk_stop`` / ``talk_status`` (gripper animation only).
* Read-only: ``status``, ``list_behaviors``, ``list_poses``, ``command``, ``commands``.

Arm motion (play behavior, go pose, Go Rest) lives in ``so101_voice.robot.motion.RobotMotion``, which
uses this client's transport. Follow Me, Emergency Hold, recording and calibration are never exposed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

TalkIntensity = Literal["gentle", "normal", "excited"]


class RobotAPIError(Exception):
    """Base error. ``reachable`` is False when the service could not be contacted at all."""

    def __init__(self, message: str, *, status: int | None = None, reachable: bool = True) -> None:
        super().__init__(message)
        self.status = status
        self.reachable = reachable


class RobotUnavailable(RobotAPIError):
    """Connection refused / timeout / DNS — the service is down or not started."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reachable=False)


class RobotAuthError(RobotAPIError):
    pass


class RobotRejected(RobotAPIError):
    """409: the controller refused (Emergency Hold latched, torque off, fault ...). Nothing moved."""


@dataclass(frozen=True)
class RobotResult:
    status: int
    body: dict[str, Any]
    request_id: str


class RobotAPIClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("SO-101 API token is missing (set SO101_API_TOKEN)")
        self.base_url = base_url.rstrip("/")
        # trust_env=False: never route the local robot through HTTP(S)_PROXY.
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "so101-voice/0.1"},
            timeout=httpx.Timeout(timeout_s),
            trust_env=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> RobotResult:
        request_id = uuid.uuid4().hex
        headers = {"X-Request-ID": request_id}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = await self._http.request(method, path, json=json, headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            raise RobotUnavailable(f"SO-101 API unreachable at {self.base_url}: {type(e).__name__}") from e
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {"raw": r.text[:500]}
        if not isinstance(body, dict):
            body = {"data": body}
        if r.status_code == 401:
            raise RobotAuthError("SO-101 API token rejected (401)", status=401)
        if r.status_code == 409:
            raise RobotRejected(str(body.get("detail") or body.get("error") or body), status=409)
        if r.status_code == 503:
            raise RobotAPIError("SO-101 controller not running (503)", status=503)
        if r.status_code >= 400:
            raise RobotAPIError(
                f"SO-101 API {r.status_code}: {body.get('detail', body)}", status=r.status_code
            )
        return RobotResult(r.status_code, body, request_id)

    # ------------------------------------------------------------------ Talk Mode overlay
    async def talk_start(self, *, lease_s: float, intensity: TalkIntensity = "normal") -> RobotResult:
        """Start, or renew the lease of, Talk Mode. The robot auto-stops when the lease expires."""
        if not 0.0 < lease_s <= 60.0:
            raise ValueError("lease_s must be in (0, 60]")
        return await self._request(
            "POST", "/api/v1/talk/start", json={"intensity": intensity, "lease_s": round(lease_s, 2)}
        )

    async def talk_stop(self) -> RobotResult:
        return await self._request("POST", "/api/v1/talk/stop")

    async def talk_status(self) -> dict[str, Any]:
        return (await self._request("GET", "/api/v1/talk/status")).body

    # ------------------------------------------------------------------ read-only
    async def status(self) -> dict[str, Any]:
        return (await self._request("GET", "/api/v1/status")).body

    async def list_behaviors(self) -> list[dict[str, Any]]:
        return list((await self._request("GET", "/api/v1/behaviors")).body.get("behaviors", []))

    async def list_poses(self) -> list[dict[str, Any]]:
        return list((await self._request("GET", "/api/v1/poses")).body.get("poses", []))

    async def command(self, command_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/api/v1/commands/{command_id}")).body

    async def commands(self, limit: int = 20) -> list[dict[str, Any]]:
        body = (await self._request("GET", f"/api/v1/commands?limit={int(limit)}")).body
        return list(body.get("commands", []))

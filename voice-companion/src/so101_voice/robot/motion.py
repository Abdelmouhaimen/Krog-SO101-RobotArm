"""Arm motion through the existing SO-101 HTTP API: play a saved behavior, go to a named pose, Go Rest.

Verified against the robot repository on 2026-11-08 (``docs/HTTP_API.md`` and ``api/server.py``):

* ``POST /api/v1/behaviors/{id}/play``, body fields all optional. An **omitted** ``repeat`` or ``speed``
  uses the behavior's **saved** setting, so ``idle_*`` loops until stopped and one-shots play once.
  Overrides apply to that run only; the behavior file is never modified.
* ``POST /api/v1/poses/{id}/go`` with ``{"transition": ...}``, and ``POST /api/v1/rest``.
* Every POST returns at once:
  * ``202`` ``running`` / ``queued``;
  * ``409`` ``rejected`` (Emergency Hold, torque, fault ...);
  * ``200`` ``idempotent_replay`` for a repeated ``Idempotency-Key``.
* A new motion supersedes the running one with no rest in between. Lifecycle:
  ``GET /api/v1/commands/{id}``.

**Retries never duplicate a movement.** Every request carries an ``Idempotency-Key``. After a timeout
the same request is sent once more with the **same** key: the robot either replays the original command
or runs it for the first time. If that also fails, the command ledger is searched for the key before
giving up, because the robot may have accepted it.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from so101_voice.robot.client import RobotAPIClient, RobotUnavailable

log = logging.getLogger(__name__)

Transition = Literal["gentle", "normal", "fast"]
Repeat = int | Literal["until_stopped"]


class RobotMotion:
    """Implements ``so101_voice.robot.actions.RobotActions`` over the shared ``RobotAPIClient``."""

    def __init__(self, client: RobotAPIClient) -> None:
        self.client = client
        self.sent: list[tuple[str, str]] = []  # (path, idempotency key), for logs and tests

    async def list_behaviors(self) -> list[dict[str, Any]]:
        return await self.client.list_behaviors()

    async def list_poses(self) -> list[dict[str, Any]]:
        return await self.client.list_poses()

    async def play_behavior(
        self,
        behavior_id: str,
        *,
        idempotency_key: str,
        speed: float | None = None,
        repeat: Repeat | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}  # omitted fields = the behavior's saved settings
        if speed is not None:
            body["speed"] = speed
        if repeat is not None:
            body["repeat"] = repeat
        return await self._post(f"/api/v1/behaviors/{behavior_id}/play", body or None, idempotency_key)

    async def go_pose(
        self, pose_id: str, *, idempotency_key: str, transition: Transition = "normal"
    ) -> dict[str, Any]:
        return await self._post(f"/api/v1/poses/{pose_id}/go", {"transition": transition}, idempotency_key)

    async def go_rest(self, *, idempotency_key: str) -> dict[str, Any]:
        return await self._post("/api/v1/rest", None, idempotency_key)

    async def _post(self, path: str, body: dict[str, Any] | None, key: str) -> dict[str, Any]:
        self.sent.append((path, key))
        try:
            return (await self.client._request("POST", path, json=body, idempotency_key=key)).body
        except RobotUnavailable as first:
            log.warning("robot POST %s timed out; retrying once with the same Idempotency-Key", path)
            try:
                return (await self.client._request("POST", path, json=body, idempotency_key=key)).body
            except RobotUnavailable as second:
                found = await self._find_by_key(key)
                if found is not None:
                    return found
                raise first from second

    async def _find_by_key(self, key: str) -> dict[str, Any] | None:
        try:
            for rec in await self.client.commands(50):
                if rec.get("idempotency_key") == key:
                    return rec
        except RobotUnavailable:
            return None
        return None

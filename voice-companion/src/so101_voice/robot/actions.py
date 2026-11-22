"""Interface for SO-101 arm actions, implemented by ``so101_voice.robot.motion.RobotMotion``.

Rules every implementation and caller follows (see docs/ARCHITECTURE.md §11):
1. Only the robot's public HTTP API: saved behaviors, named poses and Go Rest. Never raw joint
   targets, calibration, torque, recording or serial access. Follow Me is not exposed.
2. Every motion request carries an ``Idempotency-Key`` derived from the LLM function-call id (or a
   fresh id for automatic idles), so a retried or duplicated call never triggers a second movement.
3. LLM tools run only for a committed user turn. If the user barges in before the request is sent,
   nothing is sent. A command already accepted by the robot is an external side effect: it is
   replaced by a newer command, never "cancelled" by dropping the LLM response.
4. On a real arm, motion additionally needs the robot service's ``allow_physical_control`` and the
   companion's ``[robot_tools] allow_physical`` (default true by the owner's choice;
   ``--no-physical-robot`` refuses it for a session).
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

Transition = Literal["gentle", "normal", "fast"]


class RobotActions(Protocol):
    async def list_behaviors(self) -> list[dict[str, Any]]: ...
    async def list_poses(self) -> list[dict[str, Any]]: ...
    async def play_behavior(
        self,
        behavior_id: str,
        *,
        idempotency_key: str,
        speed: float | None = None,
        repeat: int | Literal["until_stopped"] | None = None,
    ) -> dict[str, Any]: ...
    async def go_pose(
        self, pose_id: str, *, idempotency_key: str, transition: Transition = "normal"
    ) -> dict[str, Any]: ...
    async def go_rest(self, *, idempotency_key: str) -> dict[str, Any]: ...

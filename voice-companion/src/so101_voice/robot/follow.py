"""Existing Studio latest-value Follow Me API; separate from arm and Talk transports."""

from so101_voice.robot.client import RobotAPIClient


class FollowAPI:
    def __init__(self, robot: RobotAPIClient):
        self.robot = robot

    async def enable(self, angle: float) -> None:
        result = await self.robot._request(
            "POST",
            "/api/v1/follow",
            json={"enabled": True, "target_angle_deg": angle, "transition": "normal"},
        )
        if result.body.get("state") in ("rejected", "failed", "cancelled"):
            raise RuntimeError("Follow Me enable refused")

    async def disable(self) -> None:
        await self.robot._request("POST", "/api/v1/follow", json={"enabled": False})

    async def target(self, angle: float) -> None:
        await self.robot._request("PUT", "/api/v1/follow/target", json={"angle_deg": angle})

    async def status(self) -> dict[str, object]:
        return (await self.robot._request("GET", "/api/v1/follow/status")).body

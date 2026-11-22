"""Harmless built-in tools used to exercise the function-calling path end to end.

No tool here can move the robot. Robot tools (list behaviors, play behavior, go pose, Go Rest, Follow
Me) are planned in ``so101_voice.robot.actions`` and are not registered in this delivery.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from so101_voice.tools.registry import Effect, ToolContext, ToolRegistry


class LocalTimeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = Field(
        default=None,
        description="IANA time zone such as 'Europe/Paris'. Omit for the computer's local time.",
        max_length=64,
    )

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown time zone {v!r}") from e
        return v


async def get_local_time(args: LocalTimeArgs, ctx: ToolContext) -> dict[str, str]:
    now = dt.datetime.now(ZoneInfo(args.timezone)) if args.timezone else dt.datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "spoken": now.strftime("%A %d %B, %H:%M"),
        "timezone": args.timezone or str(now.tzinfo),
    }


def register_builtin_tools(registry: ToolRegistry) -> None:
    registry.tool(
        "get_local_time",
        "Get the current date and time. Use it whenever the user asks what time or day it is.",
        LocalTimeArgs,
        effect=Effect.NONE,
        timeout_s=2.0,
    )(get_local_time)

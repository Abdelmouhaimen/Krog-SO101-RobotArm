"""High-level SO-101 tools for the LLM, backed by the BehaviorDirector (existing robot API only).

| Tool | Effect | What it does |
|---|---|---|
| ``list_robot_behaviors`` | READ | idles and reactions available now (optionally refreshed) |
| ``play_robot_behavior`` | PHYSICAL | play a behavior; a reaction's ``next_idle`` loops afterwards |
| ``list_robot_poses`` | READ | named saved poses |
| ``go_to_robot_pose`` | PHYSICAL | move to a named pose and hold it |
| ``robot_go_rest`` | PHYSICAL | the existing Go Rest |
| ``get_robot_status`` | READ | readiness, current behavior, faults, Emergency Hold |

**Not exposed:** raw joints, calibration, torque, serial, recording or editing behaviors, Follow Me,
Talk Mode (speech drives it) and Emergency Hold.

**Validation and safety:**
- Arguments are validated by pydantic, and names are resolved against the live library. An unknown
  name, a wrong kind or an invalid value means **no request is sent**.
- Physical tools need a committed turn: if the user barged in before the request went out, it is
  never sent.
- The function-call id is the robot ``Idempotency-Key``, so a duplicated or retried call does not move
  the robot twice.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from so101_voice.character.director import BehaviorDirector
from so101_voice.character.library import BehaviorLibrary
from so101_voice.tools.registry import Effect, ToolContext, ToolRegistry

NAME = r"^[A-Za-z0-9_. -]{1,64}$"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListBehaviorsArgs(_Args):
    refresh: bool = Field(default=False, description="Reload the library from the robot first.")


class PlayArgs(_Args):
    behavior: str = Field(pattern=NAME, description="Exact name of a recorded behavior.")
    next_idle: str | None = Field(
        default=None,
        pattern=NAME,
        description="For a one-shot reaction: the looping idle to start when it finishes "
        "(choose it to match the mood). Leave empty for an idle.",
    )
    speed: float | None = Field(default=None, ge=0.5, le=2.0, description="Optional; default: saved speed.")
    repeat: int | Literal["until_stopped"] | None = Field(
        default=None, description="Optional 1–10 or until_stopped; default: the saved repetitions."
    )


class PoseArgs(_Args):
    pose: str = Field(pattern=NAME, description="Exact name of a saved pose.")
    transition: Literal["gentle", "normal", "fast"] = "normal"


class NoArgs(_Args):
    pass


def _cancelled(ctx: ToolContext) -> None:
    if ctx.cancelled.is_set():
        raise RuntimeError("the user moved on before this was sent; nothing was done")


def register_robot_tools(
    registry: ToolRegistry, director: BehaviorDirector, library: BehaviorLibrary
) -> None:
    def behavior_enum(schema: dict[str, Any]) -> dict[str, Any]:
        props = schema.get("properties", {})
        names = sorted(library.behaviors)
        idles = [b.name for b in library.idles()]
        if names:
            props["behavior"] = {**props.get("behavior", {}), "enum": names}
        if idles:
            props["next_idle"] = {**props.get("next_idle", {}), "enum": idles}
        return schema

    def pose_enum(schema: dict[str, Any]) -> dict[str, Any]:
        if library.poses:
            schema.get("properties", {}).setdefault("pose", {})["enum"] = sorted(library.poses)
        return schema

    async def list_behaviors(args: ListBehaviorsArgs, ctx: ToolContext) -> dict[str, Any]:
        if args.refresh or not library.loaded:
            await library.refresh()
        return director.describe_library()

    async def play(args: PlayArgs, ctx: ToolContext) -> dict[str, Any]:
        if args.repeat is not None and args.repeat != "until_stopped" and not 1 <= args.repeat <= 10:
            raise ValueError("repeat must be 1..10 or until_stopped")
        _cancelled(ctx)
        return await director.play(
            args.behavior,
            key=ctx.idempotency_key,
            next_idle=args.next_idle,
            speed=args.speed,
            repeat=args.repeat,
        )

    async def list_poses(args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
        return {"poses": sorted(library.poses), "note": "no named poses saved" if not library.poses else ""}

    async def go_pose(args: PoseArgs, ctx: ToolContext) -> dict[str, Any]:
        _cancelled(ctx)
        return await director.go_pose(args.pose, key=ctx.idempotency_key, transition=args.transition)

    async def go_rest(args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
        _cancelled(ctx)
        return await director.go_rest(key=ctx.idempotency_key)

    async def status(args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
        return await director.robot_status()

    registry.tool(
        "list_robot_behaviors",
        "List the recorded SO-101 behaviors you can play now.",
        ListBehaviorsArgs,
        effect=Effect.READ,
        timeout_s=4.0,
    )(list_behaviors)
    registry.tool(
        "play_robot_behavior",
        "Express a physical reaction or mood using one available recorded animation. Looping idles "
        "continue until replaced; one-shot reactions finish before their selected next_idle. "
        "Choose according to intention and emotion, not automatically for every sentence.",
        PlayArgs,
        effect=Effect.PHYSICAL,
        timeout_s=4.0,
        requires_committed_turn=True,
        schema_hook=behavior_enum,
    )(play)
    registry.tool(
        "list_robot_poses", "List the named saved poses.", NoArgs, effect=Effect.READ, timeout_s=4.0
    )(list_poses)
    registry.tool(
        "go_to_robot_pose",
        "Move to an available saved static pose when intentionally needed or requested. "
        "Poses are not substitutes for expressive moving idles.",
        PoseArgs,
        effect=Effect.PHYSICAL,
        timeout_s=4.0,
        requires_committed_turn=True,
        schema_hook=pose_enum,
    )(go_pose)
    registry.tool(
        "robot_go_rest",
        "Move to the robot's actual rest position when intentionally ending activity or explicitly "
        "requested. This is not an ordinary emotional idle.",
        NoArgs,
        effect=Effect.PHYSICAL,
        timeout_s=4.0,
        requires_committed_turn=True,
    )(go_rest)
    registry.tool(
        "get_robot_status",
        "Robot readiness, current behavior, faults and Emergency Hold.",
        NoArgs,
        effect=Effect.READ,
        timeout_s=4.0,
    )(status)

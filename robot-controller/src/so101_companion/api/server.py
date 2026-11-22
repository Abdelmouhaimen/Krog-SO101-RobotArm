"""FastAPI application for the SO-101 HTTP API, ``/api/v1`` (ADR-018).

(No ``from __future__ import annotations`` here: FastAPI must evaluate the route annotations, which use
aliases local to ``create_app``.)

* Every ``/api/v1`` route requires ``Authorization: Bearer <token>``. An unauthenticated request gets
  401 before anything is recorded or dispatched.
* POST handlers validate, record a command, submit it to the ONE controller queue and return at once:
  202 ``running`` / ``queued``, or 409 when the controller refused it. They wait at most
  ``ack_timeout_s`` for the controller's verdict (off the event loop) and NEVER for the motion.
* ``Idempotency-Key``: a retried request returns the original command without dispatching again.
* No route records behaviors, sends raw joint targets, controls motors, writes calibration, connects,
  arms, changes mode or deletes library items.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from so101_companion import __version__
from so101_companion.api.auth import TokenVerifier
from so101_companion.api.service import (
    ApiCommands,
    Conflict,
    ControllerUnavailable,
    IdempotencyConflict,
    InvalidRequest,
    NotFound,
    PlayOptions,
)
from so101_companion.api.video import SimulationVideo, VideoUnavailable
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

Preset = Literal["gentle", "normal", "fast"]
BOUNDARY = "so101frame"


class PlayBody(BaseModel):
    """Optional overrides for this run only (the behavior file is never modified)."""

    model_config = ConfigDict(extra="forbid")

    speed: float | None = Field(
        default=None, ge=policy.PLAYBACK_SPEED_MIN, le=policy.PLAYBACK_SPEED_MAX, description="0.5–2.0×"
    )
    repeat: int | Literal["until_stopped"] | None = Field(
        default=None, description="1–1000 repetitions, or 'until_stopped' (default: the saved setting)"
    )
    getting_into_position: Preset = Field(
        default="normal", description="entry (and resume) transition preset"
    )
    between_repetitions: Preset = Field(default="fast", description="loop bridge preset")
    after_behavior: Literal["saved", "rest", "hold", "pose"] = Field(
        default="saved",
        description="ending after ALL repetitions complete (default: the behavior's saved ending)",
    )
    after_pose_id: str | None = Field(
        default=None, description="named pose id, required for after_behavior='pose'"
    )


class FollowBody(BaseModel):
    """Follow Me on/off. The angle is measured CLOCKWISE seen from above (toward the robot's right) from
    the configured front; Follow Me owns shoulder_pan only."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    target_angle_deg: float | None = Field(default=None, ge=-180.0, le=180.0)
    transition: Preset = Field(default="normal", description="pan tracking preset")


class FollowTargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    angle_deg: float = Field(ge=-180.0, le=180.0, description="clockwise from the front, degrees")


class TalkStartBody(BaseModel):
    """Talk Mode: procedural gripper animation (owns the gripper only). Re-sending it while active renews
    the lease (no second generator)."""

    model_config = ConfigDict(extra="forbid")

    intensity: Literal["gentle", "normal", "excited"] = "normal"
    lease_s: float = Field(default=10.0, gt=0.0, le=60.0, description="auto-stop after this, unless renewed")
    seed: int | None = Field(default=None, description="reproducible animation (tests)")


class GoPoseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transition: Preset = Field(
        default="normal", description="transition preset (Studio 'Getting into position')"
    )


def create_app(
    commands: ApiCommands, verifier: TokenVerifier, video: SimulationVideo | None = None
) -> FastAPI:
    app = FastAPI(
        title="SO-101 Companion API",
        version=f"1 (app {__version__})",
        description=(
            "Reactive control of the SO-101 Behavior Engine and Named Pose Library through the SAME "
            "controller as SO-101 Studio. New motion commands supersede the running one (no queue, no "
            "intermediate rest). Emergency Hold, Torque OFF and faults always take precedence. All "
            "/api/v1 routes need a Bearer token."
        ),
    )
    bearer = HTTPBearer(auto_error=False, description="API token (so101-companion --create-api-token)")

    def authenticated(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        name = verifier.verify(creds.credentials if creds is not None else None)
        if name is None:
            raise HTTPException(
                401, "missing or invalid API token", headers={"WWW-Authenticate": 'Bearer realm="so101"'}
            )
        return name

    Auth = Annotated[str, Depends(authenticated)]
    IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]

    async def finish(rec: dict[str, Any], replay: bool) -> JSONResponse:
        if not replay:
            rec = await anyio.to_thread.run_sync(commands.wait_handled, rec["id"])
        else:
            rec = commands.command(rec["id"])
        body = rec | {"idempotent_replay": replay, "links": {"self": f"/api/v1/commands/{rec['id']}"}}
        if replay:
            code = 200  # the original command; nothing was dispatched again
        elif rec["state"] in ("rejected", "failed"):
            code = 409  # the controller refused it (reason in "detail"); nothing moved
        else:
            code = 202  # accepted: running (or still queued behind a slow serial cycle)
        return JSONResponse(body, status_code=code)

    async def run(fn: Any, *args: Any) -> JSONResponse:
        try:
            rec, replay = fn(*args)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except InvalidRequest as exc:
            raise HTTPException(422, {"problems": exc.problems}) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, {"error": str(exc), "command_id": exc.command_id}) from exc
        except ControllerUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        return await finish(rec, replay)

    def state(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ControllerUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    # ------------------------------------------------------------------ state
    @app.get("/api/v1/status", tags=["state"])
    async def get_status(_: Auth) -> Any:
        """Mode, connection, torque, fault, emergency latch, per-action readiness, active command."""
        return state(commands.status)

    @app.get("/api/v1/behaviors", tags=["library"])
    async def get_behaviors(_: Auth) -> Any:
        return await anyio.to_thread.run_sync(commands.list_behaviors)

    @app.get("/api/v1/poses", tags=["library"])
    async def get_poses(_: Auth) -> Any:
        return await anyio.to_thread.run_sync(commands.list_poses)

    @app.get("/api/v1/commands", tags=["commands"])
    async def get_commands(_: Auth, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> Any:
        return {"commands": commands.commands(limit)}

    @app.get("/api/v1/commands/{command_id}", tags=["commands"])
    async def get_command(command_id: str, _: Auth) -> Any:
        return state(commands.command, command_id)

    # ------------------------------------------------------------------ motion commands
    @app.post("/api/v1/behaviors/{behavior_id}/play", tags=["commands"], status_code=202)
    async def play(behavior_id: str, _: Auth, key: IdemKey = None, body: PlayBody | None = None) -> Any:
        """Play a saved behavior. Supersedes any running motion (behavior, pose move, rest return)."""
        b = body or PlayBody()
        opts = PlayOptions(
            speed=b.speed,
            repeat_count=b.repeat if isinstance(b.repeat, int) else None,
            until_stopped=True if b.repeat == "until_stopped" else None,
            getting_into_position=TransitionProfile(b.getting_into_position),
            between_repetitions=TransitionProfile(b.between_repetitions),
            after_behavior=b.after_behavior,
            after_pose_id=b.after_pose_id,
        )
        return await run(commands.play, behavior_id, opts, key)

    @app.post("/api/v1/poses/{pose_id}/go", tags=["commands"], status_code=202)
    async def go_pose(pose_id: str, _: Auth, key: IdemKey = None, body: GoPoseBody | None = None) -> Any:
        """Move smoothly to a saved pose and hold. Supersedes any running motion."""
        b = body or GoPoseBody()
        return await run(commands.go_pose, pose_id, TransitionProfile(b.transition), key)

    @app.post("/api/v1/rest", tags=["commands"], status_code=202)
    async def go_rest(_: Auth, key: IdemKey = None) -> Any:
        """Return to the designated default rest pose (interrupts any behavior or pose move)."""
        return await run(commands.go_rest, key)

    @app.post("/api/v1/playback/pause", tags=["commands"], status_code=202)
    async def pause(_: Auth, key: IdemKey = None) -> Any:
        return await run(commands.pause, key)

    @app.post("/api/v1/playback/resume", tags=["commands"], status_code=202)
    async def resume(_: Auth, key: IdemKey = None) -> Any:
        return await run(commands.resume, key)

    @app.post("/api/v1/emergency-hold", tags=["commands"], status_code=202)
    async def emergency_hold(_: Auth, key: IdemKey = None) -> Any:
        """Best-effort hold at the measured pose. Latches: API motion stays refused until the operator
        releases it in the Studio."""
        return await run(commands.emergency_hold, key)

    # ------------------------------------------------------------------ Follow Me / Talk Mode (ADR-019)
    @app.post("/api/v1/follow", tags=["follow"], status_code=202)
    async def follow(body: FollowBody, _: Auth, key: IdemKey = None) -> Any:
        """Follow Me ON/OFF. ON: shoulder_pan tracks the target while behaviors and poses keep the other
        joints. OFF (Stop following): pan returns smoothly to the active behavior / pose."""
        return await run(
            commands.follow_set, body.enabled, body.target_angle_deg, TransitionProfile(body.transition), key
        )

    @app.put("/api/v1/follow/target", tags=["follow"], status_code=202)
    async def follow_target(body: FollowTargetBody, _: Auth) -> Any:
        """Latest-value target update (never queued; send 5–20 per second if you like)."""
        return JSONResponse(state(commands.follow_target, body.angle_deg), status_code=202)

    @app.get("/api/v1/follow/status", tags=["follow"])
    async def follow_status(_: Auth) -> Any:
        return state(commands.follow_status)

    @app.websocket("/api/v1/follow/targets")
    async def follow_targets(ws: WebSocket) -> None:
        """Authenticated stream of Follow Me targets: send {"angle_deg": x}; latest value wins. The token
        goes in the Authorization header of the upgrade request (query-string tokens are not accepted).
        Status messages come back at most 5 per second."""
        scheme, _, cred = ws.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or verifier.verify(cred.strip()) is None:
            await ws.close(code=1008)  # policy violation (before accept: the upgrade is refused)
            return
        await ws.accept()
        last_status = 0.0
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    angle = float(msg["angle_deg"])
                    reply: dict[str, Any] = {"type": "ack"} | commands.follow_target(angle)
                except (ValueError, TypeError, KeyError):
                    await ws.send_json(
                        {"type": "error", "error": 'expected {"angle_deg": number in [-180, 180]}'}
                    )
                    continue
                except (Conflict, ControllerUnavailable) as exc:
                    await ws.send_json({"type": "error", "error": str(exc)})
                    continue
                now = time.monotonic()
                if now - last_status >= 0.2:
                    last_status = now
                    await ws.send_json({"type": "status", "follow": commands.follow_status()})
                else:
                    await ws.send_json(reply)
        except WebSocketDisconnect:
            return  # the last target is held (never reset to 0)

    @app.post("/api/v1/talk/start", tags=["talk"], status_code=202)
    async def talk_start(_: Auth, key: IdemKey = None, body: TalkStartBody | None = None) -> Any:
        """Start (or renew) Talk Mode near the start of audio playback."""
        b = body or TalkStartBody()
        return await run(commands.talk_start, b.intensity, b.lease_s, b.seed, key)

    @app.post("/api/v1/talk/stop", tags=["talk"], status_code=202)
    async def talk_stop(_: Auth, key: IdemKey = None) -> Any:
        """Stop Talk Mode (harmless when already off); the gripper returns smoothly to the motion."""
        return await run(commands.talk_stop, key)

    @app.get("/api/v1/talk/status", tags=["talk"])
    async def talk_status(_: Auth) -> Any:
        return state(commands.talk_status)

    # ------------------------------------------------------------------ simulation video
    def need_video() -> SimulationVideo:
        if video is None:
            raise HTTPException(503, "simulation video is not available in this process")
        return video

    @app.get(
        "/api/v1/simulation/frame",
        tags=["video"],
        responses={200: {"content": {"image/jpeg": {}}}},
        response_class=Response,
    )
    async def frame(_: Auth) -> Response:
        """One JPEG of the MuJoCo visualization (rendered on demand)."""
        v = need_video()
        v.subscribe()
        try:
            f = await anyio.to_thread.run_sync(v.wait_frame, 0, 10.0)
        except VideoUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        finally:
            v.unsubscribe()
        if f is None:
            raise HTTPException(504, "no frame rendered in time")
        return Response(f.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get(
        "/api/v1/simulation/stream.mjpeg",
        tags=["video"],
        responses={200: {"content": {f"multipart/x-mixed-replace; boundary={BOUNDARY}": {}}}},
        response_class=StreamingResponse,
    )
    async def stream(request: Request, _: Auth) -> StreamingResponse:
        """MJPEG stream, at most 5 FPS. Rendering runs only while at least one client is connected."""
        v = need_video()

        async def frames() -> AsyncIterator[bytes]:
            last = 0
            v.subscribe()  # the producer runs only while a client is iterating this stream
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        f = await anyio.to_thread.run_sync(v.wait_frame, last, 1.0)
                    except VideoUnavailable:
                        return
                    if f is None:
                        continue
                    last = f.seq
                    yield (
                        (
                            f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(f.jpeg)}\r\n\r\n"
                        ).encode()
                        + f.jpeg
                        + b"\r\n"
                    )
            finally:
                v.unsubscribe()

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store"},
        )

    return app

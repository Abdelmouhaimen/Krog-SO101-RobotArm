"""The API's command layer (ADR-018), independent of FastAPI.

It turns validated API parameters into the SAME controller requests the Studio submits
(``rq.PlayBehavior`` / ``rq.MoveToPose`` / ``rq.StopToRest`` / pause / resume / Emergency Hold), wraps
them in ``rq.Tracked`` and submits them to the ONE controller queue. It never calls the coordinator's
methods directly, never touches the robot, and reads state only from the published snapshot.

Movement semantics are therefore exactly the Studio's: the same plan compiler, transition presets,
loop bridge, per-behavior ending, pause/resume, Go Rest, calibration checks and joint limits.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from so101_companion.behaviors.model import REPEAT_MAX, Behavior, PlaybackSettings
from so101_companion.behaviors.poses import PoseFormatError, pose_problems_for_arm
from so101_companion.behaviors.service import BehaviorService
from so101_companion.behaviors.validate import BehaviorFormatError, settings_problems
from so101_companion.control import requests as rq
from so101_companion.control.commands import IdempotencyConflict
from so101_companion.control.coordinator import ControllerCoordinator
from so101_companion.domain.state import Action, Connection, Mode, Snapshot, Torque
from so101_companion.safety.policy import DEFAULT_BRIDGE_PROFILE, DEFAULT_ENTRY_PROFILE, TransitionProfile

API_VERSION = "1"
SNAPSHOT_MAX_AGE_S = 2.0  # older published snapshot -> the controller thread is not running


class NotFound(LookupError):
    pass


class InvalidRequest(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class ControllerUnavailable(RuntimeError):
    pass


class Conflict(RuntimeError):
    """The request is valid but does not apply in the current state (HTTP 409)."""


__all__ = [
    "ApiCommands",
    "Conflict",
    "ControllerUnavailable",
    "IdempotencyConflict",
    "InvalidRequest",
    "NotFound",
    "PlayOptions",
]


@dataclass(frozen=True)
class PlayOptions:
    """Request-level overrides. ``None`` = the behavior's saved setting. They are used for THIS run
    only and never written to the behavior file."""

    speed: float | None = None
    repeat_count: int | None = None
    until_stopped: bool | None = None
    getting_into_position: TransitionProfile = DEFAULT_ENTRY_PROFILE
    between_repetitions: TransitionProfile = DEFAULT_BRIDGE_PROFILE
    after_behavior: str = "saved"  # saved | rest | hold | pose
    after_pose_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "speed": self.speed,
            "repeat_count": self.repeat_count,
            "until_stopped": self.until_stopped,
            "getting_into_position": self.getting_into_position.value,
            "between_repetitions": self.between_repetitions.value,
            "after_behavior": self.after_behavior,
            "after_pose_id": self.after_pose_id,
        }


class ApiCommands:
    def __init__(
        self,
        coordinator: ControllerCoordinator,
        behaviors: BehaviorService,
        *,
        allow_physical_control: bool = False,
        ack_timeout_s: float = 0.25,
    ) -> None:
        self.coordinator = coordinator
        self.behaviors = behaviors
        self.ledger = coordinator.commands
        self.allow_physical_control = allow_physical_control
        self.ack_timeout_s = ack_timeout_s

    # ------------------------------------------------------------------ state
    def snapshot(self) -> Snapshot:
        snap = self.coordinator.published.get()
        if snap is None:
            raise ControllerUnavailable("the controller has not published any state yet")
        return snap

    def controller_running(self, snap: Snapshot) -> bool:
        return self.coordinator.clock() - snap.t_mono <= SNAPSHOT_MAX_AGE_S

    def readiness(self, snap: Snapshot) -> dict[str, dict[str, Any]]:
        """Per-action readiness for API requests, with the first blocking reason (advisory: the
        controller re-checks everything when the request is handled)."""
        acts = snap.available_actions
        ex = snap.extra
        hw = snap.mode != Mode.SIM_ONLY and bool(ex.get("robot"))
        latched = ex.get("emergency_latched")
        gate: str | None = None
        if not self.controller_running(snap):
            gate = "the controller thread is not running"
        elif snap.fault is not None:
            gate = f"fault {snap.fault.code} is latched — the operator must recover it in the Studio"
        elif latched:
            gate = f"Emergency Hold is latched ({latched}) — the operator must release it in the Studio"
        elif hw and not self.allow_physical_control:
            gate = "physical API control is disabled ([api] allow_physical_control = false)"

        def item(action: Action, why_not: str, *, gated: bool = True) -> dict[str, Any]:
            if gated and gate:
                return {"ok": False, "reason": gate}
            if action in acts:
                return {"ok": True, "reason": None}
            return {"ok": False, "reason": why_not}

        play_why = (
            "Play needs Simulation → Real (the real arm, or --fake-hardware); the operator selects it in "
            "the Studio"
            if snap.mode != Mode.SIM_TO_REAL
            else _hw_reason(snap)
        )
        return {
            "play_behavior": item(Action.PLAY_BEHAVIOR, play_why),
            "go_pose": item(Action.MOVE_TO_POSE, _hw_reason(snap)),
            "go_rest": item(Action.STOP_TO_REST, _hw_reason(snap)),
            "pause": item(Action.PAUSE_BEHAVIOR, "no behavior is playing on the arm"),
            "resume": item(Action.RESUME_BEHAVIOR, "no paused behavior"),
            "emergency_hold": item(Action.EMERGENCY_HOLD, "no robot connected", gated=False),
            "follow": item(Action.FOLLOW, _overlay_reason(snap)),
            "talk": item(Action.TALK, _overlay_reason(snap)),
        }

    def status(self) -> dict[str, Any]:
        snap = self.snapshot()
        ex = snap.extra
        running = self.controller_running(snap)
        return {
            "api_version": API_VERSION,
            "time_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "controller": {
                "running": running,
                "snapshot_age_s": round(max(0.0, self.coordinator.clock() - snap.t_mono), 3),
                "tick_hz": round(snap.tick_hz_actual, 2),
                "cycle_ms": round(snap.cycle_ms, 3),
                "overruns": snap.overruns,
            },
            "mode": snap.mode.value,
            "connection": snap.connection.value,
            "torque": snap.torque.value,
            "robot": ex.get("robot") or None,
            "calibration_valid": snap.calibration_valid,
            "rest_pose_ready": ex.get("rest_pose") is not None,
            "fault": None if snap.fault is None else {"code": snap.fault.code, "details": snap.fault.details},
            "emergency_hold_latched": ex.get("emergency_latched"),
            "physical_control_enabled": self.allow_physical_control,
            "ready": self.readiness(snap),
            "active_command": ex.get("active_command"),
            "behavior": ex.get("behavior"),
            "pose_move": ex.get("pose_move"),
            "rest_progress": ex.get("rest_progress"),
            "message": snap.message,
            "pose": {
                "displayed": snap.displayed.as_json(),
                "actual": snap.actual.as_json(),
            },
        }

    # ------------------------------------------------------------------ libraries
    def list_behaviors(self) -> dict[str, Any]:
        summaries, problems = self.behaviors.list_behaviors()
        items = []
        for s in summaries:
            try:
                b = self.behaviors.load(s.id)
            except (BehaviorFormatError, OSError):
                continue
            settings = b.settings.to_json()
            direction = self.behaviors.behavior_direction(b)
            items.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "origin": s.origin.value,
                    "revision": s.revision,
                    "duration_s": round(s.duration_s, 3),
                    "updated_at_utc": s.updated_at_utc,
                    "saved_settings": settings,
                    "follow_direction": None if direction is None else direction.to_json(),
                }
            )
        return {"behaviors": items, "unreadable_files": len(problems)}

    def list_poses(self) -> dict[str, Any]:
        poses, problems = self.behaviors.list_poses()
        snap = self.coordinator.published.get()
        fp = bounds = None
        if snap is not None and snap.mode != Mode.SIM_ONLY:
            fp = snap.extra.get("calibration_fingerprint")
            bounds = snap.extra.get("hw_bounds")
        items = []
        for p in poses:
            usable = pose_problems_for_arm(p.target(), fp, bounds) if fp else []
            direction = self.behaviors.pose_direction(p.target())
            items.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "source": p.source.value,
                    "pose": p.pose,
                    "updated_at_utc": p.updated_at_utc,
                    "usable_on_connected_arm": None if not fp else not usable,
                    "problems": usable,
                    "follow_direction": None if direction is None else direction.to_json(),
                }
            )
        return {"poses": items, "unreadable_files": len(problems)}

    # ------------------------------------------------------------------ commands
    def play(
        self, behavior_id: str, opts: PlayOptions, idempotency_key: str | None
    ) -> tuple[dict[str, Any], bool]:
        b = self._behavior(behavior_id)
        settings = self._settings(b, opts)
        params = {
            "behavior_id": b.id,
            "behavior_name": b.name,
            "behavior_revision": b.revision,
            "options": opts.to_json(),
            "effective_settings": settings.to_json(),
        }
        request = self.behaviors.play_request(
            b,
            settings,
            None,  # API requests use no preview (owner decision D7: preview optional)
            confirmed=True,  # authenticated + configured: no dialog (ADR-018); all other checks still run
            enable_torque=False,  # the API never energizes the motors
            entry_profile=opts.getting_into_position,
            bridge_profile=opts.between_repetitions,
        )
        if settings.after_action == "pose" and request.end_pose is None:
            raise InvalidRequest(["the ending pose does not exist"])
        return self._dispatch("play_behavior", params, request, idempotency_key)

    def go_pose(
        self, pose_id: str, transition: TransitionProfile, idempotency_key: str | None
    ) -> tuple[dict[str, Any], bool]:
        try:
            pose = self.behaviors.load_pose(pose_id)
        except (PoseFormatError, ValueError, OSError) as exc:
            raise NotFound(f"no saved pose {pose_id!r}") from exc
        params = {"pose_id": pose.id, "pose_name": pose.name, "transition": transition.value}
        target = pose.target()
        request = rq.MoveToPose(
            target, transition, confirmed=True, direction=self.behaviors.pose_direction(target)
        )
        return self._dispatch("go_pose", params, request, idempotency_key)

    def go_rest(self, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        return self._dispatch("go_rest", {}, rq.StopToRest(), idempotency_key)

    def pause(self, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        return self._dispatch("pause", {}, rq.PauseBehavior(), idempotency_key)

    def resume(self, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        return self._dispatch("resume", {}, rq.ResumeBehavior(), idempotency_key)

    def emergency_hold(self, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        return self._dispatch("emergency_hold", {}, rq.EmergencyHold(), idempotency_key)

    # ------------------------------------------------------------------ overlays (ADR-019)
    def follow_set(
        self,
        enabled: bool,
        target_deg: float | None,
        transition: TransitionProfile,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        params = {"enabled": enabled, "target_deg": target_deg, "transition": transition.value}
        return self._dispatch(
            "follow", params, rq.SetFollow(enabled, target_deg, transition), idempotency_key
        )

    def follow_target(self, angle_deg: float) -> dict[str, Any]:
        """Latest-value target update (no ledger record: 5–20 updates/s must not grow any queue)."""
        snap = self.snapshot()
        follow = snap.extra.get("follow") or {}
        if not follow.get("enabled"):
            raise Conflict("Follow Me is off — enable it first (POST /api/v1/follow)")
        self.coordinator.set_follow_target(angle_deg)  # raises ValueError for a bad angle
        return {"accepted": True, "target_deg": angle_deg, "state": follow.get("state")}

    def follow_status(self) -> dict[str, Any]:
        snap = self.snapshot()
        return dict(snap.extra.get("follow") or {}) | {"ready": self.readiness(snap)["follow"]}

    def talk_start(
        self, intensity: str, lease_s: float, seed: int | None, idempotency_key: str | None
    ) -> tuple[dict[str, Any], bool]:
        params = {"intensity": intensity, "lease_s": lease_s, "seed": seed}
        return self._dispatch("talk_start", params, rq.TalkStart(intensity, lease_s, seed), idempotency_key)

    def talk_stop(self, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        return self._dispatch("talk_stop", {}, rq.TalkStop(), idempotency_key)

    def talk_status(self) -> dict[str, Any]:
        snap = self.snapshot()
        return dict(snap.extra.get("talk") or {}) | {"ready": self.readiness(snap)["talk"]}

    def command(self, command_id: str) -> dict[str, Any]:
        rec = self.ledger.get(command_id)
        if rec is None:
            raise NotFound(f"no command {command_id!r}")
        return self._with_progress(rec)

    def commands(self, limit: int) -> list[dict[str, Any]]:
        return self.ledger.list(limit)

    def wait_handled(self, command_id: str) -> dict[str, Any]:
        """Blocking (call it off the event loop): wait up to ``ack_timeout_s`` for the controller's
        verdict on the request — never for the motion itself."""
        rec = self.ledger.wait_handled(command_id, self.ack_timeout_s)
        assert rec is not None
        return self._with_progress(rec)

    # ------------------------------------------------------------------ internals
    def _dispatch(
        self, kind: str, params: dict[str, Any], request: rq.Request, idempotency_key: str | None
    ) -> tuple[dict[str, Any], bool]:
        snap = self.snapshot()
        if not self.controller_running(snap):
            raise ControllerUnavailable("the controller thread is not running")
        rec, replay = self.ledger.create(kind, "api", params, idempotency_key=idempotency_key)
        if not replay:
            self.coordinator.submit(
                rq.Tracked(request, rec["id"], source="api", physical_allowed=self.allow_physical_control)
            )
        return rec, replay

    def _with_progress(self, rec: dict[str, Any]) -> dict[str, Any]:
        snap = self.coordinator.published.get()
        if snap is None or rec["terminal"]:
            return rec
        active = snap.extra.get("active_command") or {}
        if active.get("id") != rec["id"]:
            return rec
        ex = snap.extra
        progress: dict[str, Any] = {"connection": snap.connection.value}
        if snap.connection == Connection.PLAYING_BEHAVIOR:
            progress["behavior"] = ex.get("behavior")
        elif snap.connection == Connection.MOVING_TO_POSE:
            progress["pose_move"] = ex.get("pose_move")
        elif snap.connection == Connection.RETURNING_TO_REST:
            progress["rest_progress"] = ex.get("rest_progress")
        elif ex.get("sim_motion") is not None:
            progress["simulation_progress"] = ex.get("sim_rest_progress")
        return rec | {"progress": progress}

    def _behavior(self, behavior_id: str) -> Behavior:
        try:
            path = self.behaviors.repository.path_for(behavior_id)  # validates the id (a UUID)
        except ValueError as exc:
            raise NotFound(f"no behavior {behavior_id!r}") from exc
        if not path.is_file():
            raise NotFound(f"no behavior {behavior_id!r}")
        try:
            return self.behaviors.load(behavior_id)
        except BehaviorFormatError as exc:
            raise InvalidRequest([f"behavior {behavior_id!r} cannot be played: {exc}"]) from exc

    def _settings(self, b: Behavior, o: PlayOptions) -> PlaybackSettings:
        s = b.settings
        if o.speed is not None:
            s = replace(s, speed=float(o.speed))
        if o.until_stopped:
            s = replace(s, repeat_count=None)
        elif o.repeat_count is not None:
            if not 1 <= o.repeat_count <= REPEAT_MAX:
                raise InvalidRequest([f"repeat_count must be 1..{REPEAT_MAX}"])
            s = replace(s, repeat_count=o.repeat_count)
        elif o.until_stopped is False and s.repeat_count is None:
            s = replace(s, repeat_count=1)
        if o.after_behavior == "pose":
            if not o.after_pose_id:
                raise InvalidRequest(["after_behavior 'pose' needs after_pose_id"])
            if not self.behaviors.poses.exists(o.after_pose_id):
                raise InvalidRequest([f"no saved pose {o.after_pose_id!r} for the ending"])
            s = replace(s, after_action="pose", after_pose_id=o.after_pose_id)
        elif o.after_behavior in ("rest", "hold"):
            s = replace(s, after_action=o.after_behavior, after_pose_id=None)
        elif o.after_behavior != "saved":
            raise InvalidRequest([f"unknown after_behavior {o.after_behavior!r}"])
        problems = settings_problems(s, b.duration_s)
        if problems:
            raise InvalidRequest(problems)
        return s


def _overlay_reason(snap: Snapshot) -> str:
    if snap.mode == Mode.REAL_TO_SIM:
        return "Real → Simulation (torque off): overlays need Simulation → Real or Simulation Only"
    if snap.mode == Mode.SIM_TO_REAL and snap.torque != Torque.ON:
        return f"torque is {snap.torque.value}"
    rec = snap.extra.get("recording") or {}
    if rec.get("state") == "recording":
        return "a recording is in progress"
    return f"not available while {snap.connection.value}"


def _hw_reason(snap: Snapshot) -> str:
    if snap.mode == Mode.SIM_TO_REAL:
        if snap.torque != Torque.ON:
            return f"torque is {snap.torque.value} — the operator must enable it in the Studio"
        if snap.extra.get("rest_pose") is None:
            return "no valid rest pose for this calibration"
        return f"not available while {snap.connection.value}"
    if snap.mode == Mode.REAL_TO_SIM:
        return "Real → Simulation (torque off, hand guidance): motion is not available"
    return f"not available in {snap.mode.value}/{snap.connection.value}"

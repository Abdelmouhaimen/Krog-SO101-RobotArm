"""UI-independent Behavior Engine entry points (owner requirement 6; ADR-013).

The Qt panel uses this, and a future Milestone 3 tool layer could too — by stable behavior ID only.
Nothing here talks to hardware: physical effects happen exclusively through the controller's request
queue (``ControllerCoordinator.submit``), where every M1 permission, clamp and fault rule applies.
There is deliberately NO method that sends raw joint positions. M3 / agent / MCP are not implemented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from so101_companion.behaviors.direction import Direction
from so101_companion.behaviors.model import Behavior, Origin, PlaybackSettings, Sample
from so101_companion.behaviors.plan import compile_plan
from so101_companion.behaviors.poses import NamedPose, PoseFormatError, PoseRepository, PoseSource, PoseTarget
from so101_companion.behaviors.preview import PreviewApproval, PreviewSession
from so101_companion.behaviors.recorder import Recording
from so101_companion.behaviors.repository import (
    BehaviorRepository,
    BehaviorSummary,
    LoadProblem,
    new_behavior,
)
from so101_companion.behaviors.validate import BehaviorFormatError, settings_problems
from so101_companion.control import requests as rq
from so101_companion.control.coordinator import ControllerCoordinator
from so101_companion.domain.state import Mode, Snapshot
from so101_companion.safety.policy import DEFAULT_BRIDGE_PROFILE, DEFAULT_ENTRY_PROFILE, TransitionProfile


@dataclass(frozen=True)
class PreparedPreview:
    session: PreviewSession
    provisional_reason: str | None  # why this preview cannot authorize a real run (None = it can)


class BehaviorService:
    def __init__(
        self,
        coordinator: ControllerCoordinator,
        repository: BehaviorRepository,
        app_version: str | None = None,
        poses: PoseRepository | None = None,
        directions: Any = None,
    ) -> None:
        self.coordinator = coordinator
        self.repository = repository
        self.app_version = app_version
        # Follow Me (ADR-019): FK direction analyzer with its own cache (load/edit time, never per tick)
        self.directions = directions
        # named poses live next to the behavior library (``poses/`` beside ``behaviors/``), gitignored
        self.poses = poses or PoseRepository(repository.directory.parent / "poses")

    # ------------------------------------------------------------------------- library
    def list_behaviors(self) -> tuple[list[BehaviorSummary], list[LoadProblem]]:
        return self.repository.list()

    def load(self, behavior_id: str) -> Behavior:
        return self.repository.load(behavior_id)

    def rename(self, b: Behavior, name: str, description: str) -> Behavior:
        return self.repository.rename(b, name, description)

    def set_settings(self, b: Behavior, settings: PlaybackSettings) -> Behavior:
        return self.repository.set_settings(b, settings)

    def set_after(self, b: Behavior, action: str, pose_id: str | None = None) -> Behavior:
        """Save the behavior's "After behavior" ending in its file (new revision; ADR-017)."""
        new = replace(b.settings, after_action=action, after_pose_id=pose_id if action == "pose" else None)
        problems = settings_problems(new, b.duration_s)
        if action == "pose" and pose_id is not None and not self.poses.exists(pose_id):
            problems.append("the selected pose does not exist")
        if problems:
            raise BehaviorFormatError(problems)
        details = {"before": b.settings.to_json()["after"], "after": new.to_json()["after"]}
        return self.repository.update(b, "after_behavior", details, settings=new)

    def resolve_end_pose(self, settings: PlaybackSettings) -> PoseTarget | None:
        """The named pose a behavior ends at (None if the ending is not a pose or the pose is gone)."""
        if settings.after_action != "pose" or not settings.after_pose_id:
            return None
        try:
            return self.poses.load(settings.after_pose_id).target()
        except PoseFormatError:
            return None

    # ------------------------------------------------------------------------- directions (ADR-019)
    def behavior_direction(self, b: Behavior, settings: PlaybackSettings | None = None) -> Direction | None:
        """Representative facing direction of the behavior's effective (trimmed) recording; cached by
        id, revision, trim, calibration and model identity. Behavior files are never modified."""
        if self.directions is None:
            return None
        result: Direction = self.directions.analyze_behavior(b, settings or b.settings)
        return result

    def pose_direction(self, target: PoseTarget) -> Direction | None:
        if self.directions is None:
            return None
        key = (target.id, tuple(round(v, 4) for v in target.pose.values()))
        result: Direction = self.directions.analyze_pose(target.pose, f"pose '{target.name}'", key)
        return result

    # ------------------------------------------------------------------------- named poses (ADR-017)
    def list_poses(self) -> tuple[list[NamedPose], list[str]]:
        return self.poses.list()

    def load_pose(self, pose_id: str) -> NamedPose:
        return self.poses.load(pose_id)

    def rename_pose(self, pose_id: str, name: str) -> NamedPose:
        return self.poses.rename(pose_id, name)

    def delete_pose(self, pose_id: str) -> None:
        self.poses.delete(pose_id)

    def save_current_pose(self, name: str, snap: Snapshot) -> NamedPose:
        """Save the arm's measured pose (hardware modes, fresh reading; clamped to the calibrated bounds
        like the rest pose, with the calibration fingerprint) or, in Simulation Only, the 3D arm's pose."""
        ex = snap.extra
        real = (
            snap.mode != Mode.SIM_ONLY
            and snap.actual.values is not None
            and not snap.actual.stale
            and ex.get("hw_bounds") is not None
            and ex.get("calibration_fingerprint")
        )
        if real:
            assert snap.actual.values is not None
            bounds = ex["hw_bounds"]
            values, clamped = {}, []
            for n, v in snap.actual.values.items():
                lo, hi = bounds[n]
                values[n] = min(max(v, lo), hi)
                if values[n] != v:
                    clamped.append(n)
            return self.poses.create(
                name,
                values,
                PoseSource.REAL_ARM,
                calibration_fingerprint=ex["calibration_fingerprint"],
                clamped_joints=tuple(clamped),
            )
        source = snap.displayed if snap.displayed.values is not None else snap.requested
        if source.values is None:
            raise PoseFormatError(["no pose available to save"])
        return self.poses.create(name, source.values, PoseSource.SIMULATION)

    def move_to_pose(self, pose_id: str, profile: TransitionProfile, *, confirmed: bool) -> None:
        """Smooth move to a named pose (real arm: validated against the current calibration first)."""
        target = self.poses.load(pose_id).target()
        self.coordinator.submit(
            rq.MoveToPose(target, profile, confirmed=confirmed, direction=self.pose_direction(target))
        )

    def set_keyframes(self, b: Behavior, keyframes: tuple[Sample, ...]) -> Behavior:
        return self.repository.set_keyframes(b, keyframes)

    def delete(self, behavior_id: str) -> None:
        self.repository.delete(behavior_id)

    # ------------------------------------------------------------------------- authoring
    def start_recording(self, origin: Origin) -> None:
        self.coordinator.submit(rq.StartRecording(origin))

    def stop_recording(self) -> None:
        self.coordinator.submit(rq.StopRecording())

    def take_recording(self) -> Recording | None:
        return self.coordinator.take_recording()

    def save_recording(self, rec: Recording, name: str, description: str = "") -> Behavior:
        if not rec.valid:
            raise BehaviorFormatError(rec.problems(), "recording not playable")
        b = new_behavior(
            name or "untitled",
            rec.origin,
            rec.samples,
            description=description,
            calibration_fingerprint=rec.calibration_fingerprint,
            calibration_robot_id=rec.calibration_robot_id,
            app_version=self.app_version,
        )
        return self.repository.create(b)

    def create_keyframe_behavior(
        self, name: str, keyframes: list[tuple[float, Mapping[str, float]]], description: str = ""
    ) -> Behavior:
        samples = tuple(Sample(float(t), requested=dict(p)) for t, p in keyframes)
        b = new_behavior(
            name, Origin.SIM_KEYFRAMES, samples, description=description, app_version=self.app_version
        )
        return self.repository.create(b)

    # ------------------------------------------------------------------------- preview / play
    @staticmethod
    def hardware_context(snap: Snapshot) -> tuple[bool, str | None]:
        """Can a preview made from this snapshot authorize a REAL run? (owner D3)"""
        ex = snap.extra
        if not snap.hardware_authorized or ex.get("hw_bounds") is None:
            return False, "not connected to the arm: simulation-only preview (cannot authorize a real run)"
        if not snap.calibration_valid or not ex.get("calibration_fingerprint"):
            return False, "calibration not validated: preview is provisional"
        if snap.fault is not None:
            return False, f"fault {snap.fault.code} latched: preview is provisional"
        if snap.mode == Mode.SIM_ONLY:
            return (
                False,
                "SIM_ONLY does not read the arm: select REAL_TO_SIM or SIM_TO_REAL to preview from it",
            )
        if snap.actual.values is None or snap.actual.stale:
            return False, "no fresh measured pose: preview is provisional"
        if ex.get("rest_pose") is None:
            return False, "no valid rest pose for this calibration: record or adopt one first"
        return True, None

    def prepare_preview(
        self,
        b: Behavior,
        settings: PlaybackSettings,
        snap: Snapshot,
        *,
        entry_profile: TransitionProfile = DEFAULT_ENTRY_PROFILE,
        bridge_profile: TransitionProfile = DEFAULT_BRIDGE_PROFILE,
    ) -> PreparedPreview:
        hw, reason = self.hardware_context(snap)
        ex = snap.extra
        if hw:
            assert snap.actual.values is not None
            start: Mapping[str, float] = snap.actual.values
            bounds: Any = ex["hw_bounds"]
            fingerprint: str | None = ex["calibration_fingerprint"]
            rest_goal: dict[str, float] | None = dict(ex["rest_pose"]["pose"])
        else:
            source = snap.displayed if snap.displayed.values is not None else snap.requested
            assert source.values is not None
            start, bounds, fingerprint = source.values, None, None
            rest = ex.get("rest_pose")
            rest_goal = dict(rest["pose"]) if rest else None
        end_pose = self.resolve_end_pose(settings)
        if settings.after_action == "pose" and end_pose is None:
            raise BehaviorFormatError(
                ["the behavior ends at a saved pose that no longer exists — choose another 'After behavior'"]
            )
        plan = compile_plan(
            b,
            settings,
            start,
            bounds=bounds,
            calibration_fingerprint=fingerprint,
            entry_profile=entry_profile,
            bridge_profile=bridge_profile,
        )
        session = PreviewSession(
            plan,
            rest_goal,
            hardware_context=hw,
            after=settings.after_action,
            end_pose_goal=None if end_pose is None else end_pose.pose,
            end_pose_name="" if end_pose is None else end_pose.name,
        )
        return PreparedPreview(session, reason)

    def play(
        self,
        b: Behavior,
        settings: PlaybackSettings,
        approval: PreviewApproval | None,
        *,
        confirmed: bool,
        enable_torque: bool,
        entry_profile: TransitionProfile = DEFAULT_ENTRY_PROFILE,
        bridge_profile: TransitionProfile = DEFAULT_BRIDGE_PROFILE,
    ) -> None:
        self.coordinator.submit(
            self.play_request(
                b,
                settings,
                approval,
                confirmed=confirmed,
                enable_torque=enable_torque,
                entry_profile=entry_profile,
                bridge_profile=bridge_profile,
            )
        )

    def play_request(
        self,
        b: Behavior,
        settings: PlaybackSettings,
        approval: PreviewApproval | None,
        *,
        confirmed: bool,
        enable_torque: bool,
        entry_profile: TransitionProfile = DEFAULT_ENTRY_PROFILE,
        bridge_profile: TransitionProfile = DEFAULT_BRIDGE_PROFILE,
    ) -> rq.PlayBehavior:
        """The ONE way a play request is built (Studio and API): resolved ending pose and the precomputed
        Follow Me directions of the behavior and of its ending pose."""
        end_pose = self.resolve_end_pose(settings)
        return rq.PlayBehavior(
            b,
            settings,
            approval,
            confirmed=confirmed,
            enable_torque=enable_torque,
            entry_profile=entry_profile,
            bridge_profile=bridge_profile,
            end_pose=end_pose,
            direction=self.behavior_direction(b, settings),
            end_pose_direction=None if end_pose is None else self.pose_direction(end_pose),
        )

    def duplicate(self, b: Behavior, name: str | None = None) -> Behavior:
        """A new behavior (new id, revision 1) with the same samples, origin, calibration and settings."""
        copy = new_behavior(
            name or f"{b.name} copy",
            b.origin,
            b.samples,
            description=b.description,
            calibration_fingerprint=b.calibration_fingerprint,
            calibration_robot_id=b.calibration_robot_id,
            app_version=self.app_version,
        )
        created = self.repository.create(copy)
        if b.settings != created.settings:
            created = self.repository.set_settings(created, b.settings)
        return created

    def stop(self) -> None:
        """Normal interruption: the controller cancels the behavior and returns to rest."""
        self.coordinator.submit(rq.StopBehavior())

    def pause(self) -> None:
        """Hold the arm at its measured pose and freeze the timeline (never a return to rest)."""
        self.coordinator.submit(rq.PauseBehavior())

    def resume(self) -> None:
        """Fresh read, then a smooth move back onto the trajectory, then continue."""
        self.coordinator.submit(rq.ResumeBehavior())

    @staticmethod
    def state(snap: Snapshot) -> dict[str, Any]:
        return {
            "behavior": snap.extra.get("behavior"),
            "recording": snap.extra.get("recording"),
            "connection": snap.connection.value,
            "torque": snap.torque.value,
        }

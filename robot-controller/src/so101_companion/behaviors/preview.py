"""MuJoCo preview of the exact effective trajectory (M2-REQ-12, owner D3; ADR-013). Pure, no Qt.

A ``PreviewSession`` plays a compiled ``PlaybackPlan`` on an ideal clock for display only — it never
issues hardware targets. It shows: entry transition from the measured start pose, the content, every
loop bridge (for "until stopped": two cycles and the bridge between them) and the planned return to
the rest pose. Only a preview that ran to the END, compiled with hardware context (calibrated bounds,
calibration fingerprint, fresh measured start pose), yields a ``PreviewApproval``. An approval is
single-use (one run) and the controller re-checks every field of it before moving.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from so101_companion.behaviors.model import PlaybackSettings, Pose
from so101_companion.behaviors.plan import PlaybackPlan, SmoothTransition
from so101_companion.devices.rest_pose import RestTrajectory
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile


@dataclass(frozen=True)
class PreviewApproval:
    behavior_id: str
    behavior_revision: int
    settings: PlaybackSettings
    content_token: str
    start_pose: Pose
    calibration_fingerprint: str
    bounds: Mapping[str, tuple[float, float]]
    nonce: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class PreviewSession:
    plan: PlaybackPlan
    rest_goal: Pose | None
    hardware_context: bool  # compiled from a connected arm (bounds + fingerprint + measured start)
    completed: bool = False
    cancelled: bool = False
    # ADR-017 "After behavior": rest (default) | hold | pose (``end_pose_goal`` with ``end_pose_name``)
    after: str = "rest"
    end_pose_goal: Pose | None = None
    end_pose_name: str = ""

    def __post_init__(self) -> None:
        reps = self.plan.repeat_count if self.plan.repeat_count is not None else 2
        self.behavior_s = self.plan.nominal_duration_for(reps)
        self.rest: RestTrajectory | None = None
        self.end_move: SmoothTransition | None = None
        finite = self.plan.repeat_count is not None  # "until stopped" ends by interruption -> rest
        if finite and self.after == "hold":
            return
        if finite and self.after == "pose" and self.end_pose_goal is not None:
            profile: TransitionProfile = self.plan.entry_profile
            self.end_move = SmoothTransition.plan(self.plan.end_pose(), dict(self.end_pose_goal), profile)
            return
        if self.rest_goal is not None:
            # every repetition (also the 2nd shown for "until stopped") ends at the content's last pose
            self.rest = RestTrajectory.plan(
                self.plan.end_pose(),
                dict(self.rest_goal),
                self.behavior_s,
                policy.REST_RETURN_SPEED,
                policy.REST_MIN_DURATION_S,
            )

    @property
    def duration(self) -> float:
        if self.end_move is not None:
            return self.behavior_s + self.end_move.duration
        return self.behavior_s + (self.rest.duration if self.rest is not None else 0.0)

    def pose_at(self, t: float) -> tuple[Pose, str]:
        """Pose and a label for preview time ``t`` (seconds)."""
        if t < self.behavior_s:
            p = self.plan.at(t)
            label = {
                "entry": "entry transition",
                "content": f"behavior (repetition {p.repetition})",
                "bridge": f"loop bridge after repetition {p.repetition}",
                "done": "behavior end",
            }[p.segment]
            return p.pose, label
        if self.end_move is not None:
            return self.end_move.at(t - self.behavior_s), f"move to pose '{self.end_pose_name}' (planned)"
        if self.rest is not None:
            return self.rest.at(t), "return to rest (planned)"
        if self.after == "hold":
            return self.plan.end_pose(), "hold last position (planned)"
        return self.plan.end_pose(), "behavior end"

    def step(self, t: float) -> tuple[Pose, str]:
        if t >= self.duration and not self.cancelled:
            self.completed = True
        return self.pose_at(min(t, self.duration))

    def cancel(self) -> None:
        self.cancelled = True

    def approval(self) -> PreviewApproval | None:
        """The run authorization; None unless the full preview ran with hardware context."""
        if not (self.completed and not self.cancelled and self.hardware_context):
            return None
        bounds = self.plan.audit.bounds
        fp = self.plan.calibration_fingerprint
        if bounds is None or fp is None:
            return None
        return PreviewApproval(
            self.plan.behavior_id,
            self.plan.behavior_revision,
            self.plan.settings,
            self.plan.content_token,
            dict(self.plan.start_pose),
            fp,
            dict(bounds),
        )

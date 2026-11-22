"""Operator requests posted by the UI (or scripts) to the single controller owner."""

from __future__ import annotations

from dataclasses import dataclass

from so101_companion.behaviors.direction import Direction
from so101_companion.behaviors.model import Behavior, Origin, PlaybackSettings
from so101_companion.behaviors.poses import PoseTarget
from so101_companion.behaviors.preview import PreviewApproval
from so101_companion.domain.state import Mode
from so101_companion.safety.policy import DEFAULT_BRIDGE_PROFILE, DEFAULT_ENTRY_PROFILE, TransitionProfile


@dataclass(frozen=True)
class SetMode:
    mode: Mode


@dataclass(frozen=True)
class Connect:
    port: str
    calibration_path: str
    confirmed: bool = False  # operator acknowledged that LeRobot configure() may toggle torque


@dataclass(frozen=True)
class EnableMovement:
    confirmed: bool = False


@dataclass(frozen=True)
class Disarm:
    pass


@dataclass(frozen=True)
class EmergencyHold:
    pass


@dataclass(frozen=True)
class TorqueOff:
    confirmed: bool = False  # operator acknowledged the arm may fall


@dataclass(frozen=True)
class EnableTorque:
    confirmed: bool = False


@dataclass(frozen=True)
class Recover:
    confirmed: bool = False


@dataclass(frozen=True)
class Disconnect:
    confirmed: bool = False  # operator saw the disable_torque_on_disconnect consequence


@dataclass(frozen=True)
class RecordRestPose:
    confirmed: bool = False  # operator holds the arm in its rest pose; measured pose is saved (clamped)


@dataclass(frozen=True)
class StopToRest:
    """Normal Stop: timed return to the recorded physical rest pose, then hold (torque on)."""


@dataclass(frozen=True)
class AdoptRestPose:
    """D5: use a legacy rest pose (no calibration fingerprint) with the CURRENT calibration. The file is
    backed up, then rewritten with the fingerprint. Only on explicit operator confirmation."""

    confirmed: bool = False


@dataclass(frozen=True)
class StartRecording:
    origin: Origin  # REAL_HAND_GUIDED (torque OFF, read-only) or SIM_SLIDERS (SIM_ONLY)


@dataclass(frozen=True)
class StopRecording:
    pass


@dataclass(frozen=True)
class PlayBehavior:
    """Real playback. Needs explicit confirmation. A completed hardware preview is optional (owner
    decision D7); when one is given it must match exactly and is single use.
    ``enable_torque``: the operator's confirmation also covered energizing the motors (goal := fresh
    measured pose first, then torque on)."""

    behavior: Behavior
    settings: PlaybackSettings
    approval: PreviewApproval | None
    confirmed: bool = False
    enable_torque: bool = False
    entry_profile: TransitionProfile = DEFAULT_ENTRY_PROFILE  # "Getting into position" (also resume)
    bridge_profile: TransitionProfile = DEFAULT_BRIDGE_PROFILE  # "Between repetitions"
    end_pose: PoseTarget | None = None  # resolved named pose for settings.after_action == "pose"
    # Follow Me (ADR-019): precomputed representative directions (None -> the controller asks its own
    # direction analyzer once, at start; never per tick)
    direction: Direction | None = None
    end_pose_direction: Direction | None = None


@dataclass(frozen=True)
class StopBehavior:
    """Normal interruption: cancel the behavior and run the M1 return to rest."""


@dataclass(frozen=True)
class EnterMode:
    """Studio mode workflow (one confirmed operator choice), executed atomically by the controller:

    * SIM_ONLY: stop motion targets (stay connected, torque unchanged).
    * REAL_TO_SIM: connect if needed -> mirror mode -> torque OFF (the confirmation told the operator to
      support the arm).
    * SIM_TO_REAL: connect if needed -> control mode -> if torque is not ON: fresh read, goal := present,
      torque on -> enable movement.

    Any failing step stops the sequence and leaves the last safe state, reported; nothing is retried.
    ``port``/``calibration_path`` are only used when a connection is needed."""

    mode: Mode
    port: str = ""
    calibration_path: str = ""
    confirmed: bool = False


@dataclass(frozen=True)
class MoveToPose:
    """Smooth move (the given preset) from the freshly measured pose to a named pose, then hold. On the
    real arm the pose is validated against the current calibration first; in Simulation Only it moves the
    3D arm only."""

    target: PoseTarget
    profile: TransitionProfile = DEFAULT_ENTRY_PROFILE
    confirmed: bool = False
    direction: Direction | None = None  # Follow Me reference of the pose (ADR-019)


@dataclass(frozen=True)
class FinishBehavior:
    """Graceful end: let the current repetition complete, then run the behavior's own "After behavior"
    ending (as if that repetition were the last). Not an interruption."""


@dataclass(frozen=True)
class PauseBehavior:
    """Physical playback: hold at the freshly measured pose and suspend the timeline (no return to rest)."""


@dataclass(frozen=True)
class ResumeBehavior:
    """Resume a paused playback: fresh read, then a smooth move (entry preset) back onto the trajectory."""


@dataclass(frozen=True)
class SetFollow:
    """Follow Me on/off (ADR-019). ON: shoulder_pan is owned by Follow Me and tracks ``target_deg`` (or the
    last target), measured clockwise from the configured front. OFF ("Stop following"): pan is handed
    back smoothly to the active behavior / pose. Turning it ON while it is SUSPENDED re-evaluates the
    reference from the current pose (the explicit operator action of D5)."""

    enabled: bool
    target_deg: float | None = None
    profile: TransitionProfile = TransitionProfile.NORMAL


@dataclass(frozen=True)
class TalkStart:
    """Talk Mode on (or renew its lease / change its intensity while on): procedural gripper animation."""

    intensity: str = "normal"
    lease_s: float = 10.0
    seed: int | None = None


@dataclass(frozen=True)
class TalkStop:
    """Talk Mode off: the gripper returns smoothly to the active behavior / pose. Harmless when off."""


@dataclass(frozen=True)
class SetFrontReference:
    """Studio only: define the physical direction meaning Follow Me angle 0 — from the current facing
    (``from_current``), as a clockwise model-frame bearing (``bearing_deg``), or back to the pan center
    (``reset``). Persisted per calibration and model identity."""

    bearing_deg: float | None = None
    from_current: bool = False
    reset: bool = False


@dataclass(frozen=True)
class Pause:
    pass


@dataclass(frozen=True)
class Resume:
    pass


Request = (
    SetMode
    | Connect
    | EnableMovement
    | Disarm
    | EmergencyHold
    | TorqueOff
    | EnableTorque
    | Recover
    | Disconnect
    | RecordRestPose
    | StopToRest
    | AdoptRestPose
    | StartRecording
    | StopRecording
    | PlayBehavior
    | StopBehavior
    | Pause
    | Resume
    | EnterMode
    | PauseBehavior
    | ResumeBehavior
    | MoveToPose
    | FinishBehavior
    | SetFollow
    | TalkStart
    | TalkStop
    | SetFrontReference
)


@dataclass(frozen=True)
class Tracked:
    """Envelope for a request whose lifecycle is recorded in the command ledger (ADR-018).

    ``command_id`` names a record created beforehand (``CommandLedger.create``). ``source`` is
    ``"studio"`` (a direct operator action, already confirmed in the window) or ``"api"``. For API
    requests the controller itself enforces, at handling time, the physical-control gate
    (``physical_allowed``) and the emergency latch, so a mode change between the HTTP check and the
    controller cannot let a simulation-only command move the real arm."""

    request: Request
    command_id: str
    source: str = "api"
    physical_allowed: bool = False


# Requests whose lifecycle the ledger records (Studio requests are tracked automatically), by kind.
TRACKED_KINDS: dict[type, str] = {
    PlayBehavior: "play_behavior",
    MoveToPose: "go_pose",
    StopToRest: "go_rest",
    StopBehavior: "go_rest",
    PauseBehavior: "pause",
    ResumeBehavior: "resume",
    FinishBehavior: "finish",
    EmergencyHold: "emergency_hold",
    SetFollow: "follow",
    TalkStart: "talk_start",
    TalkStop: "talk_stop",
}
# Requests that start or change motion (gated for the API; Emergency Hold never is).
MOTION_KINDS = frozenset(
    {"play_behavior", "go_pose", "go_rest", "pause", "resume", "finish", "follow", "talk_start"}
)

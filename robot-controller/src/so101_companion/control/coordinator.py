"""ControllerCoordinator — the single owner of mode/authority state and of every hardware call.

Command path per issued tick — FIXED M1 policy (safety/policy.py, ADR-010):

    UI intent (latest only) -> finite 6-joint validation -> calibrated range clamp (5° arm margin,
    gripper 0..100) = STAGED -> RobotPort.send (LeRobot, max_relative_target=None)
    -> returned SENT -> observed ACTUAL -> DISPLAYED pose

There is no step limiter and no measured-relative cap: motion timing/smoothness is the caller's job
(the M2 Behavior Engine trajectories and the rest return). Tracking divergence is a diagnostic only.

M2 (ADR-013): the behavior player is one more MOTION state (PLAYING_BEHAVIOR) that only produces the
REQUESTED pose each tick from a precompiled, previewed plan; clamp, send, telemetry and every fault /
Emergency Hold / Torque Off path stay the M1 ones. Timed trajectories (playback and the return to rest)
use a stall-aware clock: a serial stall delays them, it never makes the next target jump ahead (D1/D4).
The behavior recorder only listens to measured frames (hand-guided, torque OFF) or authored slider
targets (SIM_ONLY); it never sends anything.

Rules enforced here (CONSTITUTION, FR-05/07/11/13/14/15, SR-01..08):
* SIM_ONLY never touches the robot. The robot object is only created by an explicitly confirmed
  Connect request, after the calibration file validated.
* Sends happen only in SIM_TO_REAL + ARMED + calibration_valid + torque ON.
* Any hardware exception during operation latches FAULT_LATCHED: the intent is discarded, no more
  bus calls happen (no polling, no retries, no reconnect), ACTUAL is marked stale and torque UNKNOWN.
  Only explicit, confirmed operator actions (Recover / Torque Off / Emergency Hold / Disconnect)
  may touch the bus afterwards. A later good read never clears the fault by itself.

This class has no threads; ``control.worker`` drives it. Thread-safe entry points for the UI are
:meth:`submit` and :meth:`set_requested` only.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from so101_companion.behaviors.direction import Direction, wrap180
from so101_companion.behaviors.model import Origin
from so101_companion.behaviors.plan import (
    CompatibilityError,
    PlanPoint,
    PlaybackPlan,
    SmoothTransition,
    compile_plan,
    pose_deviation,
)
from so101_companion.behaviors.poses import PoseTarget, pose_problems_for_arm
from so101_companion.behaviors.preview import PreviewApproval
from so101_companion.behaviors.recorder import BehaviorRecorder, Recording
from so101_companion.behaviors.validate import BehaviorFormatError
from so101_companion.config import AppConfig
from so101_companion.control.commands import CommandLedger, CommandState
from so101_companion.control.front import SIM_KEY, FrontStore, MemoryFrontStore
from so101_companion.control.overlays import (
    GRIPPER,
    PAN,
    TALK_LEASE_MAX_S,
    TALK_PRESETS,
    TARGET_DEADBAND_DEG,
    FollowState,
    FrontReference,
    Handoff,
    TalkGenerator,
    TalkState,
    profile_limits,
    reachable_theta,
    solve_pan,
)
from so101_companion.control.timeline import StallAwareClock
from so101_companion.devices.calibration import (
    CalibrationError,
    calibrated_bounds,
    calibration_fingerprint,
    load_calibration,
)
from so101_companion.devices.protocol import RobotPort
from so101_companion.devices.rest_pose import (
    MemoryRestPoseStore,
    RestPose,
    RestPoseError,
    RestPoseStore,
    RestTrajectory,
    now_iso,
    within,
)
from so101_companion.domain.joints import JOINT_NAMES, JointFrame, validate_joint_values
from so101_companion.domain.state import (
    NOT_AVAILABLE,
    Action,
    Connection,
    Fault,
    Mode,
    Snapshot,
    StageValue,
    Torque,
)
from so101_companion.safety import policy
from so101_companion.safety.bounds import InvertedBoundsError, JointBounds
from so101_companion.safety.divergence import detect_runaway
from so101_companion.telemetry.events import TICK_KIND, EventLog

from . import requests as rq

MOTION = {  # targets are sent every tick
    Connection.ARMED,
    Connection.RETURNING_TO_REST,
    Connection.PLAYING_BEHAVIOR,
    Connection.MOVING_TO_POSE,
}
HW_CONNECTED = {
    Connection.CONNECTED_READ_ONLY,
    Connection.ARMED,
    Connection.RETURNING_TO_REST,
    Connection.PLAYING_BEHAVIOR,
    Connection.MOVING_TO_POSE,
    Connection.HOLDING,
    Connection.TORQUE_OFF,
    Connection.FAULT_LATCHED,
}
READ_ONLY_POLLING = {Connection.CONNECTED_READ_ONLY, Connection.HOLDING, Connection.TORQUE_OFF}
# A timed motion is in flight: a replacement continues from the last commanded target (D9, ADR-018).
IN_FLIGHT = {Connection.PLAYING_BEHAVIOR, Connection.MOVING_TO_POSE, Connection.RETURNING_TO_REST}
# Follow Me / Talk Mode may run on the real arm in these states (ADR-019); any other state turns them off.
OVERLAY_STATES = {Connection.ARMED, Connection.HOLDING} | IN_FLIGHT
# Simulation-only rest pose used by Go Rest in SIM_ONLY when no physical rest pose is known (display only;
# clamped to the simulation bounds; never sent to hardware).
SLIDER_SETTLE_S = 0.3  # a slider-reshaped arm gets its Follow Me reference re-measured after this pause
DEFAULT_SIM_REST_POSE = {
    "shoulder_pan": 0.0,
    "shoulder_lift": -95.0,
    "elbow_flex": 90.0,
    "wrist_flex": -85.0,
    "wrist_roll": 0.0,
    "gripper": 2.0,
}

RobotFactory = Callable[[], RobotPort]


class PublishedSnapshot:
    """The latest snapshot published by the controller thread, for readers on OTHER threads (HTTP API,
    video producer). ``coordinator.snapshot()`` itself must only run on the controller thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None

    def set(self, snap: Snapshot) -> None:
        with self._lock:
            self._snap = snap

    def get(self) -> Snapshot | None:
        with self._lock:
            return self._snap


class BehaviorRun:
    """The one active behavior (single owner of the targets while PLAYING_BEHAVIOR)."""

    def __init__(self, plan: PlaybackPlan, clock: StallAwareClock, approval_nonce: str | None) -> None:
        self.plan = plan
        self.clock = clock
        self.approval_nonce = approval_nonce
        self.point: PlanPoint | None = None
        # Studio pause/resume: while paused the arm holds the measured pose and the timeline is frozen;
        # on resume a smooth move (entry preset) brings it back onto the trajectory before it continues.
        self.end_pose: PoseTarget | None = None  # resolved "After behavior" pose (validated at start)
        self.finish_rep: int | None = None  # Finish: the repetition after which the run completes
        self.finish_tau: float | None = None  # ... at this logical time (end of that repetition's content)
        self.paused = False
        self.resume_move: SmoothTransition | None = None
        self.resume_clock: StallAwareClock | None = None
        self.direction: Direction | None = None  # Follow Me reference (fixed for the whole run)
        self.end_direction: Direction | None = None

    def status(self) -> dict[str, Any]:
        p = self.point
        segment = None if p is None else p.segment
        if self.resume_move is not None:
            segment = "resume"
        return {
            "state": "paused" if self.paused else "playing",
            "behavior_id": self.plan.behavior_id,
            "name": self.plan.behavior_name,
            "revision": self.plan.behavior_revision,
            "settings": self.plan.settings.to_json(),
            "segment": segment,
            "repetition": 0 if p is None else p.repetition,
            "repeat_count": self.plan.repeat_count,
            "t_logical_s": round(self.clock.elapsed, 3),
            "nominal_total_s": None
            if self.plan.total_duration is None
            else round(self.plan.total_duration, 3),
            "timeline_delay_ms": round(self.clock.delay_s * 1000.0, 1),
            "finish_after_repetition": self.finish_rep,
            "entry_s": round(self.plan.entry.duration, 3),
            "content_s": round(self.plan.content.duration, 3),
            "bridge_s": round(self.plan.bridge.duration, 3),
        }


class PoseMove:
    """A smooth move to a named pose (direct request or a behavior's ending), then hold (ADR-017)."""

    def __init__(
        self, target: PoseTarget, move: SmoothTransition, clock: StallAwareClock, reason: str
    ) -> None:
        self.target = target
        self.move = move
        self.clock = clock
        self.reason = reason
        self.done = False

    def status(self) -> dict[str, Any]:
        return {
            "pose_id": self.target.id,
            "name": self.target.name,
            "duration_s": round(self.move.duration, 3),
            "progress": round(min(1.0, self.clock.elapsed / max(self.move.duration, 1e-9)), 3),
            "purpose": self.reason,
        }


class ControllerCoordinator:
    def __init__(
        self,
        cfg: AppConfig,
        sim_bounds: JointBounds,
        initial_sim_pose: Mapping[str, float],
        robot_factory: RobotFactory,
        *,
        events: EventLog | None = None,
        clock: Callable[[], float] = time.monotonic,
        rest_store: RestPoseStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.rest_store: RestPoseStore = rest_store or MemoryRestPoseStore()
        self.rest_pose: RestPose | None = None
        self.rest_status: tuple[str, str] = ("none", "")  # none|loaded|needs_adoption|mismatch|rejected
        self._legacy_rest: RestPose | None = None  # D5: fingerprint-less file awaiting explicit adoption
        self.calibration_fp: str | None = None
        self._rest_traj: RestTrajectory | None = None
        self._rest_clock: StallAwareClock | None = None
        self._rest_deadline_s = 0.0  # on the rest trajectory's (stall-aware) logical clock
        # Go Rest / Move to pose in SIM_ONLY (display only, never sent)
        self._sim_rest: RestTrajectory | SmoothTransition | None = None
        self._sim_rest_clock: StallAwareClock | None = None
        self._sim_motion_label = "rest"
        self._pose_move: PoseMove | None = None
        self.pose_move_last_result: str | None = None
        self._period = 1.0 / cfg.app.command_hz
        self.recorder = BehaviorRecorder()
        self._recording_lock = threading.Lock()
        self._finished_recording: Recording | None = None
        self.last_recording_status: dict[str, Any] | None = None
        self._run: BehaviorRun | None = None
        self.behavior_last_result: str | None = None
        self._used_approvals: set[str] = set()
        self.clock = clock
        self.events = events or EventLog(cfg.app.event_ring_size, clock)
        self.sim_bounds = sim_bounds
        self.hw_bounds: JointBounds | None = None
        self._robot_factory = robot_factory
        self.robot: RobotPort | None = None
        self.divergence_warning: str | None = None
        self._divergence_warning_until = 0.0

        self.mode = Mode.SIM_ONLY
        self.connection = Connection.DISCONNECTED
        self.torque = Torque.NOT_APPLICABLE
        self.hardware_authorized = False
        self.calibration_valid = False
        self.paused = False
        self.fault: Fault | None = None
        self.message = "SIM_ONLY — no hardware connected"
        self.port_label = ""

        now = clock()
        pose = validate_joint_values(initial_sim_pose)
        self.requested = StageValue(pose, now)
        self.staged = StageValue(pose, now)
        self.sent = NOT_AVAILABLE
        self.actual = NOT_AVAILABLE
        self.displayed = StageValue(pose, now)
        self.clamp_reasons: tuple[str, ...] = ()
        self.last_success_read_at: float | None = None
        self.last_success_send_at: float | None = None
        self.command_seq = 0
        self.tick_seq = 0
        self.requested_epoch = 0
        self.overruns = 0
        self.cycle_ms = 0.0
        self._tick_times: deque[float] = deque(maxlen=200)
        self._goal_in_flight: dict[str, float] | None = None  # goal sent in the previous ARMED tick
        self._last_moves: dict[str, float] | None = None  # per-joint measured motion over the last tick

        self._intent_lock = threading.Lock()
        self._intent: dict[str, float] | None = None
        self._requests: queue.Queue[rq.Request | rq.Tracked] = queue.Queue()
        self.wake = threading.Event()
        self.on_change: Callable[[], None] = lambda: None
        # ADR-018: command lifecycle (Studio + API), the published snapshot for other threads, and the
        # emergency latch (set by every Emergency Hold; API motion requests are refused while it is set;
        # only a direct Studio operator motion/mode action or a disconnect releases it).
        self.commands = CommandLedger(clock)
        self.commands.listeners.append(self._on_command_update)
        self.published = PublishedSnapshot()
        self.emergency_latched: str | None = None
        self._cmd: tuple[str | None, str] = (None, "studio")  # (command id, source) being handled
        self._cmd_rejected = False
        self._active_is_sim = False
        self._start_origin = "measured"  # how the last takeover chose its start pose (for the audit)
        # ---- Milestone 2.6 overlays (ADR-019): Follow Me owns shoulder_pan, Talk Mode owns the gripper.
        self.directions: Any = None  # DirectionAnalyzer (FK), injected by the app; event-time use only
        self.front_store: FrontStore = MemoryFrontStore()
        self.front: FrontReference | None = None
        self.follow = FollowState()
        self.talk = TalkState()
        self._owner_dir: Direction | None = None  # representative direction of the current motion owner
        self._rest_dir: Direction | None = None
        self._base: dict[str, float] | None = None  # the motion's own target before the overlays
        self._composed: StageValue | None = None  # the last composed ``requested`` (identity check)
        self._overlay_t: float | None = None
        self._follow_lock = threading.Lock()
        self._follow_target: float | None = None  # latest-value slot (HTTP / WebSocket / Studio)
        self._sim_preview: tuple[dict[str, float], Direction | None, bool] | None = None  # Studio preview
        self._sim_preview_dir: Direction | None = None
        # sliders are an owner too: their reference is re-measured once a slider move settles (real-arm
        # finding 2026-11-07: a reference kept from the rest pose pointed a NORMAL arm the opposite way)
        self._owner_from_sliders = False
        self._slider_moved_at: float | None = None
        self.events.emit("startup", mode=self.mode.value, connection=self.connection.value)

    # ===================================================================== thread-safe inputs
    def submit(self, request: rq.Request | rq.Tracked) -> None:
        self._requests.put(request)
        self.wake.set()

    def set_requested(self, values: Mapping[str, float]) -> None:
        """Latest-intent slot (FR-10): newer slider frames replace older ones; never queued."""
        clean = validate_joint_values(values)
        with self._intent_lock:
            self._intent = clean

    def set_follow_target(self, angle_deg: float) -> None:
        """Latest-value Follow Me target (degrees clockwise from the front). Never queued: a newer value
        replaces an older one; the controller reads it once per tick."""
        if not math.isfinite(angle_deg) or not -180.0 <= angle_deg <= 180.0:
            raise ValueError("follow target must be a finite angle in [-180, 180] degrees")
        with self._follow_lock:
            self._follow_target = float(angle_deg)

    def _take_follow_target(self) -> float | None:
        with self._follow_lock:
            t, self._follow_target = self._follow_target, None
        return t

    def set_sim_preview(
        self, pose: Mapping[str, float] | None, direction: Direction | None = None, *, paused: bool = False
    ) -> None:
        """Simulation Only (D6): the Studio's existing timeline preview feeds its pose here while an overlay
        is engaged, so the ONE controller composes Follow Me / Talk Mode onto it (no second player)."""
        value = None if pose is None else (validate_joint_values(pose), direction, paused)
        with self._intent_lock:
            self._sim_preview = value

    def _take_intent(self) -> dict[str, float] | None:
        with self._intent_lock:
            intent, self._intent = self._intent, None
        return intent

    def _discard_intent(self) -> None:
        with self._intent_lock:
            self._intent = None

    def pending_requests(self) -> bool:
        return not self._requests.empty()

    def process_requests(self) -> int:
        n = 0
        while True:
            try:
                req = self._requests.get_nowait()
            except queue.Empty:
                return n
            self.handle(req)
            n += 1

    # ===================================================================== permissions
    def available_actions(self) -> frozenset[Action]:
        c, m = self.connection, self.mode
        has_robot = self.robot is not None
        acts: set[Action] = set()
        if c != Connection.CONNECTING:
            if m != Mode.SIM_ONLY:
                acts.add(Action.SET_MODE_SIM_ONLY)
            hw_readable = {
                Connection.CONNECTED_READ_ONLY,
                Connection.ARMED,
                Connection.RETURNING_TO_REST,
                Connection.PLAYING_BEHAVIOR,
                Connection.MOVING_TO_POSE,
                Connection.HOLDING,
                Connection.TORQUE_OFF,
            }
            if c in hw_readable and m != Mode.REAL_TO_SIM:
                acts.add(Action.SET_MODE_REAL_TO_SIM)
            if c in READ_ONLY_POLLING and m != Mode.SIM_TO_REAL and self.calibration_valid:
                acts.add(Action.SET_MODE_SIM_TO_REAL)
        if c == Connection.DISCONNECTED:
            acts.add(Action.CONNECT)
        if (
            m == Mode.SIM_TO_REAL
            and c in {Connection.CONNECTED_READ_ONLY, Connection.HOLDING}
            and self.calibration_valid
            and self.hardware_authorized
            and self.torque == Torque.ON
            and self.hw_bounds is not None
        ):
            acts.add(Action.ENABLE_MOVEMENT)
        if c in {
            Connection.ARMED,
            Connection.RETURNING_TO_REST,
            Connection.PLAYING_BEHAVIOR,
            Connection.MOVING_TO_POSE,
            Connection.HOLDING,
        }:
            acts.add(Action.DISARM)
        if has_robot and c in {
            Connection.ARMED,
            Connection.RETURNING_TO_REST,
            Connection.PLAYING_BEHAVIOR,
            Connection.MOVING_TO_POSE,
            Connection.HOLDING,
            Connection.FAULT_LATCHED,
        }:
            acts.add(Action.EMERGENCY_HOLD)
        if has_robot and c in {
            Connection.CONNECTED_READ_ONLY,
            Connection.ARMED,
            Connection.RETURNING_TO_REST,
            Connection.PLAYING_BEHAVIOR,
            Connection.MOVING_TO_POSE,
            Connection.HOLDING,
            Connection.FAULT_LATCHED,
        }:
            acts.add(Action.TORQUE_OFF)
        if has_robot and c == Connection.TORQUE_OFF:
            acts.add(Action.ENABLE_TORQUE)
        if has_robot and c == Connection.FAULT_LATCHED:
            acts.add(Action.RECOVER)
        if has_robot and c in HW_CONNECTED:
            acts.add(Action.DISCONNECT)
        if m == Mode.SIM_ONLY:
            acts.add(Action.RESUME if self.paused else Action.PAUSE)
        if m == Mode.SIM_ONLY or (m == Mode.SIM_TO_REAL and c == Connection.ARMED):
            acts.add(Action.MOVE_SLIDERS)
        if (
            has_robot
            and self.calibration_valid
            and self.hw_bounds is not None
            and c
            in {Connection.CONNECTED_READ_ONLY, Connection.HOLDING, Connection.TORQUE_OFF, Connection.ARMED}
        ):
            acts.add(Action.RECORD_REST_POSE)
        if (
            m == Mode.SIM_TO_REAL
            and c in {Connection.ARMED} | IN_FLIGHT
            and self.rest_pose is not None
            and self.torque == Torque.ON
        ):
            acts.add(Action.STOP_TO_REST)
            if c == Connection.PLAYING_BEHAVIOR:
                acts.add(Action.STOP_BEHAVIOR)
        if (  # Studio Go Rest from a still, held arm (the UI confirms first: the arm starts moving)
            m == Mode.SIM_TO_REAL
            and c in {Connection.HOLDING, Connection.CONNECTED_READ_ONLY}
            and self.rest_pose is not None
            and self.torque == Torque.ON
            and self.fault is None
            and not self.recorder.active
        ):
            acts.add(Action.STOP_TO_REST)
        if (  # Studio Go Rest in SIM_ONLY: the SIMULATED arm only (never touches hardware)
            m == Mode.SIM_ONLY
            and not self.paused
            and not self.recorder.active
            and self.staged.values is not None
            and c != Connection.CONNECTING
        ):
            acts.add(Action.STOP_TO_REST)
        if (  # named pose on the real arm: from a held / armed arm, never while recording or faulted
            m == Mode.SIM_TO_REAL
            and c in {Connection.ARMED, Connection.HOLDING, Connection.CONNECTED_READ_ONLY}
            and self.torque == Torque.ON
            and self.fault is None
            and self.hw_bounds is not None
            and self.calibration_fp is not None
            and not self.recorder.active
        ) or (
            m == Mode.SIM_ONLY
            and not self.paused
            and not self.recorder.active
            and self.staged.values is not None
            and c != Connection.CONNECTING
        ):
            acts.add(Action.MOVE_TO_POSE)
        if (  # Go to a pose while a motion runs: it supersedes that motion (ADR-017/018)
            m == Mode.SIM_TO_REAL
            and c in IN_FLIGHT
            and self.torque == Torque.ON
            and self.fault is None
            and self.hw_bounds is not None
            and self.calibration_fp is not None
        ):
            acts.add(Action.MOVE_TO_POSE)
        if (
            c == Connection.PLAYING_BEHAVIOR
            and self._run is not None
            and not self._run.paused
            and self._run.resume_move is None
            and self._run.finish_rep is None
        ):
            acts.add(Action.FINISH_BEHAVIOR)
        if c == Connection.PLAYING_BEHAVIOR and self._run is not None and self.torque == Torque.ON:
            if self._run.paused:
                if self.fault is None:
                    acts.add(Action.RESUME_BEHAVIOR)
            else:
                acts.add(Action.PAUSE_BEHAVIOR)
        mode_change_ok = (
            c
            not in {
                Connection.CONNECTING,
                Connection.RETURNING_TO_REST,
                Connection.PLAYING_BEHAVIOR,
                Connection.MOVING_TO_POSE,
            }
            and not self.recorder.active
            and (has_robot or c == Connection.DISCONNECTED)
        )
        if mode_change_ok:  # Studio workflow (connects if needed; the controller re-checks every step)
            acts.add(Action.ENTER_MODE_REAL_TO_SIM)
            if not has_robot or self.calibration_valid:
                acts.add(Action.ENTER_MODE_SIM_TO_REAL)
        if self._overlay_capable():
            acts.add(Action.FOLLOW)
            acts.add(Action.TALK)
        if self.directions is not None and (
            (m == Mode.SIM_ONLY and c != Connection.CONNECTING)
            or (m != Mode.SIM_ONLY and has_robot and self.calibration_valid and self.fault is None)
        ):
            acts.add(Action.SET_FRONT)
        if (
            has_robot
            and self._legacy_rest is not None
            and self.calibration_valid
            and self.hw_bounds is not None
            and self.fault is None
        ):
            acts.add(Action.ADOPT_REST_POSE)
        # ---- M2 recording (never while motion is possible) and playback
        if self.recorder.active:
            acts.add(Action.STOP_RECORDING)
        elif self._run is None:
            if (
                has_robot
                and m != Mode.SIM_ONLY
                and c == Connection.TORQUE_OFF
                and self.torque == Torque.OFF
                and self.calibration_valid
                and self.calibration_fp is not None
                and self.fault is None
            ):
                acts.add(Action.START_REAL_RECORDING)
            if m == Mode.SIM_ONLY and not self.paused and c != Connection.CONNECTING:
                acts.add(Action.START_SIM_RECORDING)
        if (
            has_robot
            and m == Mode.SIM_TO_REAL
            and not self.recorder.active
            and self.fault is None
            and self.calibration_valid
            and self.hardware_authorized
            and self.hw_bounds is not None
            and self.calibration_fp is not None
            and self.rest_pose is not None
            and (
                (  # ... or while a motion runs: the new behavior supersedes it (ADR-018)
                    c in {Connection.CONNECTED_READ_ONLY, Connection.HOLDING, Connection.ARMED} | IN_FLIGHT
                    and self.torque == Torque.ON
                )
                or (c == Connection.TORQUE_OFF and self.torque == Torque.OFF)
            )
        ):
            acts.add(Action.PLAY_BEHAVIOR)
        return frozenset(acts)

    def _overlay_capable(self) -> bool:
        if self.recorder.active:
            return False
        if self.mode == Mode.SIM_ONLY:
            return self.connection != Connection.CONNECTING and self.staged.values is not None
        return (
            self.mode == Mode.SIM_TO_REAL
            and self.robot is not None
            and self.connection in OVERLAY_STATES
            and self.torque == Torque.ON
            and self.fault is None
            and self.calibration_valid
            and self.hw_bounds is not None
        )

    def active_bounds(self) -> tuple[JointBounds, str]:
        if self.mode != Mode.SIM_ONLY and self.hw_bounds is not None:
            return self.hw_bounds, self.hw_bounds.label
        return self.sim_bounds, self.sim_bounds.label

    # ===================================================================== requests
    def handle(self, request: rq.Request | rq.Tracked) -> None:
        """Handle one request on the controller thread. Tracked requests (every Studio motion request
        is tracked automatically) get their lifecycle recorded in ``self.commands``."""
        cmd_id: str | None = None
        source, physical_ok = "studio", True
        if isinstance(request, rq.Tracked):
            cmd_id, source, physical_ok = request.command_id, request.source, request.physical_allowed
            req: rq.Request = request.request
        else:
            req = request
        kind = rq.TRACKED_KINDS.get(type(req))
        if kind is not None and cmd_id is None:
            cmd_id = self.commands.create(kind, source, _describe(req))[0]["id"]
        self._cmd = (cmd_id, source)
        self._cmd_rejected = False
        try:
            self._dispatch(req, kind, source, physical_ok)
        finally:
            if cmd_id is not None and self.commands.state_of(cmd_id) == CommandState.QUEUED:
                self.commands.mark(cmd_id, CommandState.FAILED, self.message)  # e.g. read failed -> fault
            self._cmd = (None, "studio")

    def _dispatch(self, req: rq.Request, kind: str | None, source: str, physical_ok: bool) -> None:
        if source == "api":
            problem = self._api_problem(req, kind, physical_ok)
            if problem:
                self._reject(req, problem)
                return
        handlers: dict[type, tuple[Action | None, Callable[[Any], None]]] = {
            rq.SetMode: (None, self._on_set_mode),
            rq.Connect: (Action.CONNECT, self._on_connect),
            rq.EnableMovement: (Action.ENABLE_MOVEMENT, self._on_enable_movement),
            rq.Disarm: (Action.DISARM, self._on_disarm),
            rq.EmergencyHold: (Action.EMERGENCY_HOLD, self._on_hold),
            rq.TorqueOff: (Action.TORQUE_OFF, self._on_torque_off),
            rq.EnableTorque: (Action.ENABLE_TORQUE, self._on_enable_torque),
            rq.Recover: (Action.RECOVER, self._on_recover),
            rq.Disconnect: (Action.DISCONNECT, self._on_disconnect),
            rq.RecordRestPose: (Action.RECORD_REST_POSE, self._on_record_rest_pose),
            rq.StopToRest: (Action.STOP_TO_REST, self._on_stop_to_rest),
            rq.AdoptRestPose: (Action.ADOPT_REST_POSE, self._on_adopt_rest_pose),
            rq.StartRecording: (None, self._on_start_recording),
            rq.StopRecording: (Action.STOP_RECORDING, self._on_stop_recording),
            rq.PlayBehavior: (Action.PLAY_BEHAVIOR, self._on_play_behavior),
            rq.StopBehavior: (Action.STOP_BEHAVIOR, self._on_stop_behavior),
            rq.Pause: (Action.PAUSE, self._on_pause),
            rq.Resume: (Action.RESUME, self._on_resume),
            rq.EnterMode: (None, self._on_enter_mode),
            rq.PauseBehavior: (Action.PAUSE_BEHAVIOR, self._on_pause_behavior),
            rq.ResumeBehavior: (Action.RESUME_BEHAVIOR, self._on_resume_behavior),
            rq.MoveToPose: (Action.MOVE_TO_POSE, self._on_move_to_pose),
            rq.FinishBehavior: (Action.FINISH_BEHAVIOR, self._on_finish_behavior),
            rq.SetFollow: (None, self._on_set_follow),  # OFF always works; ON checks Action.FOLLOW
            rq.TalkStart: (Action.TALK, self._on_talk_start),
            rq.TalkStop: (None, self._on_talk_stop),
            rq.SetFrontReference: (Action.SET_FRONT, self._on_set_front),
        }
        action, fn = handlers[type(req)]
        if action is not None and action not in self.available_actions():
            self._reject(req, f"{action.value} not permitted in {self.mode.value}/{self.connection.value}")
            return
        if getattr(req, "confirmed", True) is False:
            self._reject(req, "explicit operator confirmation required")
            return
        fn(req)
        if (
            source == "studio"
            and self.emergency_latched is not None
            and not self._cmd_rejected
            and self.fault is None
            and (kind in rq.MOTION_KINDS or isinstance(req, rq.EnterMode | rq.EnableMovement))
        ):
            self._release_emergency_latch(f"operator {type(req).__name__} in the Studio")
        self._recording_guard()
        self.on_change()

    def _api_problem(self, req: rq.Request, kind: str | None, physical_ok: bool) -> str | None:
        """ADR-018 gates for API requests, checked HERE (at handling time) so no state change between
        the HTTP check and the controller can be exploited. Authentication is not permission to bypass
        anything: every normal readiness check still runs afterwards."""
        if kind is None:
            return f"{type(req).__name__} is not available through the API"
        if isinstance(req, rq.TalkStop) or (isinstance(req, rq.SetFollow) and not req.enabled):
            return None  # turning an overlay OFF is always allowed (it can only reduce motion)
        if kind not in rq.MOTION_KINDS:
            return None  # Emergency Hold: always allowed
        if self.emergency_latched is not None:
            return (
                f"Emergency Hold is latched ({self.emergency_latched}) — API motion stays refused until "
                "the operator releases it in the Studio (any direct motion or mode action)"
            )
        if self.robot is not None and self.mode != Mode.SIM_ONLY and not physical_ok:
            return "physical API control is disabled ([api] allow_physical_control = false)"
        if isinstance(req, rq.PlayBehavior) and req.enable_torque:
            return "the API never energizes the motors (torque is the operator's decision)"
        return None

    def _release_emergency_latch(self, by: str) -> None:
        if self.emergency_latched is not None:
            self.events.emit("emergency_latch_released", by=by, was=self.emergency_latched)
            self.emergency_latched = None

    def _on_command_update(self, rec: dict[str, Any]) -> None:
        self.events.emit(
            "command_update",
            id=rec["id"],
            command_kind=rec["kind"],
            source=rec["source"],
            state=rec["state"],
            detail=rec["detail"],
            superseded_by=rec["superseded_by"],
            replaced=rec["replaced"],
            cancelled_by=rec["cancelled_by"],
            after_behavior_cancelled=rec["after_behavior_cancelled"],
        )

    def _cmd_done(self, detail: str = "") -> None:
        """The request being handled (an instantaneous one: pause, resume, finish, hold) took effect."""
        self.commands.mark(self._cmd[0], CommandState.COMPLETED, detail)

    def _reject(self, req: rq.Request, reason: str) -> None:
        self.message = f"Rejected {type(req).__name__}: {reason}"
        self._cmd_rejected = True
        self.commands.mark(self._cmd[0], CommandState.REJECTED, reason)
        self.events.emit("request_rejected", request=type(req).__name__, reason=reason)
        self.on_change()

    def _on_set_mode(self, req: rq.SetMode) -> None:
        target = req.mode
        action = {
            Mode.SIM_ONLY: Action.SET_MODE_SIM_ONLY,
            Mode.REAL_TO_SIM: Action.SET_MODE_REAL_TO_SIM,
            Mode.SIM_TO_REAL: Action.SET_MODE_SIM_TO_REAL,
        }[target]
        if action not in self.available_actions():
            self._reject(req, f"cannot enter {target.value} from {self.mode.value}/{self.connection.value}")
            return
        previous = self.mode
        if self.connection in {
            Connection.ARMED,
            Connection.RETURNING_TO_REST,
            Connection.PLAYING_BEHAVIOR,
            Connection.MOVING_TO_POSE,
            Connection.HOLDING,
        }:
            self._set_connection(Connection.CONNECTED_READ_ONLY, "mode change stops motion targets")
        self._discard_intent()
        self._overlays_off(f"mode change to {target.value}")
        if self._sim_rest is not None and self._active_is_sim:
            self.commands.finish_active(CommandState.CANCELLED, f"mode change to {target.value}")
        self._sim_rest = None
        self._sim_rest_clock = None
        self.mode = target
        self._load_front()
        now = self.clock()
        if target == Mode.SIM_ONLY:
            pose = (
                dict(self.displayed.values)
                if self.displayed.values is not None
                else dict(self.staged.values or {})
            )
            if pose:
                self.requested = StageValue(pose, now)
                self.staged = StageValue(pose, now)
                self.requested_epoch += 1
        else:
            self.requested = NOT_AVAILABLE
            self.staged = NOT_AVAILABLE
        self.message = f"Mode {target.value}"
        self.events.emit("mode_switched", previous=previous.value, mode=target.value)

    def _state(self) -> Connection:
        """Current connection state (a call, so type narrowing does not survive handler calls)."""
        return self.connection

    def _on_enter_mode(self, req: rq.EnterMode) -> None:
        """Studio mode workflow (see ``rq.EnterMode``). Every step uses the existing M1 handler, so each
        keeps its own checks, events and fault handling; the first failing step ends the sequence."""
        target = req.mode
        busy = {
            Connection.CONNECTING,
            Connection.RETURNING_TO_REST,
            Connection.PLAYING_BEHAVIOR,
            Connection.MOVING_TO_POSE,
        }
        if self.connection in busy or self.recorder.active:
            self._reject(req, f"cannot change mode while {self.connection.value.lower().replace('_', ' ')}")
            return
        self.events.emit("mode_workflow_started", mode=target.value, from_mode=self.mode.value)

        def failed(reason: str) -> None:
            self.message = f"{_MODE_LABEL[target]} not entered: {reason}"
            self.events.emit(
                "mode_workflow_failed",
                mode=target.value,
                reason=reason,
                now_mode=self.mode.value,
                connection=self.connection.value,
                torque=self.torque.value,
            )

        if target == Mode.SIM_ONLY:
            if self.mode != Mode.SIM_ONLY:
                self._on_set_mode(rq.SetMode(Mode.SIM_ONLY))
            self.events.emit("mode_workflow_completed", mode=target.value, connection=self.connection.value)
            self.message = "Simulation Only — sliders and playback drive the 3D arm"
            return
        if self.robot is None:
            if not req.port.startswith("/dev/") or not req.calibration_path:
                failed("no serial port / calibration file configured (Advanced → Connection)")
                return
            self._on_connect(rq.Connect(req.port, req.calibration_path, confirmed=True))
            if self.robot is None:
                failed(self.message)
                return
        if self.fault is not None:
            failed(f"fault {self.fault.code} is latched — recover first (Advanced → Hardware)")
            return
        if self.mode != target:
            self._on_set_mode(rq.SetMode(target))
            if self.mode != target:
                failed(self.message)
                return
        if target == Mode.REAL_TO_SIM:
            if self.connection != Connection.TORQUE_OFF:
                self._on_torque_off(rq.TorqueOff(confirmed=True))
                if self._state() != Connection.TORQUE_OFF:
                    failed(self.message)
                    return
            self.message = "Real → Simulation — torque OFF, move the arm by hand; the 3D arm mirrors it"
        else:
            if self.connection == Connection.TORQUE_OFF:
                self._on_enable_torque(rq.EnableTorque(confirmed=True))  # goal := fresh present, then on
                if self.torque != Torque.ON or self._state() != Connection.CONNECTED_READ_ONLY:
                    failed(self.message)
                    return
            if self.torque != Torque.ON:
                failed(
                    f"torque state is {self.torque.value} — establish it first "
                    "(Advanced → Hardware: Torque OFF, then choose this mode again)"
                )
                return
            if self.connection in {Connection.CONNECTED_READ_ONLY, Connection.HOLDING}:
                if Action.ENABLE_MOVEMENT not in self.available_actions():
                    failed("movement cannot be enabled in this state")
                    return
                self._on_enable_movement(rq.EnableMovement(confirmed=True))
                if self._state() != Connection.ARMED:
                    failed(self.message)
                    return
            self.message = "Simulation → Real — the arm holds its pose; Play and Go Rest move it"
        self.events.emit(
            "mode_workflow_completed",
            mode=target.value,
            connection=self.connection.value,
            torque=self.torque.value,
        )

    def _on_connect(self, req: rq.Connect) -> None:
        now = self.clock()
        self.events.emit("connect_confirmation", port=req.port, calibration_path=req.calibration_path)
        try:
            calibration = load_calibration(req.calibration_path)
            bounds = calibrated_bounds(calibration, policy.ANGULAR_MARGIN_DEG)
        except (CalibrationError, InvertedBoundsError) as exc:
            self.message = f"Connect refused — calibration invalid: {exc}"
            self.events.emit("calibration_rejected", reason=str(exc))
            return
        self.events.emit(
            "calibration_validated",
            path=str(calibration.path),
            robot_id=calibration.robot_id,
            bounds={k: [round(a, 2), round(b, 2)] for k, (a, b) in bounds.as_tuples().items()},
        )
        self._set_connection(Connection.CONNECTING, "operator confirmed connect")
        self.on_change()
        robot = self._robot_factory()
        try:
            robot.initialize(Path(req.calibration_path), req.port)
        except Exception as exc:
            self._set_connection(Connection.DISCONNECTED, "connect failed")
            # LeRobot configure() may have disabled (and maybe re-enabled) torque before failing
            # (observed on hardware: gripper "Input voltage error" mid-configure) -> state unknown.
            # Errors raised before the port was opened (driver import, bad port, calibration) change nothing.
            touched = getattr(exc, "port_touched", not isinstance(exc, CalibrationError))
            if touched:
                self.torque = Torque.UNKNOWN
            self.message = f"Connect failed: {exc}." + (
                " Torque state UNKNOWN (connect may have changed it) — support the arm." if touched else ""
            )
            self.events.emit("connect_failed", error=f"{type(exc).__name__}: {exc}", torque=self.torque.value)
            return
        self.robot = robot
        self.port_label = req.port
        self.hardware_authorized = True
        self.hw_bounds = bounds
        self.calibration_fp = calibration_fingerprint(calibration)
        # LeRobot configure() runs inside bus.torque_disabled(): torque is re-enabled on exit.
        self.torque = Torque.ON
        self.events.emit("hardware_connected", port=req.port, driver=robot.describe())
        self._set_connection(Connection.CONNECTED_READ_ONLY, "connected; no motion until enabled")
        try:
            self.calibration_valid = bool(robot.verify_calibration())
        except Exception as exc:
            self._latch(exc, now)
            return
        if not self.calibration_valid:
            self._latch_code(
                "CALIBRATION_MISMATCH",
                "Motor calibration registers differ from the selected file. Movement is disabled; "
                "re-run LeRobot calibration manually or select the matching file (no automatic calibration).",
                torque_unknown=False,
            )
            return
        self._read_actual()
        self._load_rest_pose()
        self._load_front()
        if self.connection == Connection.CONNECTED_READ_ONLY:
            self.message = "Connected (read-only). Select a hardware mode; motion requires Enable movement."

    def _on_enable_movement(self, req: rq.EnableMovement) -> None:
        frame = self._read_actual()
        if frame is None:
            return
        now = self.clock()
        pose = frame.as_dict()
        self.requested = StageValue(pose, now)
        self.staged = StageValue(pose, now)
        self.requested_epoch += 1
        self._discard_intent()
        self._set_connection(Connection.ARMED, "operator enabled movement")
        assert self.hw_bounds is not None
        outside = self.hw_bounds.outside(pose)
        self.events.emit("movement_enabled", start_pose=_r(pose), joints_outside_margin=outside)
        self.message = "ARMED — sliders command the real arm" + (
            f" (joints inside safety margin, inward-only: {', '.join(outside)})" if outside else ""
        )

    def _on_disarm(self, req: rq.Disarm) -> None:
        self._discard_intent()
        self._set_connection(Connection.CONNECTED_READ_ONLY, "operator disarmed")
        self.message = "Disarmed — no motion commands are sent"

    def _on_hold(self, req: rq.EmergencyHold) -> None:
        self._discard_intent()
        robot = self.robot
        assert robot is not None
        was_faulted = self.connection == Connection.FAULT_LATCHED
        self.emergency_latched = f"{self._cmd[1]} Emergency Hold"  # API motion refused until released
        self._overlays_off("Emergency Hold")  # overrides every overlay; nothing animates while holding
        self.events.emit("hold_requested", while_faulted=was_faulted, source=self._cmd[1])
        now = self.clock()
        try:
            measured = robot.observe()
            self._record_actual(measured)
            sent = robot.send(measured)
            self._record_sent(sent)
        except Exception as exc:
            self.events.emit("hold_failed", error=f"{type(exc).__name__}: {exc}")
            self._latch(exc, now, code="HOLD_FAILED")
            self.message = (
                "HOLD FAILED — software cannot guarantee a stop. Use physical power cut-off if needed."
            )
            return
        pose = measured.as_dict()
        self.requested = StageValue(pose, now)
        self.staged = StageValue(pose, now)
        self.requested_epoch += 1
        if was_faulted:
            self.message = "Hold target sent (best effort); fault remains LATCHED"
        else:
            self._set_connection(Connection.HOLDING, "emergency hold (best effort)")
            self.message = "HOLDING measured pose (best effort — not a physical E-stop)"
        self._cmd_done("hold target sent (best effort)")

    def _on_torque_off(self, req: rq.TorqueOff) -> None:
        self._discard_intent()
        robot = self.robot
        assert robot is not None
        was_faulted = self.connection == Connection.FAULT_LATCHED
        now = self.clock()
        try:
            robot.torque_off()
        except Exception as exc:
            self.events.emit("torque_off_failed", error=f"{type(exc).__name__}: {exc}")
            self._latch(exc, now)
            self.message = "TORQUE OFF FAILED — torque state UNKNOWN"
            return
        self.torque = Torque.OFF
        self.events.emit("torque_off_confirmed", while_faulted=was_faulted)
        self.staged = NOT_AVAILABLE
        if not was_faulted:
            self._set_connection(Connection.TORQUE_OFF, "operator confirmed torque off")
        self.message = "Torque OFF — the arm is not actively held"

    def _on_enable_torque(self, req: rq.EnableTorque) -> None:
        robot = self.robot
        assert robot is not None
        now = self.clock()
        try:
            measured = robot.enable_torque_holding_present()
        except Exception as exc:
            self._latch(exc, now)
            self.message = "Enable torque FAILED — torque state UNKNOWN"
            return
        self._record_actual(measured)
        self.torque = Torque.ON
        self.events.emit("torque_on_confirmed", hold_pose=_r(measured.as_dict()))
        self._set_connection(Connection.CONNECTED_READ_ONLY, "torque re-enabled holding present pose")
        self.message = "Torque ON, holding the present pose. Movement still requires Enable movement."

    def _on_recover(self, req: rq.Recover) -> None:
        robot = self.robot
        assert robot is not None and self.fault is not None
        now = self.clock()
        self.events.emit("recovery_requested", fault=self.fault.code)
        try:
            frame = robot.observe()
            if self.fault.code == "CALIBRATION_MISMATCH" and not robot.verify_calibration():
                self.fault = self.fault.repeated("calibration registers still differ from file", now)
                self.message = "Recovery refused: calibration mismatch persists"
                self.events.emit("recovery_failed", reason="calibration mismatch persists")
                return
            if self.fault.code == "CALIBRATION_MISMATCH":
                self.calibration_valid = True
        except Exception as exc:
            self.fault = self.fault.repeated(f"recovery read failed: {exc}", now)
            self.message = f"Recovery failed: {exc}"
            self.events.emit("recovery_failed", reason=f"{type(exc).__name__}: {exc}")
            return
        self._record_actual(frame)
        cleared = self.fault
        self.fault = None
        self.events.emit("fault_recovered", code=cleared.code, acknowledged=True, torque=self.torque.value)
        nxt = Connection.TORQUE_OFF if self.torque == Torque.OFF else Connection.CONNECTED_READ_ONLY
        self._set_connection(nxt, "manual recovery after fresh read")
        self.message = (
            "Recovered (read-only). Torque state UNKNOWN: use Torque Off then Enable torque to establish it."
            if self.torque == Torque.UNKNOWN
            else "Recovered (read-only)."
        )

    def _on_disconnect(self, req: rq.Disconnect) -> None:
        robot = self.robot
        assert robot is not None
        self._discard_intent()
        disable = robot.disable_torque_on_disconnect
        self.events.emit("disconnect_requested", disable_torque_on_disconnect=disable)
        error = None
        try:
            robot.disconnect()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.events.emit("disconnect_error", error=error)
        self.robot = None
        self.hardware_authorized = False
        self.calibration_valid = False
        self.hw_bounds = None
        self.calibration_fp = None
        self._legacy_rest = None
        self.fault = None
        self._release_emergency_latch("disconnect")
        if error is None and disable:
            self.torque = Torque.OFF
        else:
            self.torque = Torque.UNKNOWN
        self.actual = StageValue(self.actual.values, self.actual.t_mono, self.actual.seq, stale=True)
        self._set_connection(Connection.DISCONNECTED, "operator disconnect")
        if self.mode != Mode.SIM_ONLY:
            self._on_set_mode(rq.SetMode(Mode.SIM_ONLY))
        self.message = f"Disconnected. Torque {self.torque.value}" + (f" ({error})" if error else "")

    # ----------------------------------------------------------------- rest pose (T-039, ADR-011)
    def _robot_id(self) -> str | None:
        cal = self.robot.calibration if self.robot is not None else None
        return cal.robot_id if cal is not None else None

    def _load_rest_pose(self) -> None:
        """Load the rest pose for this calibration. D5: it must carry THIS calibration's fingerprint;
        a fingerprint-less (legacy) file needs explicit adoption, a different one is rejected."""
        robot_id = self._robot_id()
        self.rest_pose = None
        self._legacy_rest = None
        self.rest_status = ("none", "")
        if robot_id is None or self.hw_bounds is None:
            return
        try:
            rest = self.rest_store.load(robot_id)
        except RestPoseError as exc:
            self.rest_status = ("rejected", str(exc))
            self.events.emit("rest_pose_rejected", reason=str(exc))
            return
        if rest is None:
            return
        outside = self.hw_bounds.outside(rest.pose)
        if outside:
            reason = f"outside the calibrated bounds: {outside}"
            self.rest_status = ("rejected", reason)
            self.events.emit("rest_pose_rejected", reason=reason)
            return
        if rest.calibration_fingerprint is None:
            self._legacy_rest = rest
            self.rest_status = (
                "needs_adoption",
                f"saved {rest.recorded_at} without a calibration fingerprint — confirm it for the current "
                "calibration (Adopt rest pose…) or record a new one",
            )
            self.events.emit(
                "rest_pose_needs_adoption",
                robot_id=robot_id,
                pose=_r(rest.pose),
                recorded_at=rest.recorded_at,
            )
            return
        if rest.calibration_fingerprint != self.calibration_fp:
            reason = "recorded with a different calibration — record a new rest pose"
            self.rest_status = ("mismatch", reason)
            self.events.emit(
                "rest_pose_rejected",
                reason=reason,
                file_fingerprint=rest.calibration_fingerprint,
                calibration_fingerprint=self.calibration_fp,
            )
            return
        self.rest_pose = rest
        self.rest_status = ("loaded", "")
        self._rest_dir = None  # recomputed on demand (one FK evaluation, never per tick)
        self.events.emit(
            "rest_pose_loaded", robot_id=robot_id, pose=_r(rest.pose), recorded_at=rest.recorded_at
        )

    def _on_adopt_rest_pose(self, req: rq.AdoptRestPose) -> None:
        legacy = self._legacy_rest
        assert legacy is not None and self.calibration_fp is not None
        adopted = RestPose(
            legacy.robot_id,
            dict(legacy.pose),
            dict(legacy.measured),
            legacy.clamped_joints,
            legacy.recorded_at,
            self.calibration_fp,
            {**legacy.provenance, "adopted_for_calibration_at": now_iso(), "adopted_from": "legacy file"},
        )
        location = self.rest_store.save(adopted)
        backup = getattr(self.rest_store, "last_backup", None)
        self.rest_pose = adopted
        self._rest_dir = None
        self._legacy_rest = None
        self.rest_status = ("loaded", "")
        self.events.emit(
            "rest_pose_adopted",
            robot_id=adopted.robot_id,
            pose=_r(adopted.pose),
            calibration_fingerprint=self.calibration_fp,
            location=location,
            backup=backup,
        )
        self.message = "Rest pose adopted for the current calibration" + (
            f" (original kept as {backup})" if backup else ""
        )

    def _on_record_rest_pose(self, req: rq.RecordRestPose) -> None:
        robot_id = self._robot_id()
        assert robot_id is not None and self.hw_bounds is not None
        frame = self._read_actual()
        if frame is None:
            return
        measured = frame.as_dict()
        pose, reasons = self.hw_bounds.clamp(measured)  # owner decision: clamp into the allowed ranges
        clamped = tuple(sorted({r.split(":")[0] for r in reasons}))
        rest = RestPose(robot_id, pose, measured, clamped, now_iso(), self.calibration_fp)
        location = self.rest_store.save(rest)
        self.rest_pose = rest
        self._rest_dir = None
        self._legacy_rest = None
        self.rest_status = ("loaded", "")
        self.events.emit(
            "rest_pose_recorded",
            robot_id=robot_id,
            pose=_r(pose),
            measured=_r(measured),
            clamped_joints=list(clamped),
            location=location,
        )
        self.message = "Rest pose recorded" + (
            f" (clamped into the allowed range: {', '.join(clamped)})" if clamped else ""
        )

    def _on_stop_to_rest(self, req: rq.StopToRest) -> None:
        if self.talk.engaged:  # D3/T8: Go Rest cancels Talk Mode; the rest line takes the gripper from
            self._talk_off("Go Rest")  # its last command, so there is no jump
        if self.mode == Mode.SIM_ONLY:
            self._start_sim_rest()
            return
        if self.connection == Connection.PLAYING_BEHAVIOR:
            self._stop_behavior("operator STOP")
            return
        if self.connection == Connection.MOVING_TO_POSE:
            self._start_rest(
                "normal stop during a pose move: return to rest pose", from_last_target=True, claim=True
            )
            return
        if self.connection == Connection.RETURNING_TO_REST:
            # already on the way: the new command takes over the SAME trajectory (no restart, no jump)
            self._claim_motion("already returning to rest: took over the running return")
            return
        self._start_rest("normal stop: return to rest pose", claim=True, from_last_target=self.follow.engaged)

    def _start_sim_rest(self) -> None:
        """Go Rest in SIM_ONLY: move the SIMULATED arm along the same 30°/s line to the rest pose (the
        recorded physical one if known, else a default simulated pose), clamped to the simulation bounds.
        Display only; a slider move cancels it."""
        assert self.staged.values is not None
        now = self.clock()
        known = self.rest_pose is not None
        goal_raw = dict(self.rest_pose.pose) if self.rest_pose is not None else dict(DEFAULT_SIM_REST_POSE)
        goal, _ = self.sim_bounds.clamp(goal_raw)
        self._discard_intent()
        start = dict(self.staged.values)
        traj = RestTrajectory.plan(
            self._overlay_adjusted(start, goal),
            goal,
            0.0,
            policy.REST_RETURN_SPEED,
            policy.REST_MIN_DURATION_S,
        )
        self._claim_motion("simulated arm returning to rest", sim=True)
        self._set_owner_direction(self._rest_direction())
        self._sim_rest = traj
        self._sim_rest_clock = StallAwareClock(self._period * policy.TIMELINE_MAX_STEP_PERIODS, now)
        self._sim_motion_label = "rest"
        self.requested = StageValue(start, now)
        self.requested_epoch += 1
        self.events.emit(
            "sim_rest_started",
            duration_s=round(traj.duration, 2),
            goal=_r(goal),
            source="recorded rest pose" if known else "default simulated rest pose",
        )
        self.message = f"Simulated arm returning to rest ({traj.duration:.1f} s)"

    def _start_rest(self, reason: str, *, from_last_target: bool = False, claim: bool = False) -> None:
        """M1 Normal Stop: fresh read, timed straight line to the rest pose (30°/s), then hold.
        D4: driven by a stall-aware clock — after a serial stall the next target is the next point of
        the line, never a jump ahead. Speed, tolerance, timeout and failure handling are unchanged.

        D9 (owner, 2026-11-07, ADR-015): when a BEHAVIOR hands over (completed or normal Stop), the line
        starts at the last target sent, not at the measured pose. A fast gesture leaves the arm lagging
        behind its target; starting from the measured pose snapped the target back by up to 24° and
        braked the arm hard. The arm now finishes its current move and turns smoothly towards rest. The
        fresh read still runs first (a failed read latches a fault and nothing is sent)."""
        assert self.rest_pose is not None
        self._discard_intent()
        frame = self._read_actual()
        if frame is None:
            return
        now = self.clock()
        measured = frame.as_dict()
        last = self.sent.values
        start = dict(last) if from_last_target and last is not None else measured
        if claim:  # a new Go Rest command (an ending rest belongs to its behavior's command)
            self._start_origin = "last_target" if start is not measured else "measured"
            self._take_over(measured, start)
            self._claim_motion("returning to the default rest pose")
        goal = dict(self.rest_pose.pose)
        # joints owned by an overlay keep their overlay; the rest line is timed by the joints it moves
        traj = RestTrajectory.plan(
            self._overlay_adjusted(start, goal),
            goal,
            now,
            policy.REST_RETURN_SPEED,
            policy.REST_MIN_DURATION_S,
        )
        self._set_owner_direction(self._rest_direction())
        self._set_connection(Connection.RETURNING_TO_REST, reason)
        self._rest_traj = traj
        self._rest_clock = StallAwareClock(self._period * policy.TIMELINE_MAX_STEP_PERIODS, now)
        self._rest_deadline_s = traj.duration + policy.REST_TIMEOUT_EXTRA_S
        self.staged = StageValue(dict(start), now)
        self.requested = StageValue(dict(start), now)
        self.requested_epoch += 1
        self.events.emit(
            "stop_to_rest_started",
            duration_s=round(traj.duration, 2),
            goal=_r(traj.goal),
            reason=reason,
            starts_from="last_target" if start is not measured else "measured",
            lag_at_start=_r({n: start[n] - measured[n] for n in JOINT_NAMES}),
        )
        self.message = (
            f"STOP: returning to rest pose ({traj.duration:.1f} s at {policy.REST_RETURN_SPEED:g}°/s)"
        )

    def _rest_elapsed(self) -> float:
        return 0.0 if self._rest_clock is None else self._rest_clock.elapsed

    def _check_rest_arrival(self, now: float) -> None:
        traj = self._rest_traj
        if traj is None or self.actual.values is None:
            return
        actual = dict(self.actual.values)
        elapsed = self._rest_elapsed()
        delay_ms = 0.0 if self._rest_clock is None else round(self._rest_clock.delay_s * 1000.0, 1)
        owned = self._owned_joints()  # D3: an overlay-owned joint is not judged against the rest pose
        if traj.done(traj.t0 + elapsed) and within(
            {n: (traj.goal[n] if n in owned else actual[n]) for n in JOINT_NAMES},
            traj.goal,
            policy.REST_ARRIVAL_TOL_ARM_DEG,
            policy.REST_ARRIVAL_TOL_GRIPPER,
        ):
            self.commands.finish_active(CommandState.COMPLETED, "at the default rest pose — holding")
            self.requested = StageValue(dict(traj.goal), now)
            self.staged = StageValue(dict(traj.goal), now)
            self.requested_epoch += 1
            self._set_connection(Connection.HOLDING, "rest pose reached; holding with torque on")
            self.events.emit(
                "rest_reached",
                elapsed_s=round(now - traj.t0, 2),
                actual=_r(actual),
                timeline_delay_ms=delay_ms,
            )
            self.message = "At REST pose — holding position with torque on"
            return
        if elapsed > self._rest_deadline_s:
            errors = {n: round(actual[n] - traj.goal[n], 2) for n in JOINT_NAMES if n not in owned}
            robot = self.robot
            assert robot is not None
            try:
                measured = robot.observe()
                self._record_actual(measured)
                self._record_sent(robot.send(measured))
            except Exception as exc:
                self._latch(exc, now)
                return
            pose = measured.as_dict()
            self.requested = StageValue(pose, now)
            self.staged = StageValue(pose, now)
            self.requested_epoch += 1
            self.commands.finish_active(
                CommandState.FAILED, f"rest not reached within the timeout — holding where it is ({errors})"
            )
            self._set_connection(Connection.HOLDING, "rest not reached within timeout; holding measured pose")
            self.events.emit("rest_not_reached", errors=errors, timeline_delay_ms=delay_ms)
            self.message = f"REST NOT REACHED within timeout — holding where it is (errors: {errors})"

    # ----------------------------------------------------------------- M2 recording (ADR-013)
    def _on_start_recording(self, req: rq.StartRecording) -> None:
        origin = Origin(req.origin)
        if origin is Origin.SIM_KEYFRAMES:
            self._reject(req, "keyframes are authored in the editor, not recorded")
            return
        action = (
            Action.START_REAL_RECORDING if origin is Origin.REAL_HAND_GUIDED else Action.START_SIM_RECORDING
        )
        if action not in self.available_actions():
            why = (
                "hand-guided recording needs a connected arm in REAL_TO_SIM or SIM_TO_REAL, TORQUE_OFF with "
                "torque confirmed OFF (UNKNOWN is not enough), a valid calibration and no fault"
                if origin is Origin.REAL_HAND_GUIDED
                else "slider recording needs SIM_ONLY (not paused)"
            )
            self._reject(req, f"{action.value} not permitted: {why}")
            return
        now = self.clock()
        self._overlays_off("recording started")  # a take records the motion, never overlay output
        self.recorder.start(origin, now, self.calibration_fp, self._robot_id())
        self.last_recording_status = None
        with self._recording_lock:
            self._finished_recording = None
        self.events.emit(
            "recording_started", origin=origin.value, calibration_fingerprint=self.calibration_fp
        )
        self.message = (
            "REC — hand guidance, torque OFF: support the arm and move it; nothing is commanded"
            if origin is Origin.REAL_HAND_GUIDED
            else "REC — slider authoring in SIM_ONLY (no hardware)"
        )

    def _on_stop_recording(self, req: rq.StopRecording) -> None:
        self._finish_recording(None)

    def _finish_recording(self, aborted: str | None, note: str = "") -> None:
        rec = self.recorder.finish(aborted, note)
        with self._recording_lock:
            self._finished_recording = rec
        problems = rec.problems()
        self.last_recording_status = {
            "state": "finished",
            "origin": rec.origin.value,
            "valid": rec.valid,
            "samples": rec.report.sample_count,
            "duration_s": round(rec.report.duration_s, 3),
            "max_gap_ms": round(rec.report.max_gap_s * 1000.0, 1),
            "aborted": rec.aborted,
            "problems": problems[:8],
            "note": note,
        }
        self.events.emit(
            "recording_finished" if aborted is None else "recording_aborted",
            origin=rec.origin.value,
            valid=rec.valid,
            samples=rec.report.sample_count,
            duration_s=round(rec.report.duration_s, 3),
            max_gap_ms=round(rec.report.max_gap_s * 1000.0, 1),
            problems=problems[:8],
        )
        if rec.valid:
            self.message = (
                f"Recording finished: {rec.report.sample_count} samples, {rec.report.duration_s:.1f} s"
            )
        else:
            self.message = "Recording NOT playable: " + "; ".join(problems[:3])

    def take_recording(self) -> Recording | None:
        """Thread-safe: hand the finished take to the caller (UI / service) exactly once."""
        with self._recording_lock:
            rec, self._finished_recording = self._finished_recording, None
        return rec

    def _recording_guard(self) -> None:
        """A take stops being valid the moment its preconditions break (never silently continued)."""
        if not self.recorder.active:
            return
        if self.recorder.origin is Origin.REAL_HAND_GUIDED:
            ok = (
                self.robot is not None
                and self.mode != Mode.SIM_ONLY
                and self.connection == Connection.TORQUE_OFF
                and self.torque == Torque.OFF
                and self.calibration_valid
                and self.fault is None
            )
        else:
            ok = self.mode == Mode.SIM_ONLY and not self.paused
        if not ok:
            self._finish_recording(
                f"preconditions changed (mode {self.mode.value}, {self.connection.value}, torque "
                f"{self.torque.value}{', paused' if self.paused else ''})"
            )

    # ----------------------------------------------------------------- M2 playback (ADR-013)
    def _refuse_play(self, req: rq.PlayBehavior, reason: str) -> None:
        self.events.emit("behavior_refused", behavior_id=req.behavior.id, reason=reason)
        self._reject(req, reason)

    def _on_play_behavior(self, req: rq.PlayBehavior) -> None:
        appr = req.approval
        fp, bounds, robot = self.calibration_fp, self.hw_bounds, self.robot
        assert fp is not None and bounds is not None and robot is not None
        # Owner decision D7 (Gate B, 2026-11-07): the MuJoCo preview is OPTIONAL. Without one the run
        # starts from the fresh measured pose after the explicit confirmation. A preview that IS
        # supplied must still match exactly (single use, same behavior/settings/calibration/start).
        if appr is not None and not self._preview_matches(req, appr, fp, bounds):
            return
        ending_problem = self._ending_problem(req, fp, bounds)
        if ending_problem:
            self._refuse_play(req, ending_problem)
            return
        if self.connection == Connection.TORQUE_OFF and not req.enable_torque:
            self._refuse_play(req, "torque is OFF: the confirmation must include energizing the motors")
            return
        frame = self._read_actual()  # always a fresh read first, also when superseding a motion
        if frame is None:
            return  # read failed: fault latched, nothing moved
        measured = frame.as_dict()
        start = self._handover_start(measured)
        if appr is not None:
            moved = pose_deviation(appr.start_pose, measured)
            if moved:
                detail = ", ".join(f"{n} {d:+.1f}" for n, d in moved.items())
                self._refuse_play(req, f"the arm moved since the preview ({detail}) — preview again")
                return
        try:
            plan = compile_plan(
                req.behavior,
                req.settings,
                start,
                bounds=bounds.as_tuples(),
                calibration_fingerprint=fp,
                entry_profile=req.entry_profile,
                bridge_profile=req.bridge_profile,
            )
        except (CompatibilityError, BehaviorFormatError) as exc:
            self._refuse_play(req, str(exc))
            return
        if appr is not None:
            if plan.content_token != appr.content_token:
                self._refuse_play(req, "the effective trajectory differs from the preview — preview again")
                return
            self._used_approvals.add(appr.nonce)  # single use, even if enabling torque fails below
        if self._owned_joints():  # the entry is timed by the joints the behavior really moves
            plan = compile_plan(
                req.behavior,
                req.settings,
                self._overlay_adjusted(start, plan.content.poses[0]),
                bounds=bounds.as_tuples(),
                calibration_fingerprint=fp,
                entry_profile=req.entry_profile,
                bridge_profile=req.bridge_profile,
            )
        self._start_behavior(req, plan, measured, start, robot)

    def _ending_problem(self, req: rq.PlayBehavior, fp: str, bounds: JointBounds) -> str | None:
        """ADR-017: a "Move to saved pose" ending must name an existing pose that is valid for THIS
        calibration, checked before anything moves (never discovered at the end of the run)."""
        s = req.settings
        if s.after_action != "pose":
            return None
        target = req.end_pose
        if target is None or target.id != s.after_pose_id:
            return (
                "the behavior ends at a saved pose that no longer exists — choose another "
                "'After behavior' ending"
            )
        problems = pose_problems_for_arm(target, fp, bounds.as_tuples())
        return "ending pose not usable: " + "; ".join(problems) if problems else None

    def _preview_matches(
        self, req: rq.PlayBehavior, appr: PreviewApproval, fp: str, bounds: JointBounds
    ) -> bool:
        if appr.nonce in self._used_approvals:
            self._refuse_play(req, "this preview was already used for a run — preview again or play without")
            return False
        if (
            appr.behavior_id != req.behavior.id
            or appr.behavior_revision != req.behavior.revision
            or appr.settings != req.settings
        ):
            self._refuse_play(req, "behavior or playback settings changed since the preview — preview again")
            return False
        if appr.calibration_fingerprint != fp or dict(appr.bounds) != bounds.as_tuples():
            self._refuse_play(
                req, "calibration or calibrated bounds changed since the preview — preview again"
            )
            return False
        return True

    def _start_behavior(
        self,
        req: rq.PlayBehavior,
        plan: PlaybackPlan,
        measured: dict[str, float],
        start: dict[str, float],
        robot: RobotPort,
    ) -> None:
        appr = req.approval
        fp, bounds = self.calibration_fp, self.hw_bounds
        assert fp is not None and bounds is not None
        now = self.clock()
        torque_enabled = False
        if self.connection == Connection.TORQUE_OFF:
            # Confirmed combined enable: goal := FRESH measured position first, then torque on.
            try:
                measured_on = robot.enable_torque_holding_present()
            except Exception as exc:
                self._latch(exc, now)
                self.message = "Enable torque FAILED — torque state UNKNOWN; behavior not started"
                return
            self._record_actual(measured_on)
            self.torque = Torque.ON
            torque_enabled = True
            start = measured = measured_on.as_dict()
            self._start_origin = "measured"
            self.events.emit("torque_on_confirmed", hold_pose=_r(start), reason="behavior play confirmation")
            self._set_connection(Connection.CONNECTED_READ_ONLY, "torque enabled holding present pose")
            moved = pose_deviation(appr.start_pose, start) if appr is not None else {}
            if moved:
                self._refuse_play(req, "the arm moved while torque was enabled — holding; preview again")
                return
            plan = compile_plan(
                req.behavior,
                req.settings,
                start,
                bounds=bounds.as_tuples(),
                calibration_fingerprint=fp,
                entry_profile=req.entry_profile,
                bridge_profile=req.bridge_profile,
            )
        self._take_over(measured, start)
        self._claim_motion(f"playing '{plan.behavior_name}'")
        self._discard_intent()
        self.requested = StageValue(dict(start), now)
        self.staged = StageValue(dict(start), now)
        self.requested_epoch += 1
        self._run = BehaviorRun(
            plan,
            StallAwareClock(self._period * policy.TIMELINE_MAX_STEP_PERIODS, now),
            appr.nonce if appr is not None else None,
        )
        self._run.end_pose = req.end_pose if req.settings.after_action == "pose" else None
        self._run.direction = req.direction or self._behavior_direction(req)
        if self._run.end_pose is not None:
            self._run.end_direction = req.end_pose_direction or self._pose_direction(
                self._run.end_pose.pose, f"pose '{self._run.end_pose.name}'", self._run.end_pose.id
            )
        self._set_owner_direction(self._run.direction)
        self.behavior_last_result = None
        self._set_connection(
            Connection.PLAYING_BEHAVIOR, f"behavior '{plan.behavior_name}' confirmed by operator"
        )
        self.events.emit(
            "behavior_started",
            **plan.summary(),
            start_pose=_r(start),
            measured_pose=_r(measured),
            starts_from=self._start_origin,
            clamp=plan.audit.describe(),
            torque_enabled_by_confirmation=torque_enabled,
            # Audit evidence of the D3 gate: which completed preview authorized this run, and how far
            # the measured start was from the previewed start (all within the ±3°/±5 tolerance).
            previewed=appr is not None,
            preview_approval=None
            if appr is None
            else {
                "nonce": appr.nonce,
                "previewed_start": _r(appr.start_pose),
                "start_offset": _r({n: start[n] - appr.start_pose[n] for n in JOINT_NAMES}),
            },
        )
        self.message = (
            f"PLAYING '{plan.behavior_name}' — STOP returns to rest; EMERGENCY HOLD / Torque OFF override"
        )

    def _on_stop_behavior(self, req: rq.StopBehavior) -> None:
        self._stop_behavior("operator Stop Behavior")

    def _stop_behavior(self, reason: str) -> None:
        run = self._run
        assert run is not None
        status = run.status()
        self.behavior_last_result = (
            f"'{run.plan.behavior_name}' stopped ({reason}) during {status['segment']} "
            f"(repetition {status['repetition']}) — returning to rest"
        )
        self.events.emit("behavior_stopped", reason=reason, **status)
        self._run = None  # handed over to the return to rest: not a cancellation
        self._start_rest(f"behavior stopped: {reason}", from_last_target=True, claim=True)

    def _complete_behavior(self) -> None:
        """All repetitions COMPLETED: run the behavior's own "After behavior" ending (ADR-017). A normal
        interruption never comes here (``_stop_behavior`` always returns to the default rest pose)."""
        run = self._run
        assert run is not None
        status = run.status()
        settings = run.plan.settings
        action = settings.after_action
        delay = f"timeline delay {status['timeline_delay_ms']:.0f} ms"
        self.events.emit(
            "behavior_completed", ending=action, finished_by_operator=run.finish_rep is not None, **status
        )
        self._run = None  # handed over: the ending is not a cancellation
        if action == "hold":
            self.behavior_last_result = (
                f"'{run.plan.behavior_name}' completed ({delay}) — holding its last position"
            )
            self.commands.finish_active(
                CommandState.COMPLETED, "behavior completed — holding its last position"
            )
            self._set_connection(Connection.HOLDING, "behavior completed: hold last position")
            self.events.emit("behavior_ending_hold", hold_target=_r(self.sent.values or {}))
            self.message = (
                f"'{run.plan.behavior_name}' done — holding its last position (Go Rest returns to rest)"
            )
            return
        if action == "pose" and run.end_pose is not None:
            self.behavior_last_result = (
                f"'{run.plan.behavior_name}' completed ({delay}) — moving to pose '{run.end_pose.name}'"
            )
            start = (
                dict(self.sent.values) if self.sent.values is not None else dict(self.requested.values or {})
            )
            self._mark_active(
                CommandState.ENDING, f"behavior completed — After Behavior: pose '{run.end_pose.name}'"
            )
            self._set_owner_direction(run.end_direction)
            self._start_pose_move(
                run.end_pose, start, run.plan.entry_profile, f"ending of '{run.plan.behavior_name}'"
            )
            return
        self.behavior_last_result = f"'{run.plan.behavior_name}' completed ({delay}) — returning to rest"
        self._mark_active(CommandState.ENDING, "behavior completed — After Behavior: return to rest")
        self._start_rest("behavior completed: return to rest", from_last_target=True)

    def _on_move_to_pose(self, req: rq.MoveToPose) -> None:
        target = req.target
        if self.mode == Mode.SIM_ONLY:
            assert self.staged.values is not None
            goal, _ = self.sim_bounds.clamp(target.pose)  # the 3D arm only; display clamp to the model range
            now = self.clock()
            self._discard_intent()
            start = dict(self.staged.values)
            self._claim_motion(f"simulated arm moving to '{target.name}'", sim=True)
            self._set_owner_direction(
                req.direction or self._pose_direction(target.pose, f"pose '{target.name}'", target.id)
            )
            self._sim_rest = SmoothTransition.plan(self._overlay_adjusted(start, goal), goal, req.profile)
            self._sim_rest_clock = StallAwareClock(self._period * policy.TIMELINE_MAX_STEP_PERIODS, now)
            self._sim_motion_label = target.name
            self.requested = StageValue(start, now)
            self.requested_epoch += 1
            self.events.emit(
                "sim_pose_move_started", name=target.name, duration_s=round(self._sim_rest.duration, 2)
            )
            self.message = f"Simulated arm moving to '{target.name}'"
            return
        problems = pose_problems_for_arm(
            target, self.calibration_fp, None if self.hw_bounds is None else self.hw_bounds.as_tuples()
        )
        if problems:
            self.events.emit("pose_move_refused", name=target.name, problems=problems)
            self._reject(req, "; ".join(problems))
            return
        # Always a fresh read first. A running motion (behavior incl. a pending ending, pose move, rest
        # return) is SUPERSEDED: the move continues from the last commanded target (D9) — never a jump,
        # never an intermediate rest, and the old behavior's own ending is NOT run.
        frame = self._read_actual()
        if frame is None:
            return  # read failed: fault latched, nothing moved
        measured = frame.as_dict()
        start = self._handover_start(measured)
        self._take_over(measured, start)
        self._claim_motion(f"moving to pose '{target.name}'")
        self._set_owner_direction(
            req.direction or self._pose_direction(target.pose, f"pose '{target.name}'", target.id)
        )
        self._start_pose_move(target, start, req.profile, "operator Move to pose")

    def _on_finish_behavior(self, req: rq.FinishBehavior) -> None:
        run = self._run
        assert run is not None
        plan = run.plan
        point = run.point
        rep_now = 1 if point is None or point.segment == "entry" else point.repetition
        if point is not None and point.segment == "bridge":
            rep_now += 1  # already travelling to the next repetition's start: finish that one
        if plan.repeat_count is not None:
            rep_now = min(rep_now, plan.repeat_count)
        run.finish_rep = rep_now
        run.finish_tau = plan.nominal_duration_for(rep_now)
        self.events.emit("behavior_finish_requested", **run.status())
        ending = {
            "rest": "return to rest",
            "hold": "hold its last position",
            "pose": "move to its saved pose",
        }
        self.message = (
            f"FINISHING '{plan.behavior_name}' after repetition {rep_now}, then it will "
            f"{ending.get(plan.settings.after_action, 'return to rest')}"
        )
        self._mark_active(CommandState.RUNNING, f"finishing after repetition {rep_now}")
        self._cmd_done(f"'{plan.behavior_name}' finishes after repetition {rep_now}")

    def _start_pose_move(
        self, target: PoseTarget, start: Mapping[str, float], profile: Any, reason: str
    ) -> None:
        now = self.clock()
        move = SmoothTransition.plan(self._overlay_adjusted(start, target.pose), target.pose, profile)
        self._discard_intent()
        self.requested = StageValue(dict(start), now)
        self.requested_epoch += 1
        self._pose_move = PoseMove(
            target, move, StallAwareClock(self._period * policy.TIMELINE_MAX_STEP_PERIODS, now), reason
        )
        self._set_connection(Connection.MOVING_TO_POSE, f"move to pose '{target.name}' ({reason})")
        self.events.emit(
            "pose_move_started",
            profile=str(profile),
            start=_r(start),
            measured=None if self.actual.values is None else _r(self.actual.values),
            **self._pose_move.status(),
        )
        self.message = f"Moving to pose '{target.name}' — Go Rest returns to rest; EMERGENCY HOLD overrides"

    def _finish_pose_move(self, now: float) -> None:
        pm = self._pose_move
        assert pm is not None
        pm.done = True
        errors = (
            {n: round(self.actual.values[n] - pm.target.pose[n], 2) for n in JOINT_NAMES}
            if self.actual.values is not None
            else {}
        )
        self.pose_move_last_result = f"At pose '{pm.target.name}' — holding"
        self.commands.finish_active(CommandState.COMPLETED, f"at pose '{pm.target.name}' — holding")
        if pm.reason.startswith("ending of ") and self.behavior_last_result:
            done = self.behavior_last_result.split(" — ")[0]
            self.behavior_last_result = f"{done} — now holding at pose '{pm.target.name}'"
        self._set_connection(Connection.HOLDING, f"pose '{pm.target.name}' reached; holding")
        self.events.emit("pose_move_completed", errors=errors, **pm.status())
        self.message = f"At pose '{pm.target.name}' — holding with torque on"

    def _on_pause_behavior(self, req: rq.PauseBehavior) -> None:
        run = self._run
        assert run is not None
        frame = self._read_actual()
        if frame is None:
            return  # read failed: fault latched (cancels the run, no return to rest)
        now = self.clock()
        hold = frame.as_dict()
        run.paused = True
        run.resume_move = None
        run.resume_clock = None
        self._discard_intent()
        self.requested = StageValue(hold, now)
        self.requested_epoch += 1
        self.events.emit("behavior_paused", hold_pose=_r(hold), **run.status())
        self._mark_active(CommandState.PAUSED, "paused — holding the measured pose")
        self._cmd_done(f"'{run.plan.behavior_name}' paused")
        self.message = (
            f"PAUSED '{run.plan.behavior_name}' — holding the measured pose. Resume continues; "
            "Go Rest returns to rest; EMERGENCY HOLD / Torque OFF still override"
        )

    def _on_resume_behavior(self, req: rq.ResumeBehavior) -> None:
        run = self._run
        assert run is not None and run.paused
        frame = self._read_actual()  # verify the current state first
        if frame is None:
            return
        now = self.clock()
        measured = frame.as_dict()
        target = run.plan.at(run.clock.elapsed).pose  # where the frozen timeline stands
        move = SmoothTransition.plan(self._overlay_adjusted(measured, target), target, run.plan.entry_profile)
        run.paused = False
        self.requested = StageValue(measured, now)
        self.requested_epoch += 1
        if move.duration > 0.0:
            run.resume_move = move
            run.resume_clock = StallAwareClock(self._period * policy.TIMELINE_MAX_STEP_PERIODS, now)
        else:
            run.clock.rebase(now)
        self.events.emit(
            "behavior_resumed",
            resume_move_s=round(move.duration, 3),
            offset=_r({n: measured[n] - target[n] for n in JOINT_NAMES}),
            **run.status(),
        )
        self.message = f"PLAYING '{run.plan.behavior_name}' — resumed"
        self._mark_active(CommandState.RUNNING, "resumed")
        self._cmd_done(f"'{run.plan.behavior_name}' resumed")

    def _on_pause(self, req: rq.Pause) -> None:
        self.paused = True
        self.message = "Simulation paused"
        self.events.emit("paused")

    def _on_resume(self, req: rq.Resume) -> None:
        self.paused = False
        self.message = "Simulation resumed"
        self.events.emit("resumed")

    # ----------------------------------------------------------------- preemption (ADR-018)
    def _handover_start(self, measured: dict[str, float]) -> dict[str, float]:
        """Start pose of a new motion. A still arm starts from its fresh MEASURED pose. While a timed
        motion is in flight it starts from the LAST COMMANDED target (owner decision D9, ADR-015): the
        servos are still travelling there, and restarting at a lagging measured pose would snap the
        target back (up to 24° observed) — the discontinuous jump ADR-018 forbids."""
        if self.connection in IN_FLIGHT and self.sent.values is not None:
            self._start_origin = "last_target"
            return dict(self.sent.values)
        self._start_origin = "measured"
        return dict(measured)

    def _take_over(self, measured: Mapping[str, float], start: Mapping[str, float]) -> None:
        """Drop the current motion owner (behavior run incl. its future repetitions and a pending
        Finish, a pose move incl. an "After behavior" ending move, a rest return incl. an ending rest)
        WITHOUT any return to rest: it was superseded by the command being handled."""
        by = self._cmd[0] or "operator"
        dropped: list[str] = []
        if self._run is not None:
            run, self._run = self._run, None
            st = run.status()
            self.behavior_last_result = (
                f"'{run.plan.behavior_name}' superseded during {st['segment']} (repetition "
                f"{st['repetition']}) — no return to rest"
            )
            self.events.emit("behavior_superseded", by=by, finish_pending=run.finish_rep is not None, **st)
            dropped.append(f"behavior '{run.plan.behavior_name}'")
        if self._pose_move is not None:
            pm, self._pose_move = self._pose_move, None
            pm.done = True
            ending = pm.reason.startswith("ending of ")
            self.pose_move_last_result = f"Move to '{pm.target.name}' superseded"
            self.events.emit("pose_move_superseded", by=by, ending=ending, **pm.status())
            dropped.append(("ending " if ending else "") + f"move to '{pm.target.name}'")
        if self._rest_traj is not None:
            self._rest_traj = None
            self._rest_clock = None
            self.events.emit("rest_return_superseded", by=by)
            dropped.append("return to rest")
        if dropped:
            self.events.emit(
                "motion_superseded",
                by=by,
                source=self._cmd[1],
                superseded=dropped,
                starts_from=self._start_origin,
                measured=_r(measured),
                start=_r(start),
                lag=_r({n: start[n] - measured[n] for n in JOINT_NAMES}),
            )

    def _claim_motion(self, detail: str, *, sim: bool = False) -> None:
        """The command being handled now owns the (one) motion; the previous owner is superseded."""
        cid = self._cmd[0]
        if cid is None:
            return
        self.commands.activate(cid, detail)
        self._active_is_sim = sim

    def _mark_active(self, state: CommandState, detail: str) -> None:
        self.commands.mark(self.commands.active_id, state, detail)

    # ----------------------------------------------------------------- overlays (ADR-019)
    def _owned_joints(self) -> set[str]:
        owned = set()
        if self.follow.engaged:
            owned.add(PAN)
        if self.talk.engaged:
            owned.add(GRIPPER)
        return owned

    def _overlay_adjusted(self, start: Mapping[str, float], goal: Mapping[str, float]) -> dict[str, float]:
        """A motion's start with overlay-owned joints already at the goal: those joints are commanded by the
        overlay, so they must not stretch the motion's duration (D10 / T1)."""
        out = dict(start)
        for j in self._owned_joints():
            out[j] = goal[j]
        return out

    def _holding_with_overlays(self) -> bool:
        """D8: holding a pose with an overlay engaged keeps issuing the merged target (no overlay: the
        M1/M2 holding semantics — nothing new is sent)."""
        return (
            self.connection == Connection.HOLDING
            and self.mode == Mode.SIM_TO_REAL
            and (self.follow.engaged or self.talk.engaged)
        )

    def _pan_sign(self) -> float:
        return float(getattr(self.directions, "pan_sign", 1.0))

    def _pose_direction(self, pose: Mapping[str, float], source: str, key: Any = None) -> Direction | None:
        if self.directions is None:
            return None
        result: Direction = self.directions.analyze_pose(pose, source, key)
        return result

    def _behavior_direction(self, req: rq.PlayBehavior) -> Direction | None:
        if self.directions is None:
            return None
        result: Direction = self.directions.analyze_behavior(req.behavior, req.settings)
        return result

    def _rest_direction(self) -> Direction | None:
        if self._rest_dir is None:
            pose = self.rest_pose.pose if self.rest_pose is not None else DEFAULT_SIM_REST_POSE
            src = "default rest pose" if self.rest_pose is not None else "simulated rest pose"
            self._rest_dir = self._pose_direction(
                pose, src, ("rest", tuple(round(pose[n], 4) for n in JOINT_NAMES))
            )
        return self._rest_dir

    def _current_pose(self) -> dict[str, float] | None:
        if self.mode != Mode.SIM_ONLY and self.actual.values is not None:
            return dict(self.actual.values)
        src = self._base or (self.staged.values if self.staged.values is not None else self.requested.values)
        return None if src is None else dict(src)

    def _set_owner_direction(self, d: Direction | None) -> None:
        """The motion owner changed: its FIXED representative direction becomes the Follow Me reference
        (never recomputed per tick; a NORMAL/REVERSED label never flips during an owner's lifetime)."""
        self._owner_dir = d
        self._owner_from_sliders = False
        if self.follow.enabled:
            self._apply_follow_direction(d, "owner changed")

    def _apply_follow_direction(self, d: Direction | None, why: str) -> None:
        f = self.follow
        f.direction = d
        if d is None or not d.compatible:
            reason = (
                "no direction analysis available"
                if d is None
                else f"{d.source or 'active motion'} is {d.classification}: {d.reason}"
            )
            if f.suspended != reason:
                self.events.emit("follow_suspended", reason=reason, held_pan=f.pan_command)
            f.suspended = reason
            return
        if f.suspended is not None:
            self.events.emit("follow_resumed", by=why, reference=d.to_json())
        f.suspended = None
        f.solution = None

    def _front_key(self) -> str:
        if self.mode != Mode.SIM_ONLY and self.calibration_fp is not None:
            return self.calibration_fp
        return SIM_KEY

    def _load_front(self) -> None:
        key = self._front_key()
        front = self.front_store.load(key)
        model = getattr(self.directions, "model_id", None)
        fp = None if key == SIM_KEY else key
        if front is not None and (front.calibration_fingerprint != fp or (model and front.model_id != model)):
            self.events.emit("follow_front_ignored", reason="saved for another calibration or model")
            front = None
        self.front = front

    def _front_bearing(self) -> float:
        return self.front.bearing_deg if self.front is not None else 0.0

    def _last_command(self, joint: str) -> float:
        if self.mode == Mode.SIM_TO_REAL and self.sent.values is not None:
            return float(self.sent.values[joint])
        src = self.staged.values if self.staged.values is not None else self.requested.values
        return float(src[joint]) if src is not None else 0.0

    def _overlays_off(self, reason: str) -> None:
        """Emergency Hold / Torque OFF / fault / mode change: every overlay stops at once (no handoff)."""
        f = self.follow
        if f.enabled or f.handoff is not None:
            f.enabled, f.handoff, f.suspended, f.solution = False, None, None, None
            f.last_reason = reason
            self.events.emit("follow_disabled", reason=reason, override=True)
        self._talk_off(reason, override=True)

    def _talk_off(self, reason: str, *, override: bool = False) -> None:
        t = self.talk
        if t.active or t.handoff is not None:
            t.active, t.handoff, t.generator, t.lease_until = False, None, None, None
            t.last_reason = reason
            self.events.emit("talk_stopped", reason=reason, override=override)

    def _on_set_follow(self, req: rq.SetFollow) -> None:
        f = self.follow
        if not req.enabled:
            if not f.enabled:
                self._cmd_done("Follow Me already off")
                return
            base = self._base[PAN] if self._base is not None else self._last_command(PAN)
            current = f.pan_command if f.pan_command is not None else self._last_command(PAN)
            f.enabled, f.suspended = False, None
            f.handoff = Handoff.plan(current - base, *profile_limits(PAN, f.profile))
            f.last_reason = "stopped by operator"
            self.events.emit(
                "follow_disabled", reason="Stop following", handoff_s=round(f.handoff.duration, 3)
            )
            self.message = "Follow Me OFF — shoulder pan returns smoothly to the behavior / pose"
            self._cmd_done(f"pan handed back in {f.handoff.duration:.2f} s")
            return
        if Action.FOLLOW not in self.available_actions():
            self._reject(req, f"Follow Me is not available in {self.mode.value}/{self.connection.value}")
            return
        if req.target_deg is not None:
            if not math.isfinite(req.target_deg) or not -180.0 <= req.target_deg <= 180.0:
                self._reject(req, "target angle must be within [-180, 180] degrees")
                return
            f.target_deg = float(req.target_deg)
            f.target_updated_at = self.clock()
        f.profile = req.profile
        f.tracker.v_max, f.tracker.a_max = profile_limits(PAN, req.profile)
        if not f.enabled:
            start = (
                f.pan_command
                if f.handoff is not None and f.pan_command is not None
                else self._last_command(PAN)
            )
            f.tracker.reset(start, 0.0)  # starts from the last pan command: no jump
            f.enabled, f.handoff, f.solution = True, None, None
            f.pan_command = start
            d = None if self._owner_from_sliders else self._owner_dir
            if d is None:  # no motion owner, or the sliders reshaped the arm: measure the actual pose
                pose = self._current_pose()
                d = None if pose is None else self._pose_direction(pose, "current pose")
                self._owner_dir = d
            self._apply_follow_direction(d, "Follow Me enabled")
            self.events.emit(
                "follow_enabled",
                target_deg=f.target_deg,
                front=None if self.front is None else self.front.to_json(),
                reference=None if d is None else d.to_json(),
                start_pan=round(start, 3),
            )
        elif f.suspended is not None:  # D5: explicit operator action re-evaluates the reference
            pose = self._current_pose()
            d = None if pose is None else self._pose_direction(pose, "current pose (operator re-check)")
            self._owner_dir = d
            self._apply_follow_direction(d, "operator re-check")
        self.message = "Follow Me ON — it owns shoulder pan only"
        self._cmd_done("Follow Me on" + (f" (SUSPENDED: {f.suspended})" if f.suspended else ""))

    def _on_talk_start(self, req: rq.TalkStart) -> None:
        t = self.talk
        preset = TALK_PRESETS.get(req.intensity)
        if preset is None:
            self._reject(req, f"unknown intensity {req.intensity!r} (gentle | normal | excited)")
            return
        if not math.isfinite(req.lease_s) or req.lease_s <= 0:
            self._reject(req, "lease_s must be a positive number of seconds")
            return
        now = self.clock()
        lease = min(req.lease_s, TALK_LEASE_MAX_S)
        t.preset = preset
        t.lease_until = now + lease
        if t.active and t.generator is not None:  # renew; no second generator
            t.generator.set_preset(preset)
            t.renewed_at = now
            self._cmd_done(f"Talk Mode renewed ({preset.name}, lease {lease:g} s)")
            return
        bounds, _ = self.active_bounds()
        lo, hi = bounds.as_tuples()[GRIPPER]
        start = (
            t.gripper_command
            if t.handoff is not None and t.gripper_command is not None
            else self._last_command(GRIPPER)
        )
        t.generator = TalkGenerator(preset, start, lo, hi, seed=req.seed)
        t.active, t.handoff, t.seed = True, None, req.seed
        t.started_at = t.renewed_at = now
        t.gripper_command = start
        self.events.emit(
            "talk_started", intensity=preset.name, lease_s=lease, start=round(start, 2), seed=req.seed
        )
        self.message = "Talk Mode ON — it owns the gripper only"
        self._cmd_done(f"Talk Mode on ({preset.name}, lease {lease:g} s)")

    def _talk_stop_smooth(self, reason: str) -> None:
        t = self.talk
        base = self._base[GRIPPER] if self._base is not None else self._last_command(GRIPPER)
        current = t.gripper_command if t.gripper_command is not None else base
        t.active, t.generator, t.lease_until = False, None, None
        t.handoff = Handoff.plan(current - base, t.preset.v_max, t.preset.a_max)
        t.last_reason = reason
        self.events.emit(
            "talk_stopped", reason=reason, override=False, handoff_s=round(t.handoff.duration, 3)
        )

    def _on_talk_stop(self, req: rq.TalkStop) -> None:
        if not self.talk.active:
            self._cmd_done("Talk Mode already off")
            return
        self._talk_stop_smooth("stopped")
        self.message = "Talk Mode OFF — the gripper returns smoothly to the behavior / pose"
        self._cmd_done("Talk Mode off")

    def _on_set_front(self, req: rq.SetFrontReference) -> None:
        from so101_companion.devices.rest_pose import now_iso

        if req.reset:
            bearing, method = 0.0, "default (pan center)"
        elif req.from_current:
            pose = self._current_pose()
            d = None if pose is None else self._pose_direction(pose, "current pose")
            if pose is None or d is None or not d.compatible:
                why = "no pose" if d is None else d.reason
                self._reject(req, f"the current facing is not well defined ({why})")
                return
            assert d.psi_deg is not None
            bearing, method = wrap180(self._pan_sign() * pose[PAN] + d.psi_deg), "current facing"
        elif req.bearing_deg is not None and math.isfinite(req.bearing_deg):
            bearing, method = wrap180(req.bearing_deg), "manual"
        else:
            self._reject(req, "give a bearing, from_current or reset")
            return
        key = self._front_key()
        front = FrontReference(
            bearing,
            None if key == SIM_KEY else key,
            str(getattr(self.directions, "model_id", "unknown")),
            now_iso(),
            method,
        )
        where = self.front_store.save(key, front)
        self.front = front
        if self.follow.enabled:
            self.follow.solution = None
        self.events.emit("follow_front_set", front=front.to_json(), location=where)
        self.message = (
            f"Follow Me front set ({method}): angle 0 = bearing {bearing:+.1f}° from the pan center"
        )

    def _compose_overlays(self, now: float, *, frozen: bool) -> None:
        """Merge Follow Me (pan) and Talk Mode (gripper) onto the motion's own target, once per tick.
        ``self._base`` keeps the motion's target, so overlay output never feeds back into the base."""
        f, t = self.follow, self.talk
        target = self._take_follow_target()
        if target is not None:  # stored even while paused/off; applied when tracking (D4)
            f.target_updates += 1
            f.target_updated_at = now
            if abs(target - f.target_deg) >= TARGET_DEADBAND_DEG or not f.enabled:
                f.target_deg = target
        if self.requested.values is None:
            return
        if self.requested is not self._composed:  # the motion wrote a new target this tick
            self._base = dict(self.requested.values)
        base = self._base if self._base is not None else dict(self.requested.values)
        if (
            f.enabled
            and self._owner_from_sliders
            and self._slider_moved_at is not None
            and now - self._slider_moved_at >= SLIDER_SETTLE_S
        ):
            # the slider move settled: ONE re-measurement of the reshaped arm (event time, not per tick)
            d = self._pose_direction(base, "current pose (after slider move)")
            self._set_owner_direction(d)
            self.events.emit("follow_reference_remeasured", reference=None if d is None else d.to_json())
        if t.active and t.lease_until is not None and now >= t.lease_until:
            self._talk_stop_smooth("lease expired")
        if not (f.engaged or t.engaged):
            self._overlay_t = now
            self._composed = None
            return
        dt = (
            0.0
            if self._overlay_t is None
            else min(max(0.0, now - self._overlay_t), self._period * policy.TIMELINE_MAX_STEP_PERIODS)
        )
        self._overlay_t = now
        bounds, _ = self.active_bounds()
        b = bounds.as_tuples()
        out = dict(base)
        # ---- Follow Me: shoulder_pan
        if f.enabled:
            if frozen:  # Pause: the whole arm holds (base = the held pose); resume continues from here
                f.tracker.reset(base[PAN], 0.0)
                f.frozen = True
            else:
                f.frozen = False
                if f.suspended is None and f.direction is not None and f.direction.psi_deg is not None:
                    lo, hi = b[PAN]
                    prev = f.solution.bound if f.solution is not None else None
                    f.solution = solve_pan(
                        f.target_deg,
                        f.direction.psi_deg,
                        self._front_bearing(),
                        lo,
                        hi,
                        s=self._pan_sign(),
                        current_bound=prev,
                        current_pan=f.tracker.q,
                    )
                    goal = f.solution.pan_target
                else:  # SUSPENDED: hold the last valid target (D5)
                    goal = f.solution.pan_target if f.solution is not None else f.tracker.q
                out[PAN] = f.tracker.step(goal, dt)
            f.pan_command = out[PAN]
        elif f.handoff is not None:
            out[PAN] = f.handoff.step(base[PAN], 0.0 if frozen else dt)
            f.pan_command = out[PAN]
            if f.handoff.done:
                f.handoff = None
                self.events.emit("follow_handoff_done", pan=round(out[PAN], 3))
        # ---- Talk Mode: gripper
        if t.active and t.generator is not None:
            lo_g, hi_g = b[GRIPPER]
            if frozen:
                t.generator.reset_at(base[GRIPPER])
                t.frozen = True
            else:
                t.frozen = False
                out[GRIPPER] = min(max(t.generator.step(dt), lo_g), hi_g)
            t.gripper_command = out[GRIPPER]
        elif t.handoff is not None:
            out[GRIPPER] = t.handoff.step(base[GRIPPER], 0.0 if frozen else dt)
            t.gripper_command = out[GRIPPER]
            if t.handoff.done:
                t.handoff = None
                self.events.emit("talk_handoff_done", gripper=round(out[GRIPPER], 2))
        self.requested = StageValue(out, now)
        self._composed = self.requested

    def _follow_status(self, now: float) -> dict[str, Any]:
        f = self.follow
        d = f.direction if f.enabled else self._owner_dir
        sol = f.solution
        psi = d.psi_deg if d is not None and d.compatible else None
        front = self._front_bearing()
        measured_pan = (
            self.actual.values[PAN]
            if self.mode != Mode.SIM_ONLY and self.actual.values is not None
            else f.pan_command
        )
        facing = (
            None
            if psi is None or measured_pan is None
            else round(wrap180(self._pan_sign() * measured_pan + psi - front), 2)
        )
        if not f.enabled:
            state = "HANDING_BACK" if f.handoff is not None else "OFF"
        elif f.suspended is not None:
            state = "SUSPENDED"
        elif f.frozen:
            state = "PAUSED"
        elif sol is not None and f.pan_command is not None and abs(sol.pan_target - f.pan_command) > 5.0:
            state = "REORIENTING"
        elif sol is not None and sol.reach != "REACHABLE":
            state = "AT_LIMIT"  # never reported as tracking when the gripper cannot face the target
        else:
            state = "TRACKING"
        bounds, _ = self.active_bounds()
        lo, hi = bounds.as_tuples()[PAN]
        reach_range = None
        if psi is not None:
            a, c = reachable_theta(psi, front, lo, hi, self._pan_sign())
            reach_range = {"from_deg": round(a, 2), "to_deg": round(c, 2), "span_deg": round(hi - lo, 2)}
        return {
            "enabled": f.enabled,
            "state": state,
            "owns": "shoulder_pan" if f.engaged else None,
            "target_deg": round(f.target_deg, 3),
            "target_age_s": None
            if f.target_updated_at is None
            else round(max(0.0, now - f.target_updated_at), 3),
            "target_updates": f.target_updates,
            "transition": f.profile.value,
            "reach": None if sol is None or f.suspended is not None else sol.reach,
            "residual_deg": None if sol is None or f.suspended is not None else round(sol.residual_deg, 2),
            "pan_target": None if sol is None else round(sol.pan_target, 3),
            "pan_command": None if f.pan_command is None else round(f.pan_command, 3),
            "facing_estimate_deg": facing,
            "reachable_range": reach_range,
            "reference": None if d is None else d.to_json(),
            "suspended_reason": f.suspended,
            "front": {"bearing_deg": round(front, 3), "set": self.front is not None}
            | ({} if self.front is None else self.front.to_json()),
            "pan_sign": self._pan_sign(),
            "last_reason": f.last_reason,
        }

    def _talk_status(self, now: float) -> dict[str, Any]:
        t = self.talk
        if t.active:
            state = "PAUSED" if t.frozen else "ON"
        else:
            state = "STOPPING" if t.handoff is not None else "OFF"
        return {
            "active": t.active,
            "state": state,
            "owns": "gripper" if t.engaged else None,
            "intensity": t.preset.name,
            "gripper_command": None if t.gripper_command is None else round(t.gripper_command, 2),
            "lease_remaining_s": None if t.lease_until is None else round(max(0.0, t.lease_until - now), 3),
            "started_age_s": None if t.started_at is None or not t.active else round(now - t.started_at, 3),
            "renewed_age_s": None if t.renewed_at is None or not t.active else round(now - t.renewed_at, 3),
            "last_reason": t.last_reason,
            "max_rhythm_hz": 1.5,
        }

    # ===================================================================== tick
    def tick(self, now: float | None = None) -> None:
        start = time.perf_counter()
        now = self.clock() if now is None else now
        self.tick_seq += 1
        self._tick_times.append(now)
        self._recording_guard()
        if self.recorder.active and self.recorder.elapsed(now) > policy.RECORDING_MAX_DURATION_S:
            self._finish_recording(
                None, note=f"stopped automatically after {policy.RECORDING_MAX_DURATION_S:g} s"
            )
        intent = self._take_intent()
        reasons: list[str] = []
        sent_this_tick = False
        if intent is not None and Action.MOVE_SLIDERS in self.available_actions():
            self.requested = StageValue(intent, now)
            self._owner_from_sliders = True  # the operator reshapes the arm: its reference must follow
            self._slider_moved_at = now
            if self._sim_rest is not None:  # a slider move takes the simulated arm back
                self._sim_rest = None
                self._sim_rest_clock = None
                self.events.emit("sim_rest_cancelled", by="slider")
                if self._active_is_sim:
                    self.commands.finish_active(CommandState.CANCELLED, "a slider moved the simulated arm")
        if self.mode == Mode.SIM_ONLY and self._sim_rest is not None and not self.paused:
            assert self._sim_rest_clock is not None
            tau = self._sim_rest_clock.advance(now)
            self.requested = StageValue(self._sim_rest.at(tau), now)
            if self._active_is_sim:
                self.commands.note_first_send()
            if tau >= self._sim_rest.duration:
                self._sim_rest = None
                self._sim_rest_clock = None
                if self._sim_motion_label == "rest":
                    self.events.emit("sim_rest_reached")
                    self.message = "Simulated arm at rest"
                else:
                    self.events.emit("sim_pose_reached", name=self._sim_motion_label)
                    self.message = f"Simulated arm at pose '{self._sim_motion_label}'"
                if self._active_is_sim:
                    self.commands.finish_active(CommandState.COMPLETED, self.message + " (simulation only)")
        sim_frozen = False
        if self.mode == Mode.SIM_ONLY:
            with self._intent_lock:
                preview = self._sim_preview
            if preview is not None:  # D6: the Studio preview is the motion; the controller composes on it
                pose, pdir, sim_frozen = preview
                self.requested = StageValue(dict(pose), now)
                if pdir is not self._sim_preview_dir:
                    self._sim_preview_dir = pdir
                    self._set_owner_direction(pdir)
        if self.mode == Mode.SIM_ONLY:
            if not self.paused and self.requested.values is not None and self.staged.values is not None:
                self._compose_overlays(now, frozen=sim_frozen)
                reasons = self._advance_staged(self.sim_bounds, now)
                self.displayed = StageValue(dict(self.staged.values or {}), now)
                if self.recorder.active and self.recorder.origin is Origin.SIM_SLIDERS:
                    self.recorder.add(self.requested.values, now)
        elif (
            self.robot is not None
            and self.connection in READ_ONLY_POLLING
            and not self._holding_with_overlays()
        ):
            if self.tick_seq % self.cfg.app.observe_every_n_ticks == 0:
                self._read_actual()
        elif (
            self.robot is not None
            and self.mode == Mode.SIM_TO_REAL
            and (self.connection in MOTION or self._holding_with_overlays())
        ):
            assert self.hw_bounds is not None
            if self.connection == Connection.RETURNING_TO_REST and self._rest_traj is not None:
                assert self._rest_clock is not None
                logical = self._rest_traj.t0 + self._rest_clock.advance(now)  # stall-aware (D4)
                self.requested = StageValue(self._rest_traj.at(logical), now)
            pm = self._pose_move if self.connection == Connection.MOVING_TO_POSE else None
            if pm is not None:
                pose_tau = pm.clock.advance(now)  # stall-aware like every timed move
                self.requested = StageValue(pm.move.at(pose_tau), now)
            run = self._run if self.connection == Connection.PLAYING_BEHAVIOR else None
            if run is not None and run.paused:
                pass  # holding the measured pose; the timeline is frozen
            elif run is not None and run.resume_move is not None:
                assert run.resume_clock is not None
                tau = run.resume_clock.advance(now)  # stall-aware like every timed move
                self.requested = StageValue(run.resume_move.at(tau), now)
                if tau >= run.resume_move.duration:
                    run.resume_move = None
                    run.resume_clock = None
                    run.clock.rebase(now)  # the pause and the resume move are not playback time
            elif run is not None:
                tau = run.clock.advance(now)  # stall-aware playhead (D1)
                if run.finish_tau is not None and tau >= run.finish_tau:
                    # Finish: the chosen repetition's content is complete -> the run is complete
                    run.point = PlanPoint(run.plan.end_pose(), "done", run.finish_rep or 1, True)
                else:
                    run.point = run.plan.at(tau)
                self.requested = StageValue(run.point.pose, now)
            # D4: Pause freezes the behavior AND both overlays (the whole arm holds its measured pose)
            self._compose_overlays(now, frozen=run is not None and run.paused)
            prev_actual = dict(self.actual.values) if self.actual.values is not None else None
            prev_goal = self._goal_in_flight  # what the servo was executing during the last interval
            reasons = self._advance_staged(self.hw_bounds, now)
            try:
                frame = JointFrame(dict(self.staged.values or {}), now, self.tick_seq)
                sent_frame = self.robot.send(frame)
                self._record_sent(sent_frame)
                self._goal_in_flight = sent_frame.as_dict()
                sent_this_tick = True
                if self.connection in IN_FLIGHT:
                    self.commands.note_first_send()
            except Exception as exc:
                self._latch(exc, now)
            if (
                self.connection in MOTION or self._holding_with_overlays()
            ) and self.tick_seq % self.cfg.app.observe_every_n_ticks == 0:
                self._read_actual()
                self._check_divergence(prev_actual, prev_goal, now)
            if self.connection == Connection.RETURNING_TO_REST:
                self._check_rest_arrival(now)
            if (
                pm is not None
                and self.connection == Connection.MOVING_TO_POSE
                and pm.clock.elapsed >= pm.move.duration
            ):
                self._finish_pose_move(now)
            if (
                run is not None
                and self.connection == Connection.PLAYING_BEHAVIOR
                and run.point is not None
                and run.point.finished
            ):
                self._complete_behavior()
        self._recording_guard()
        self.clamp_reasons = tuple(reasons)
        self.cycle_ms = (time.perf_counter() - start) * 1000.0
        self.events.emit(
            TICK_KIND,
            seq=self.tick_seq,
            mode=self.mode.value,
            authority=self.connection.value,
            requested=self.requested.as_json(),
            staged=self.staged.as_json(),
            sent=self.sent.as_json() if sent_this_tick else None,
            actual=self.actual.as_json(),
            displayed=self.displayed.as_json(),
            clamp_reasons=list(reasons),
            behavior=None
            if self._run is None or self._run.point is None
            else {
                "id": self._run.plan.behavior_id,
                "revision": self._run.plan.behavior_revision,
                "segment": self._run.point.segment,
                "repetition": self._run.point.repetition,
                "t_logical_s": round(self._run.clock.elapsed, 4),
                "timeline_delay_ms": round(self._run.clock.delay_s * 1000.0, 1),
            },
            rest_timeline_delay_ms=None
            if self._rest_clock is None
            else round(self._rest_clock.delay_s * 1000.0, 1),
            last_observation_age_seconds=None
            if self.actual.t_mono is None
            else round(max(0.0, now - self.actual.t_mono), 4),
            cycle_duration_ms=round(self.cycle_ms, 3),
        )

    def _check_divergence(
        self, prev_actual: dict[str, float] | None, prev_goal: dict[str, float] | None, now: float
    ) -> None:
        """DIAGNOSTIC ONLY (fixed M1 policy, ADR-010): report a joint that accelerates away from the
        goals it was sent (safety/divergence.py). It never latches a fault, never changes torque and
        never stops sending — Emergency Hold / Torque Off remain manual operator decisions."""
        if self.connection not in MOTION or prev_actual is None or self.actual.values is None:
            return
        last_moves = self._last_moves
        self._last_moves = {n: self.actual.values[n] - prev_actual[n] for n in JOINT_NAMES}
        if prev_goal is None:
            return
        bad = detect_runaway(
            prev_actual,
            self.actual.values,
            prev_goal,
            self._goal_in_flight or prev_goal,
            last_moves,
            step=policy.DIVERGENCE_STEP,
            error=policy.DIVERGENCE_ERROR,
        )
        if not bad:
            return
        details = ", ".join(
            f"{n}: goal {prev_goal[n]:+.1f}, was {prev_actual[n]:+.1f}, now {self.actual.values[n]:+.1f}"
            for n in bad
        )
        new_warning = f"{', '.join(bad)} moving AWAY from its goal ({details})"
        if self.divergence_warning is None or now > self._divergence_warning_until:
            self.events.emit("tracking_divergence", joints=bad, details=details, action="none (diagnostic)")
        self.divergence_warning = new_warning
        self._divergence_warning_until = now + 2.0

    def _advance_staged(self, bounds: JointBounds, now: float) -> list[str]:
        """Fixed policy: staged = requested clamped to the active bounds. No step limiter."""
        assert self.requested.values is not None and self.staged.values is not None
        staged, reasons = bounds.clamp(self.requested.values, hold_outside=self.staged.values)
        self.staged = StageValue(staged, now, self.tick_seq)
        return reasons

    # ===================================================================== hardware helpers
    def _read_actual(self) -> JointFrame | None:
        robot = self.robot
        assert robot is not None
        try:
            frame = robot.observe()
        except Exception as exc:
            self._latch(exc, self.clock())
            return None
        self._record_actual(frame)
        return frame

    def _record_actual(self, frame: JointFrame) -> None:
        self.actual = StageValue(frame.as_dict(), frame.timestamp_mono_s, frame.seq)
        if (
            self.recorder.origin is Origin.REAL_HAND_GUIDED
            and self.connection == Connection.TORQUE_OFF
            and self.torque == Torque.OFF
        ):
            # hand guidance: every fresh MEASURED frame, stamped when it was received; nothing is sent
            self.recorder.add(frame.as_dict(), frame.timestamp_mono_s, frame.seq)
        self.last_success_read_at = frame.timestamp_mono_s
        if self.mode != Mode.SIM_ONLY:
            self.displayed = StageValue(frame.as_dict(), frame.timestamp_mono_s, frame.seq)

    def _record_sent(self, frame: JointFrame) -> None:
        self.command_seq += 1
        self.sent = StageValue(frame.as_dict(), frame.timestamp_mono_s, self.command_seq)
        self.last_success_send_at = frame.timestamp_mono_s

    def _latch(self, exc: Exception, now: float, code: str | None = None) -> None:
        self._latch_code(
            code or str(getattr(exc, "code", "UNEXPECTED_ERROR")), f"{type(exc).__name__}: {exc}", now=now
        )

    def _latch_code(
        self, code: str, details: str, *, torque_unknown: bool = True, now: float | None = None
    ) -> None:
        now = self.clock() if now is None else now
        self._discard_intent()
        if self.fault is not None:
            self.fault = self.fault.repeated(details, now)
        else:
            self.fault = Fault(code, details, now, now)
        if torque_unknown:
            self.torque = Torque.UNKNOWN
        self.actual = StageValue(self.actual.values, self.actual.t_mono, self.actual.seq, stale=True)
        self._set_connection(Connection.FAULT_LATCHED, f"fault {code}")
        self.message = f"FAULT LATCHED [{code}] — no further commands; manual recovery required"
        self.events.emit("fault_latched", code=code, details=details, torque=self.torque.value)

    def _set_connection(self, new: Connection, reason: str) -> None:
        if self.mode != Mode.SIM_ONLY and new not in OVERLAY_STATES:
            self._overlays_off(f"{new.value} ({reason})")  # Torque OFF, faults, Disarm, disconnect
        if new not in MOTION and self.commands.active_id is not None and not self._active_is_sim:
            # the motion command ends without completing: an override (Emergency Hold, Torque OFF,
            # Disarm, mode change) cancels it; a fault fails it. Completions finish it BEFORE this call.
            failed = new == Connection.FAULT_LATCHED
            self.commands.finish_active(
                CommandState.FAILED if failed else CommandState.CANCELLED,
                f"{'failed' if failed else 'cancelled'}: {new.value} ({reason})",
                cancelled_by=self._cmd[0],
            )
        if self._run is not None and new != Connection.PLAYING_BEHAVIOR:
            run, self._run = self._run, None
            if new != Connection.RETURNING_TO_REST:  # completion / Stop already reported themselves
                status = run.status()
                self.behavior_last_result = (
                    f"'{run.plan.behavior_name}' CANCELLED by {new.value} ({reason}) — no return to rest"
                )
                self.events.emit("behavior_cancelled", by=new.value, reason=reason, **status)
        if self._pose_move is not None and new != Connection.MOVING_TO_POSE:
            pm, self._pose_move = self._pose_move, None
            if not pm.done:
                self.pose_move_last_result = f"Move to '{pm.target.name}' CANCELLED by {new.value} ({reason})"
                self.events.emit("pose_move_cancelled", by=new.value, reason=reason, **pm.status())
        if new not in MOTION:
            self._goal_in_flight = None
        if new != Connection.RETURNING_TO_REST:
            self._rest_traj = None
            self._rest_clock = None
        if new not in MOTION:
            self._last_moves = None
        if new != self.connection:
            self.events.emit(
                "state_transition", previous=self.connection.value, connection=new.value, reason=reason
            )
            self.connection = new

    # ===================================================================== snapshot
    def tick_hz(self) -> float:
        times = [t for t in self._tick_times if t >= (self._tick_times[-1] - 2.0)] if self._tick_times else []
        if len(times) < 2 or times[-1] == times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def snapshot(self, now: float | None = None) -> Snapshot:
        now = self.clock() if now is None else now
        actual = self.actual
        if actual.values is not None and not actual.stale:
            too_old = actual.t_mono is not None and now - actual.t_mono > self.cfg.app.stale_after_s
            if too_old or self.connection in {Connection.FAULT_LATCHED, Connection.DISCONNECTED}:
                actual = StageValue(actual.values, actual.t_mono, actual.seq, stale=True)
        bounds, label = self.active_bounds()
        return Snapshot(
            seq=self.tick_seq,
            t_mono=now,
            mode=self.mode,
            connection=self.connection,
            torque=self.torque,
            hardware_authorized=self.hardware_authorized,
            calibration_valid=self.calibration_valid,
            paused=self.paused,
            requested=self.requested,
            staged=self.staged,
            sent=self.sent,
            actual=actual,
            displayed=self.displayed,
            clamp_reasons=self.clamp_reasons,
            fault=self.fault,
            bounds=bounds.as_tuples(),
            bounds_label=label,
            available_actions=self.available_actions(),
            last_success_read_at=self.last_success_read_at,
            last_success_send_at=self.last_success_send_at,
            command_seq=self.command_seq,
            tick_hz_actual=self.tick_hz(),
            cycle_ms=self.cycle_ms,
            overruns=self.overruns,
            message=self.message,
            port_label=self.port_label,
            requested_epoch=self.requested_epoch,
            extra={
                "robot": self.robot.describe() if self.robot is not None else {},
                "divergence_warning": self.divergence_warning
                if now <= self._divergence_warning_until
                else None,
                "rest_pose": None
                if self.rest_pose is None
                else {
                    "pose": dict(self.rest_pose.pose),
                    "clamped_joints": list(self.rest_pose.clamped_joints),
                    "recorded_at": self.rest_pose.recorded_at,
                },
                "rest_progress": None
                if self._rest_traj is None
                else round(min(1.0, max(0.0, self._rest_elapsed() / self._rest_traj.duration)), 3),
                "pose_move": None
                if self._pose_move is None
                else self._pose_move.status() | {"state": "moving"},
                "pose_move_last_result": self.pose_move_last_result,
                "sim_motion": None if self._sim_rest is None else self._sim_motion_label,
                "sim_rest_progress": None
                if self._sim_rest is None or self._sim_rest_clock is None
                else round(min(1.0, self._sim_rest_clock.elapsed / max(self._sim_rest.duration, 1e-9)), 3),
                "rest_status": {"state": self.rest_status[0], "detail": self.rest_status[1]},
                "emergency_latched": self.emergency_latched,
                "active_command": self.commands.active(),
                "follow": self._follow_status(now),
                "talk": self._talk_status(now),
                "legacy_rest_pose": None
                if self._legacy_rest is None
                else {"pose": dict(self._legacy_rest.pose), "recorded_at": self._legacy_rest.recorded_at},
                "calibration_fingerprint": self.calibration_fp,
                "hw_bounds": None if self.hw_bounds is None else self.hw_bounds.as_tuples(),
                "recording": self.recorder.status(now)
                if self.recorder.active
                else self.last_recording_status,
                "behavior": self._run.status()
                if self._run is not None
                else {"state": "idle", "last_result": self.behavior_last_result},
            },
        )


def _describe(req: rq.Request) -> dict[str, Any]:
    """Small, JSON-safe description of a tracked request for its ledger record."""
    if isinstance(req, rq.PlayBehavior):
        return {
            "behavior_id": req.behavior.id,
            "behavior_name": req.behavior.name,
            "settings": req.settings.to_json(),
            "getting_into_position": req.entry_profile.value,
            "between_repetitions": req.bridge_profile.value,
        }
    if isinstance(req, rq.MoveToPose):
        return {"pose_id": req.target.id, "pose_name": req.target.name, "transition": req.profile.value}
    return {}


def _r(values: Mapping[str, float]) -> dict[str, float]:
    return {k: round(values[k], 3) for k in JOINT_NAMES}


_MODE_LABEL = {
    Mode.SIM_ONLY: "Simulation Only",
    Mode.REAL_TO_SIM: "Real → Simulation",
    Mode.SIM_TO_REAL: "Simulation → Real",
}

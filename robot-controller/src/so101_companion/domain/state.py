"""Controller state model and immutable telemetry snapshots (interfaces.md "Status model")."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


class Mode(enum.StrEnum):
    SIM_ONLY = "SIM_ONLY"
    REAL_TO_SIM = "REAL_TO_SIM"
    SIM_TO_REAL = "SIM_TO_REAL"


class Connection(enum.StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED_READ_ONLY = "CONNECTED_READ_ONLY"
    ARMED = "ARMED"
    RETURNING_TO_REST = "RETURNING_TO_REST"  # Normal Stop: timed trajectory to the recorded rest pose
    PLAYING_BEHAVIOR = "PLAYING_BEHAVIOR"  # M2: the behavior player owns the targets (ADR-013)
    MOVING_TO_POSE = "MOVING_TO_POSE"  # Studio: smooth move to a named pose, then hold (ADR-017)
    HOLDING = "HOLDING"
    FAULT_LATCHED = "FAULT_LATCHED"
    TORQUE_OFF = "TORQUE_OFF"


class Torque(enum.StrEnum):
    ON = "ON"
    OFF = "OFF"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "N/A"


class Action(enum.StrEnum):
    """Operator actions. The coordinator publishes which ones are currently permitted."""

    SET_MODE_SIM_ONLY = "set_mode_sim_only"
    SET_MODE_REAL_TO_SIM = "set_mode_real_to_sim"
    SET_MODE_SIM_TO_REAL = "set_mode_sim_to_real"
    CONNECT = "connect"
    ENABLE_MOVEMENT = "enable_movement"
    DISARM = "disarm"
    EMERGENCY_HOLD = "emergency_hold"
    TORQUE_OFF = "torque_off"
    ENABLE_TORQUE = "enable_torque"
    RECOVER = "recover"
    DISCONNECT = "disconnect"
    PAUSE = "pause"
    RESUME = "resume"
    MOVE_SLIDERS = "move_sliders"
    RECORD_REST_POSE = "record_rest_pose"
    STOP_TO_REST = "stop_to_rest"
    ADOPT_REST_POSE = "adopt_rest_pose"  # D5: explicitly adopt a legacy (fingerprint-less) rest pose
    START_REAL_RECORDING = "start_real_recording"  # M2: hand-guided capture, torque OFF
    START_SIM_RECORDING = "start_sim_recording"  # M2: slider capture in SIM_ONLY
    STOP_RECORDING = "stop_recording"
    PLAY_BEHAVIOR = "play_behavior"  # M2: previewed + confirmed real playback
    STOP_BEHAVIOR = "stop_behavior"  # M2: normal interruption -> return to rest
    PAUSE_BEHAVIOR = "pause_behavior"  # Studio: hold at the measured pose, timeline suspended
    RESUME_BEHAVIOR = "resume_behavior"  # Studio: fresh read, smooth move back onto the trajectory
    ENTER_MODE_REAL_TO_SIM = "enter_mode_real_to_sim"  # Studio mode workflow (connects if needed)
    ENTER_MODE_SIM_TO_REAL = "enter_mode_sim_to_real"
    MOVE_TO_POSE = "move_to_pose"  # Studio: named pose (real arm, or the 3D arm in Simulation Only)
    FINISH_BEHAVIOR = "finish_behavior"  # Studio: complete the current loop, then the behavior's ending
    FOLLOW = "follow"  # M2.6: Follow Me (shoulder_pan overlay) may be enabled
    TALK = "talk"  # M2.6: Talk Mode (gripper overlay) may be started
    SET_FRONT = "set_front"  # M2.6: the Follow Me front reference may be set from the current facing


@dataclass(frozen=True)
class Fault:
    code: str
    details: str
    first_at: float
    last_at: float
    acknowledged: bool = False
    count: int = 1

    def repeated(self, details: str, now: float) -> Fault:
        return Fault(self.code, details, self.first_at, now, self.acknowledged, self.count + 1)


@dataclass(frozen=True)
class StageValue:
    """One telemetry stage. ``values is None`` means *not available*, never zero."""

    values: Mapping[str, float] | None = None
    t_mono: float | None = None
    seq: int | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        if self.values is not None and not isinstance(self.values, MappingProxyType):
            object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def get(self, name: str) -> float | None:
        return None if self.values is None else self.values.get(name)

    def as_json(self) -> dict[str, float] | None:
        return None if self.values is None else {k: round(v, 4) for k, v in self.values.items()}


NOT_AVAILABLE = StageValue()


@dataclass(frozen=True)
class Snapshot:
    """Everything the UI may display. Produced only by the controller; immutable."""

    seq: int
    t_mono: float
    mode: Mode
    connection: Connection
    torque: Torque
    hardware_authorized: bool
    calibration_valid: bool
    paused: bool
    requested: StageValue
    staged: StageValue
    sent: StageValue
    actual: StageValue
    displayed: StageValue
    clamp_reasons: tuple[str, ...]
    fault: Fault | None
    bounds: Mapping[str, tuple[float, float]]
    bounds_label: str
    available_actions: frozenset[Action]
    last_success_read_at: float | None
    last_success_send_at: float | None
    command_seq: int
    tick_hz_actual: float
    cycle_ms: float
    overruns: int
    message: str = ""
    port_label: str = ""
    requested_epoch: int = 0  # bumps when the CONTROLLER (not the UI) resets the requested pose
    extra: Mapping[str, Any] = field(default_factory=dict)

    def age(self, t: float | None, now: float) -> float | None:
        return None if t is None else max(0.0, now - t)

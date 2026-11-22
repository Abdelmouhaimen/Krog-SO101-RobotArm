"""Studio redesign: controller workflows behind the new UI (fake hardware, fake clock).

* One-dropdown mode workflow (``EnterMode``): connect if needed, then mirror + torque OFF, or control +
  torque on from a fresh read + movement enabled. It fails safe and reports the failure.
* Pause/Resume of physical playback: hold at the measured pose, timeline frozen, resume from a fresh read
  with a smooth move back onto the trajectory. Never a return to rest.
* Go Rest from a still, held arm; Go Rest of the SIMULATED arm in Simulation Only (no hardware call).
* Transition presets (Gentle / Normal / Fast) within ADR-015's limits; part of the plan hash.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from helpers import FAKE_PORT, HW_POSE, Rig, make_rig
from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.plan import SmoothTransition, compile_plan
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.service import BehaviorService
from so101_companion.control import requests as rq
from so101_companion.devices.rest_pose import FileRestPoseStore
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Action, Connection, Mode, Torque
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

PERIOD = 0.05


def sends(rig: Rig) -> list[dict[str, float]]:
    return [a for n, a in rig.robot.calls if n == "send"] if rig.robots else []


def ready(calibration_file: Path, tmp_path: Path) -> tuple[Rig, BehaviorService]:
    rig = make_rig(calibration_file)
    rig.coordinator.rest_store = FileRestPoseStore(tmp_path / "config")
    rig.run(rq.EnterMode(Mode.SIM_TO_REAL, FAKE_PORT, str(calibration_file), confirmed=True))
    rig.run(rq.RecordRestPose(confirmed=True))
    return rig, BehaviorService(rig.coordinator, BehaviorRepository(tmp_path / "behaviors"))


def wave(fp: str, seconds: float = 2.0) -> object:
    n = round(seconds / PERIOD) + 1
    samples = tuple(
        Sample(
            round(i * PERIOD, 6), actual={**HW_POSE, "elbow_flex": HW_POSE["elbow_flex"] + 20.0 * i / (n - 1)}
        )
        for i in range(n)
    )
    return new_behavior("wave", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=fp)


# ------------------------------------------------------------------ mode workflow
def test_real_to_sim_connects_mirrors_and_turns_torque_off_only_when_confirmed(
    calibration_file: Path,
) -> None:
    rig = make_rig(calibration_file)
    rig.run(rq.EnterMode(Mode.REAL_TO_SIM, FAKE_PORT, str(calibration_file), confirmed=False))
    assert rig.robots == [] and rig.snap().mode == Mode.SIM_ONLY  # nothing touched without confirmation
    rig.run(rq.EnterMode(Mode.REAL_TO_SIM, FAKE_PORT, str(calibration_file), confirmed=True))
    s = rig.snap()
    assert s.mode == Mode.REAL_TO_SIM and s.connection == Connection.TORQUE_OFF and s.torque == Torque.OFF
    assert "send" not in [n for n, _ in rig.robot.calls]  # mirroring never commands
    assert Action.START_REAL_RECORDING in s.available_actions
    assert rig.coordinator.events.last("mode_workflow_completed")["mode"] == "REAL_TO_SIM"


def test_sim_to_real_enables_torque_from_a_fresh_read_then_arms(calibration_file: Path) -> None:
    rig = make_rig(calibration_file)
    rig.run(rq.EnterMode(Mode.REAL_TO_SIM, FAKE_PORT, str(calibration_file), confirmed=True))
    rig.robot.move_by_hand(elbow_flex=HW_POSE["elbow_flex"] + 25.0)  # the arm was moved while limp
    rig.tick(2)
    n = len(rig.robot.calls)
    rig.run(rq.EnterMode(Mode.SIM_TO_REAL, confirmed=True))
    names = [c for c, _ in rig.robot.calls[n:]]
    assert "enable_torque_holding_present" in names and "send" not in names  # goal := present first
    s = rig.snap()
    assert s.mode == Mode.SIM_TO_REAL and s.connection == Connection.ARMED and s.torque == Torque.ON
    rig.tick()
    first = sends(rig)[-1]
    assert first["elbow_flex"] == pytest.approx(HW_POSE["elbow_flex"] + 25.0, abs=0.05)  # no stale target


def test_failed_workflow_reports_and_leaves_a_safe_state(calibration_file: Path, tmp_path: Path) -> None:
    rig = make_rig(calibration_file)
    rig.run(rq.EnterMode(Mode.SIM_TO_REAL, FAKE_PORT, str(tmp_path / "missing.json"), confirmed=True))
    s = rig.snap()
    assert s.mode == Mode.SIM_ONLY and s.connection == Connection.DISCONNECTED
    failed = rig.coordinator.events.last("mode_workflow_failed")
    assert failed is not None and failed["mode"] == "SIM_TO_REAL" and "not entered" in s.message
    rig.run(rq.EnterMode(Mode.SIM_TO_REAL, "", "", confirmed=True))  # nothing configured
    assert "Advanced" in rig.snap().message and rig.robots == []


def test_enable_torque_failure_mid_workflow_latches_and_reports(calibration_file: Path) -> None:
    rig = make_rig(calibration_file)
    rig.run(rq.EnterMode(Mode.REAL_TO_SIM, FAKE_PORT, str(calibration_file), confirmed=True))
    rig.robot.faults.unplugged = True
    rig.run(rq.EnterMode(Mode.SIM_TO_REAL, confirmed=True))
    s = rig.snap()
    assert s.connection == Connection.FAULT_LATCHED and s.torque == Torque.UNKNOWN
    assert rig.coordinator.events.last("mode_workflow_failed") is not None
    assert "send" not in [n for n, _ in rig.robot.calls]


def test_mode_cannot_change_while_playing_and_sim_only_keeps_the_arm_held(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service = ready(calibration_file, tmp_path)
    b = service.repository.create(wave(rig.snap().extra["calibration_fingerprint"]))  # type: ignore[arg-type]
    rig.run(rq.PlayBehavior(b, PlaybackSettings(repeat_count=None), None, confirmed=True))
    assert Action.ENTER_MODE_REAL_TO_SIM not in rig.snap().available_actions
    rig.run(rq.EnterMode(Mode.REAL_TO_SIM, confirmed=True))
    assert rig.snap().connection == Connection.PLAYING_BEHAVIOR and "cannot change mode" in rig.snap().message
    rig.run(rq.StopBehavior())
    while rig.snap().connection == Connection.RETURNING_TO_REST:
        rig.tick()
    rig.run(rq.EnterMode(Mode.SIM_ONLY, confirmed=True))
    s = rig.snap()
    assert s.mode == Mode.SIM_ONLY and s.torque == Torque.ON and rig.coordinator.robot is not None


# ------------------------------------------------------------------ pause / resume (physical)
def test_pause_holds_measured_pose_and_resume_rejoins_smoothly(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service = ready(calibration_file, tmp_path)
    b = service.repository.create(wave(rig.snap().extra["calibration_fingerprint"]))  # type: ignore[arg-type]
    rig.run(rq.PlayBehavior(b, PlaybackSettings(repeat_count=1), None, confirmed=True))
    rig.tick(15)
    rig.run(rq.PauseBehavior())
    beh = rig.snap().extra["behavior"]
    assert beh["state"] == "paused" and rig.snap().connection == Connection.PLAYING_BEHAVIOR
    frozen = beh["t_logical_s"]
    hold = rig.robot.observe().as_dict()
    rig.tick(20)  # a long pause: the timeline does not move, the arm holds the measured pose
    assert rig.snap().extra["behavior"]["t_logical_s"] == frozen
    assert all(abs(t["elbow_flex"] - hold["elbow_flex"]) < 1e-6 for t in sends(rig)[-15:])
    assert Action.PAUSE_BEHAVIOR not in rig.snap().available_actions
    assert rig.snap().connection != Connection.RETURNING_TO_REST  # Pause never returns to rest
    rig.robot.move_by_hand(elbow_flex=hold["elbow_flex"] - 12.0)  # someone pushed it while paused
    n = len(rig.robot.calls)
    rig.run(rq.ResumeBehavior())
    assert rig.robot.calls[n][0] == "observe"  # the current state is verified first
    resumed = rig.coordinator.events.last("behavior_resumed")
    assert resumed is not None and resumed["resume_move_s"] > 0
    k = len(sends(rig))
    while rig.snap().connection == Connection.PLAYING_BEHAVIOR:
        rig.tick()
    path = [hold | {"elbow_flex": hold["elbow_flex"] - 12.0}, *sends(rig)[k:]]
    for a, c in itertools.pairwise(path):  # smooth: no jump back to where the timeline stood
        for j in JOINT_NAMES:
            assert abs(c[j] - a[j]) <= policy.TRANSITION_MAX_SPEED[j] * PERIOD + 1e-6, j
    done = rig.coordinator.events.last("behavior_completed")
    assert done is not None and done["timeline_delay_ms"] < 200  # the pause is not counted as a stall


def test_emergency_hold_still_overrides_a_paused_run(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    b = service.repository.create(wave(rig.snap().extra["calibration_fingerprint"]))  # type: ignore[arg-type]
    rig.run(rq.PlayBehavior(b, PlaybackSettings(repeat_count=None), None, confirmed=True))
    rig.tick(5)
    rig.run(rq.PauseBehavior(), rq.EmergencyHold())
    s = rig.snap()
    assert s.connection == Connection.HOLDING and s.extra["behavior"]["state"] == "idle"
    assert "CANCELLED" in s.extra["behavior"]["last_result"]


# ------------------------------------------------------------------ Go Rest
def test_go_rest_from_a_held_arm(calibration_file: Path, tmp_path: Path) -> None:
    rig, _ = ready(calibration_file, tmp_path)
    rig.request(elbow_flex=HW_POSE["elbow_flex"] + 15.0)
    rig.tick(10)
    rig.run(rq.EmergencyHold())
    assert Action.STOP_TO_REST in rig.snap().available_actions
    rig.run(rq.StopToRest())
    assert rig.snap().connection == Connection.RETURNING_TO_REST
    while rig.snap().connection == Connection.RETURNING_TO_REST:
        rig.tick()
    assert rig.snap().connection == Connection.HOLDING
    assert rig.coordinator.events.last("rest_reached") is not None


def test_go_rest_in_simulation_moves_only_the_simulated_arm(calibration_file: Path) -> None:
    rig = make_rig(calibration_file)
    rig.request(elbow_flex=40.0, shoulder_lift=10.0)
    rig.tick(10)
    assert Action.STOP_TO_REST in rig.snap().available_actions
    rig.run(rq.StopToRest())
    started = rig.coordinator.events.last("sim_rest_started")
    assert started is not None and started["source"] == "default simulated rest pose"
    for _ in range(200):
        rig.tick()
        if rig.coordinator.events.last("sim_rest_reached") is not None:
            break
    goal = started["goal"]
    shown = rig.snap().displayed.values
    assert all(abs(shown[n] - goal[n]) < 0.05 for n in JOINT_NAMES)
    assert rig.robots == []  # never touched hardware
    rig.run(rq.StopToRest())
    rig.tick(2)
    rig.request(elbow_flex=0.0)  # a slider move cancels the simulated return
    rig.tick()
    assert rig.coordinator.events.last("sim_rest_cancelled") is not None


# ------------------------------------------------------------------ transition presets
def test_presets_are_ordered_within_limits_and_part_of_the_plan_hash(
    calibration_file: Path, tmp_path: Path
) -> None:
    start = dict.fromkeys(JOINT_NAMES, 0.0)
    goal = start | {"elbow_flex": 80.0, "shoulder_pan": 60.0}
    d = {p: SmoothTransition.plan(start, goal, p).duration for p in TransitionProfile}
    assert d[TransitionProfile.FAST] < d[TransitionProfile.NORMAL] < d[TransitionProfile.GENTLE]
    for p in TransitionProfile:
        seg = SmoothTransition.plan(start, goal, p)
        for j, v in seg.peak_speeds().items():
            assert v <= policy.TRANSITION_MAX_SPEED[j] * p.speed_scale + 1e-9
        for j, a in seg.peak_accels().items():
            assert a <= policy.TRANSITION_MAX_ACCEL[j] * p.accel_scale + 1e-9
    gentle_elbow = SmoothTransition.plan(start, start | {"elbow_flex": 90.0}, TransitionProfile.GENTLE)
    assert gentle_elbow.peak_speeds()["elbow_flex"] <= 35.0  # about the old 30 °/s feel
    assert SmoothTransition.plan(start, start | {"elbow_flex": 0.5}, TransitionProfile.GENTLE).duration == 0.0
    rig, service = ready(calibration_file, tmp_path)
    b = service.repository.create(wave(rig.snap().extra["calibration_fingerprint"]))  # type: ignore[arg-type]
    fast = compile_plan(b, b.settings, HW_POSE, bounds=None, calibration_fingerprint=None)  # type: ignore[arg-type]
    gentle = compile_plan(  # type: ignore[arg-type]
        b, b.settings, HW_POSE, bounds=None, calibration_fingerprint=None,
        entry_profile=TransitionProfile.GENTLE,
    )  # fmt: skip
    assert fast.content_token != gentle.content_token
    rig.tick()
    prepared = service.prepare_preview(b, b.settings, rig.snap())  # type: ignore[arg-type]
    prepared.session.step(prepared.session.duration + 1)
    approval = prepared.session.approval()
    rig.run(rq.PlayBehavior(b, b.settings, approval, confirmed=True, entry_profile=TransitionProfile.GENTLE))  # type: ignore[arg-type]
    assert rig.snap().connection != Connection.PLAYING_BEHAVIOR  # previewed with other presets
    assert "differs from the preview" in rig.snap().message

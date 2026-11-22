"""ADR-017 in the real controller loop (fake hardware, fake clock): per-behavior endings and named poses.

* Hold last position / Return to default rest / Move to a saved pose — executed only when ALL
  repetitions completed; a normal interruption always returns to the default rest pose.
* Missing or deleted ending poses and calibration mismatches are refused BEFORE anything moves.
* Moves to poses are smooth (per-joint limits), stall-aware, interruptible by Go Rest and overridden by
  Emergency Hold / Torque OFF. Global Go Rest still targets the designated default rest pose.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from helpers import FAKE_PORT, HW_POSE, Rig, make_rig
from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.poses import PoseSource
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.service import BehaviorService
from so101_companion.control import requests as rq
from so101_companion.devices.rest_pose import FileRestPoseStore
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Action, Connection, Mode
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

PERIOD = 0.05
OTHER_FP = "sha256:" + "cd" * 32


def sends(rig: Rig) -> list[dict[str, float]]:
    return [a for n, a in rig.robot.calls if n == "send"]


def ready(calibration_file: Path, tmp_path: Path) -> tuple[Rig, BehaviorService]:
    rig = make_rig(calibration_file)
    rig.coordinator.rest_store = FileRestPoseStore(tmp_path / "config")
    rig.run(rq.EnterMode(Mode.SIM_TO_REAL, FAKE_PORT, str(calibration_file), confirmed=True))
    rig.run(rq.RecordRestPose(confirmed=True))  # default rest pose = HW_POSE
    return rig, BehaviorService(rig.coordinator, BehaviorRepository(tmp_path / "behaviors"))


def gesture(rig: Rig, service: BehaviorService, reps: int | None = 1):  # type: ignore[no-untyped-def]
    fp = rig.snap().extra["calibration_fingerprint"]
    samples = tuple(
        Sample(round(i * PERIOD, 6), actual=HW_POSE | {"elbow_flex": HW_POSE["elbow_flex"] + 1.0 * i})
        for i in range(21)
    )
    b = service.repository.create(
        new_behavior("talk", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=fp)
    )
    return service.repository.set_settings(b, PlaybackSettings(repeat_count=reps))


def idle_pose(rig: Rig, service: BehaviorService, fp: str | None = None):  # type: ignore[no-untyped-def]
    fp = fp or rig.snap().extra["calibration_fingerprint"]
    values = HW_POSE | {"shoulder_pan": HW_POSE["shoulder_pan"] + 30.0, "wrist_flex": 25.0}
    return service.poses.create("Talking Idle", values, PoseSource.REAL_ARM, calibration_fingerprint=fp)


def play(rig: Rig, service: BehaviorService, b) -> None:  # type: ignore[no-untyped-def]
    service.play(
        b, b.settings, None, confirmed=True, enable_torque=False, entry_profile=TransitionProfile.FAST
    )
    rig.coordinator.process_requests()


def run_while(rig: Rig, state: Connection, limit: int = 600) -> None:
    for _ in range(limit):
        if rig.snap().connection != state:
            return
        rig.tick()
    raise AssertionError(f"still {state}")


# ------------------------------------------------------------------ endings after completion
def test_hold_last_position_does_not_return_to_rest(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    b = service.set_after(gesture(rig, service), "hold")
    play(rig, service, b)
    run_while(rig, Connection.PLAYING_BEHAVIOR)
    s = rig.snap()
    assert s.connection == Connection.HOLDING
    assert rig.coordinator.events.last("stop_to_rest_started") is None  # no return to rest
    assert rig.coordinator.events.last("behavior_completed")["ending"] == "hold"
    last = sends(rig)[-1]
    assert last["elbow_flex"] == HW_POSE["elbow_flex"] + 20.0  # the gesture's final pose is held
    n = len(sends(rig))
    rig.tick(10)
    assert len(sends(rig)) == n  # HOLDING sends nothing new: the servos keep the last target
    assert "holding its last position" in s.extra["behavior"]["last_result"]


def test_default_and_existing_behaviors_return_to_rest(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    b = gesture(rig, service)
    path = service.repository.path_for(b.id)
    doc = json.loads(path.read_text())
    del doc["settings"]["after"]  # a behavior file written before ADR-017
    path.write_text(json.dumps(doc))
    old = service.load(b.id)
    assert old.settings.after_action == "rest"
    play(rig, service, old)
    run_while(rig, Connection.PLAYING_BEHAVIOR)
    assert rig.snap().connection == Connection.RETURNING_TO_REST
    assert rig.coordinator.events.last("stop_to_rest_started")["goal"]["elbow_flex"] == HW_POSE["elbow_flex"]


def test_move_to_saved_pose_ending_is_smooth_and_holds(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    b = service.set_after(gesture(rig, service), "pose", idle.id)
    assert service.load(b.id).settings.after_pose_id == idle.id  # saved with the behavior
    play(rig, service, b)
    run_while(rig, Connection.PLAYING_BEHAVIOR)
    assert rig.snap().connection == Connection.MOVING_TO_POSE
    assert rig.snap().extra["pose_move"]["name"] == "Talking Idle"
    k = len(sends(rig)) - 1
    run_while(rig, Connection.MOVING_TO_POSE)
    assert rig.snap().connection == Connection.HOLDING
    path = sends(rig)[k:]
    for a, c in itertools.pairwise(path):  # no instantaneous jump anywhere in the ending
        for j in JOINT_NAMES:
            assert abs(c[j] - a[j]) <= policy.TRANSITION_MAX_SPEED[j] * PERIOD + 1e-6, j
    assert all(abs(path[-1][j] - idle.pose[j]) < 1e-6 for j in JOINT_NAMES)
    done = rig.coordinator.events.last("pose_move_completed")
    assert done is not None and done["name"] == "Talking Idle"
    assert rig.coordinator.events.last("stop_to_rest_started") is None


# ------------------------------------------------------------------ refused before anything moves
def test_missing_or_deleted_ending_pose_is_refused(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    b = service.set_after(gesture(rig, service), "pose", idle.id)
    service.delete_pose(idle.id)
    n = len(sends(rig))
    play(rig, service, b)
    s = rig.snap()
    assert s.connection != Connection.PLAYING_BEHAVIOR and "no longer exists" in s.message
    assert len(sends(rig)) == n
    try:
        service.prepare_preview(b, b.settings, s)
    except Exception as exc:  # the 3D preview explains it too
        assert "no longer exists" in str(exc)
    else:
        raise AssertionError("preview of a missing ending pose must fail")


def test_pose_from_another_calibration_is_refused(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    foreign = idle_pose(rig, service, fp=OTHER_FP)
    b = service.set_after(gesture(rig, service), "pose", foreign.id)
    play(rig, service, b)
    assert rig.snap().connection != Connection.PLAYING_BEHAVIOR
    assert "another calibration" in rig.snap().message
    service.move_to_pose(foreign.id, TransitionProfile.NORMAL, confirmed=True)
    rig.coordinator.process_requests()
    assert rig.snap().connection != Connection.MOVING_TO_POSE
    assert rig.coordinator.events.last("pose_move_refused") is not None


# ------------------------------------------------------------------ interruption vs completion
def test_until_stopped_interruption_uses_the_normal_rest_policy(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service = ready(calibration_file, tmp_path)
    b = service.set_after(gesture(rig, service, reps=None), "hold")
    play(rig, service, b)
    rig.tick(30)
    rig.run(rq.StopBehavior())
    assert rig.snap().connection == Connection.RETURNING_TO_REST  # not treated as a completion
    assert rig.coordinator.events.last("behavior_completed") is None
    assert rig.coordinator.events.last("behavior_stopped") is not None


def test_stop_during_a_finite_run_also_returns_to_rest(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    b = service.set_after(gesture(rig, service, reps=3), "pose", idle.id)
    play(rig, service, b)
    rig.tick(10)
    rig.run(rq.StopBehavior())
    assert rig.snap().connection == Connection.RETURNING_TO_REST
    assert rig.coordinator.events.last("pose_move_started") is None


# ------------------------------------------------------------------ overrides during an ending
def test_emergency_hold_during_the_ending_transition(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    b = service.set_after(gesture(rig, service), "pose", idle.id)
    play(rig, service, b)
    run_while(rig, Connection.PLAYING_BEHAVIOR)
    rig.tick(2)
    assert rig.snap().connection == Connection.MOVING_TO_POSE
    rig.run(rq.EmergencyHold())
    s = rig.snap()
    assert s.connection == Connection.HOLDING
    cancelled = rig.coordinator.events.last("pose_move_cancelled")
    assert cancelled is not None and cancelled["by"] == "HOLDING"
    assert rig.coordinator.events.last("pose_move_completed") is None
    assert "CANCELLED" in s.extra["pose_move_last_result"]


def test_go_rest_during_a_pose_move_returns_to_the_default_rest(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    rig.run(rq.EmergencyHold())  # a still, held arm
    service.move_to_pose(idle.id, TransitionProfile.GENTLE, confirmed=True)
    rig.coordinator.process_requests()
    assert rig.snap().connection == Connection.MOVING_TO_POSE
    rig.tick(5)
    assert Action.STOP_TO_REST in rig.snap().available_actions
    rig.run(rq.StopToRest())
    started = rig.coordinator.events.last("stop_to_rest_started")
    assert rig.snap().connection == Connection.RETURNING_TO_REST and started["starts_from"] == "last_target"
    assert started["goal"]["shoulder_pan"] == HW_POSE["shoulder_pan"]  # always the DEFAULT rest pose
    run_while(rig, Connection.RETURNING_TO_REST)
    assert rig.snap().connection == Connection.HOLDING


def test_torque_off_during_a_pose_move(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    service.move_to_pose(idle.id, TransitionProfile.NORMAL, confirmed=True)
    rig.coordinator.process_requests()
    rig.tick(3)
    rig.run(rq.TorqueOff(confirmed=True))
    assert rig.snap().connection == Connection.TORQUE_OFF
    assert rig.coordinator.events.last("pose_move_cancelled")["by"] == "TORQUE_OFF"


# ------------------------------------------------------------------ direct moves to poses
def test_move_to_pose_from_a_held_arm_reads_first_and_arrives(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    rig.robot.move_by_hand(elbow_flex=HW_POSE["elbow_flex"] + 2.0)
    rig.tick()
    n = len(rig.robot.calls)
    service.move_to_pose(idle.id, TransitionProfile.FAST, confirmed=False)
    rig.coordinator.process_requests()
    assert rig.snap().connection != Connection.MOVING_TO_POSE  # explicit confirmation required
    service.move_to_pose(idle.id, TransitionProfile.FAST, confirmed=True)
    rig.coordinator.process_requests()
    assert rig.robot.calls[n][0] == "observe"  # fresh read before anything is commanded
    run_while(rig, Connection.MOVING_TO_POSE)
    assert rig.snap().connection == Connection.HOLDING
    assert all(abs(sends(rig)[-1][j] - idle.pose[j]) < 1e-6 for j in JOINT_NAMES)


def test_move_to_pose_in_simulation_only_moves_the_3d_arm(calibration_file: Path, tmp_path: Path) -> None:
    rig = make_rig(calibration_file)
    service = BehaviorService(rig.coordinator, BehaviorRepository(tmp_path / "behaviors"))
    p = service.poses.create(
        "Wave up", dict.fromkeys(JOINT_NAMES, 0.0) | {"elbow_flex": 45.0}, PoseSource.SIMULATION
    )
    service.move_to_pose(p.id, TransitionProfile.NORMAL, confirmed=True)
    rig.coordinator.process_requests()
    for _ in range(200):
        rig.tick()
        if rig.coordinator.events.last("sim_pose_reached") is not None:
            break
    assert rig.snap().displayed.values["elbow_flex"] == 45.0 and rig.robots == []


# ------------------------------------------------------------------ ending an "until stopped" run elsewhere
def test_finish_completes_the_current_loop_then_runs_the_ending(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service = ready(calibration_file, tmp_path)
    b = service.set_after(gesture(rig, service, reps=None), "hold")
    play(rig, service, b)
    while rig.snap().extra["behavior"]["repetition"] < 2:
        rig.tick()
    rig.tick(3)  # inside repetition 2
    rig.run(rq.FinishBehavior())
    beh = rig.snap().extra["behavior"]
    assert beh["finish_after_repetition"] == 2 and "FINISHING" in rig.snap().message
    assert Action.FINISH_BEHAVIOR not in rig.snap().available_actions  # asked once
    run_while(rig, Connection.PLAYING_BEHAVIOR)
    done = rig.coordinator.events.last("behavior_completed")
    assert done["finished_by_operator"] is True and done["repetition"] == 2 and done["ending"] == "hold"
    assert rig.snap().connection == Connection.HOLDING
    assert sends(rig)[-1]["elbow_flex"] == HW_POSE["elbow_flex"] + 20.0  # the loop played to its end
    assert rig.coordinator.events.last("stop_to_rest_started") is None


def test_finish_with_a_pose_ending_and_during_a_bridge(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    b = service.set_after(gesture(rig, service, reps=None), "pose", idle.id)
    play(rig, service, b)
    while rig.snap().extra["behavior"]["segment"] != "bridge":
        rig.tick()
    rep = rig.snap().extra["behavior"]["repetition"]
    rig.run(rq.FinishBehavior())  # already travelling to the next start: that repetition is finished
    assert rig.snap().extra["behavior"]["finish_after_repetition"] == rep + 1
    run_while(rig, Connection.PLAYING_BEHAVIOR)
    assert rig.snap().connection == Connection.MOVING_TO_POSE
    run_while(rig, Connection.MOVING_TO_POSE)
    assert rig.snap().connection == Connection.HOLDING
    assert all(abs(sends(rig)[-1][j] - idle.pose[j]) < 1e-6 for j in JOINT_NAMES)


def test_go_to_a_pose_while_playing_interrupts_smoothly(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    idle = idle_pose(rig, service)
    b = gesture(rig, service, reps=None)
    play(rig, service, b)
    rig.tick(12)
    assert Action.MOVE_TO_POSE in rig.snap().available_actions
    k = len(sends(rig)) - 1
    service.move_to_pose(idle.id, TransitionProfile.FAST, confirmed=True)
    rig.coordinator.process_requests()
    assert rig.snap().connection == Connection.MOVING_TO_POSE
    superseded = rig.coordinator.events.last("behavior_superseded")  # ADR-018: superseded, no rest
    assert superseded is not None and rig.coordinator.events.last("stop_to_rest_started") is None
    assert rig.coordinator.events.last("behavior_completed") is None  # an interruption, not a completion
    run_while(rig, Connection.MOVING_TO_POSE)
    path = sends(rig)[k:]
    for a, c in itertools.pairwise(path):  # continues from the last target: no jump
        for j in JOINT_NAMES:
            assert abs(c[j] - a[j]) <= policy.TRANSITION_MAX_SPEED[j] * PERIOD + 1e-6, j
    assert rig.snap().connection == Connection.HOLDING
    assert all(abs(path[-1][j] - idle.pose[j]) < 1e-6 for j in JOINT_NAMES)


def test_go_to_an_unusable_pose_while_playing_keeps_playing(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = ready(calibration_file, tmp_path)
    foreign = idle_pose(rig, service, fp=OTHER_FP)
    play(rig, service, gesture(rig, service, reps=None))
    rig.tick(5)
    service.move_to_pose(foreign.id, TransitionProfile.FAST, confirmed=True)
    rig.coordinator.process_requests()
    assert (
        rig.snap().connection == Connection.PLAYING_BEHAVIOR and "another calibration" in rig.snap().message
    )

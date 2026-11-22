"""ADR-019 Follow Me + Talk Mode in the REAL controller loop (fake hardware, fake clock, real FK metadata).

Covers stable tracking while a behavior animates, survival across behavior replacement / pose requests /
natural endings / Go Rest, following while holding, normal↔reversed reorientation and unreachable
targets, ambiguous behaviors (suspended), smooth handback, Pause/Resume, Emergency Hold / Torque OFF /
faults, simultaneous Follow Me + Talk Mode + behavior, Talk lease and idempotency, Simulation Only, and
the persisted front reference. One target per tick, all through the single controller.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from overlay_helpers import NORMAL_POSE, REVERSED_POSE, OverlayEnv, pan_of, without
from so101_companion.control import requests as rq
from so101_companion.control.front import FileFrontStore
from so101_companion.domain.state import Action, Connection, Mode
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

PERIOD = 0.05
PAN_STEP = policy.TRANSITION_MAX_SPEED["shoulder_pan"] * PERIOD + 1e-6  # Fast-preset per-tick pan bound


def assert_pan_continuous(sends: list[dict[str, float]]) -> None:
    for a, b in itertools.pairwise(pan_of(sends)):
        assert abs(b - a) <= PAN_STEP, (a, b)


@pytest.fixture
def env(calibration_file: Path, tmp_path: Path) -> OverlayEnv:
    return OverlayEnv(calibration_file, tmp_path)


def follow_on(
    env: OverlayEnv, target: float = 0.0, profile: TransitionProfile = TransitionProfile.FAST
) -> None:
    env.run(rq.SetFollow(True, target, profile))
    assert env.follow()["enabled"]


def settle(env: OverlayEnv, n: int = 80) -> None:
    env.tick(n)


# ------------------------------------------------------------------ tracking while a behavior animates
def test_talking_loop_animates_four_joints_while_pan_tracks(calibration_file: Path, tmp_path: Path) -> None:
    """The behavior's non-pan joints are byte-identical with and without Follow Me; pan follows."""
    runs = {}
    for follow in (False, True):
        env = OverlayEnv(calibration_file, tmp_path / str(follow))
        b = env.behavior("talk", NORMAL_POSE, reps=None)
        if follow:
            follow_on(env, 30.0)
            settle(env)
        k = len(env.sends())
        env.play(b)
        env.tick(120)
        runs[follow] = (env, env.sends()[k:])
    (_, plain), (env, followed) = runs[False], runs[True]
    assert without({"shoulder_pan"}, plain) == without({"shoulder_pan"}, followed)
    f = env.follow()
    assert f["state"] == "TRACKING" and f["reach"] == "REACHABLE"
    assert pan_of(followed)[-1] == pytest.approx(f["pan_target"], abs=2e-3)
    assert f["facing_estimate_deg"] == pytest.approx(30.0, abs=1.0)  # NORMAL psi ≈ 0
    assert all(p == pytest.approx(pan_of(followed)[-1], abs=2e-3) for p in pan_of(followed)[-40:])  # steady


def test_overlays_survive_replacement_poses_endings_and_go_rest(env: OverlayEnv) -> None:
    watching = env.pose("Watching", NORMAL_POSE | {"elbow_flex": 45.0, "wrist_flex": 25.0})
    talking = env.behavior("talking", NORMAL_POSE, reps=None)
    celebrate = env.behavior(
        "celebrate", NORMAL_POSE, "wrist_flex", reps=1, after="pose", pose_id=watching.id
    )
    follow_on(env, -20.0)
    env.run(rq.TalkStart("normal", 60.0, seed=4))
    env.play(talking)
    env.tick(40)
    env.play(celebrate)  # supersedes Talking: overlays untouched
    assert env.follow()["enabled"] and env.talk()["active"]
    env.run_while(Connection.PLAYING_BEHAVIOR)
    assert env.snap().connection == Connection.MOVING_TO_POSE  # its saved ending, no rest in between
    assert not env.events("stop_to_rest_started")
    env.run_while(Connection.MOVING_TO_POSE)
    assert env.snap().connection == Connection.HOLDING and env.follow()["enabled"] and env.talk()["active"]
    # Go Rest (D3): five joints to rest, pan keeps following; Talk is cancelled
    env.run(rq.StopToRest())
    assert not env.talk()["active"] and env.follow()["enabled"]
    env.run_while(Connection.RETURNING_TO_REST)
    assert env.snap().connection == Connection.HOLDING
    assert not env.events("rest_not_reached")  # pan is not judged against the rest pose
    assert env.follow()["pan_target"] == pytest.approx(pan_of(env.sends())[-1], abs=2e-3)
    assert_pan_continuous(env.sends())


def test_follow_off_keeps_the_full_six_joint_go_rest(env: OverlayEnv) -> None:
    env.run(rq.SetFollow(True, 40.0, TransitionProfile.FAST))
    settle(env)
    env.run(rq.SetFollow(False))
    settle(env)
    env.run(rq.StopToRest())
    env.run_while(Connection.RETURNING_TO_REST)
    assert env.snap().connection == Connection.HOLDING
    assert env.sends()[-1]["shoulder_pan"] == pytest.approx(NORMAL_POSE["shoulder_pan"])  # rest pan reached


# ------------------------------------------------------------------ holding, handback, latest value
def test_following_while_holding_and_smooth_handback(env: OverlayEnv) -> None:
    pose = env.pose("Idle", NORMAL_POSE | {"elbow_flex": 40.0})
    env.service.move_to_pose(pose.id, TransitionProfile.FAST, confirmed=True)
    env.c.process_requests()
    env.run_while(Connection.MOVING_TO_POSE)
    n = len(env.sends())
    env.tick(5)
    assert len(env.sends()) == n  # HOLDING without overlays: nothing new is sent (M1/M2 semantics)
    follow_on(env, 50.0)
    env.tick(10)
    assert len(env.sends()) == n + 10  # D8: while an overlay is engaged the merged target is issued
    settle(env)
    assert env.follow()["pan_target"] == pytest.approx(50.0 + NORMAL_POSE["shoulder_pan"] * 0, abs=1.0)
    for t in (10.0, 20.0, -35.0):  # only the latest value counts
        env.c.set_follow_target(t)
    settle(env)
    # latest value only: three writes between two ticks are ONE applied update (never a queue)
    assert env.follow()["target_deg"] == -35.0 and env.follow()["target_updates"] == 1
    env.run(rq.SetFollow(False))  # Stop following: smooth handback to the pose's own pan
    assert env.follow()["state"] == "HANDING_BACK"
    settle(env)
    assert env.follow()["state"] == "OFF"
    assert env.sends()[-1]["shoulder_pan"] == pytest.approx(pose.pose["shoulder_pan"], abs=2e-3)
    m = len(env.sends())
    env.tick(5)
    assert len(env.sends()) == m  # handback done: holding semantics again
    assert_pan_continuous(env.sends()[n:])


# ------------------------------------------------------------------ normal <-> reversed, unreachable
def test_switching_normal_to_reversed_is_one_deliberate_reorientation(env: OverlayEnv) -> None:
    normal = env.behavior("normal", NORMAL_POSE, "wrist_flex", reps=None)
    rev = env.behavior("reversed", REVERSED_POSE, reps=None)
    follow_on(env, 150.0)
    env.play(normal)
    settle(env, 60)
    f = env.follow()
    assert (f["reference"]["classification"] == "NORMAL" and f["reach"] == "LIMITED") or f[
        "reach"
    ] == "UNREACHABLE"
    k = len(env.sends())
    env.play(rev)  # psi jumps by 180°: target pan 150 − 180 = −30
    env.tick(2)
    assert env.follow()["state"] == "REORIENTING"
    settle(env, 80)
    f = env.follow()
    assert f["reference"]["classification"] == "REVERSED" and f["reach"] == "REACHABLE"
    assert f["pan_target"] == pytest.approx(150.0 - 180.0, abs=1.0) and f["state"] == "TRACKING"
    pans = pan_of(env.sends()[k:])
    settle_tol = 0.5 * policy.TRANSITION_MAX_ACCEL["shoulder_pan"] * PERIOD**2  # tracker discretization
    assert all(b - a < settle_tol for a, b in itertools.pairwise(pans))  # one monotonic move, no flips
    assert_pan_continuous(env.sends()[k:])


def test_unreachable_target_is_reported_never_tracking(env: OverlayEnv) -> None:
    rev = env.behavior("reversed", REVERSED_POSE, reps=None)
    follow_on(env, 0.0)  # front = pan center: a reversed behavior cannot face it
    env.play(rev)
    settle(env, 80)
    f = env.follow()
    assert f["reach"] == "UNREACHABLE" and abs(f["residual_deg"]) > 60.0 and f["state"] == "AT_LIMIT"
    assert abs(f["pan_command"]) == pytest.approx(108.4, abs=0.2)  # the demo arm's calibrated bound
    lo, hi = env.snap().bounds["shoulder_pan"]
    assert all(lo - 1e-9 <= p <= hi + 1e-9 for p in pan_of(env.sends()))


# ------------------------------------------------------------------ ambiguous behaviors (D5)
def test_ambiguous_behavior_suspends_tracking_until_a_clear_owner(env: OverlayEnv) -> None:
    crossing = env.behavior("crossing", REVERSED_POSE, sweep={"shoulder_lift": (-60.0, 20.0)}, reps=None)
    clear = env.pose("Clear", NORMAL_POSE | {"elbow_flex": 35.0})
    follow_on(env, 20.0)
    settle(env)
    held = env.follow()["pan_command"]
    env.play(crossing)
    f = env.follow()
    assert f["state"] == "SUSPENDED" and "MIXED" in f["suspended_reason"] and f["reach"] is None
    k = len(env.sends())
    env.c.set_follow_target(-40.0)  # new targets are kept but do not move the pan while suspended
    env.tick(60)
    assert all(p == pytest.approx(held, abs=2e-3) for p in pan_of(env.sends()[k:]))
    assert len({s["shoulder_lift"] for s in env.sends()[k:]}) > 10  # the other joints keep animating
    env.service.move_to_pose(clear.id, TransitionProfile.FAST, confirmed=True)  # a clear new owner
    env.c.process_requests()
    assert env.follow()["state"] in ("TRACKING", "REORIENTING") and env.events("follow_resumed")
    settle(env)
    assert env.follow()["pan_target"] == pytest.approx(-40.0, abs=1.0)


def test_explicit_operator_recheck_resumes_from_a_clear_current_pose(env: OverlayEnv) -> None:
    crossing = env.behavior(
        "crossing", REVERSED_POSE, sweep={"shoulder_lift": (-60.0, 20.0)}, reps=1, after="hold"
    )
    follow_on(env, 10.0)
    env.play(crossing)
    env.run_while(Connection.PLAYING_BEHAVIOR)  # ends holding a NORMAL-side configuration
    assert env.follow()["state"] == "SUSPENDED"
    env.run(rq.SetFollow(True))  # explicit operator action re-evaluates from the current pose
    assert env.follow()["state"] in ("TRACKING", "REORIENTING")


# ------------------------------------------------------------------ pause / resume (D4)
def test_pause_freezes_both_overlays_and_resume_continues_smoothly(env: OverlayEnv) -> None:
    b = env.behavior("talk", NORMAL_POSE, reps=None)
    follow_on(env, 0.0)
    env.run(rq.TalkStart("normal", 60.0, seed=2))
    env.play(b)
    env.tick(30)
    env.run(rq.PauseBehavior())
    env.tick(2)
    k = len(env.sends())
    env.c.set_follow_target(60.0)  # received while paused: stored, no motion
    env.tick(40)
    frozen = env.sends()[k:]
    assert len({(s["shoulder_pan"], s["gripper"]) for s in frozen}) == 1
    assert env.follow()["state"] == "PAUSED" and env.talk()["state"] == "PAUSED"
    assert env.follow()["target_deg"] == 60.0
    env.run(rq.ResumeBehavior())
    env.tick(60)
    after = env.sends()[k + len(frozen) - 1 :]
    assert_pan_continuous(after)  # no catch-up jump
    assert env.follow()["pan_target"] == pytest.approx(60.0 + NORMAL_POSE["shoulder_pan"] * 0, abs=1.0)
    assert len({round(s["gripper"], 3) for s in after}) > 5  # Talk animates again


# ------------------------------------------------------------------ overrides
@pytest.mark.parametrize("override", ["hold", "torque_off", "fault"])
def test_emergency_hold_torque_off_and_faults_override_every_overlay(env: OverlayEnv, override: str) -> None:
    b = env.behavior("talk", NORMAL_POSE, reps=None)
    follow_on(env, 30.0)
    env.run(rq.TalkStart("excited", 60.0, seed=1))
    env.play(b)
    env.tick(20)
    if override == "hold":
        env.run(rq.EmergencyHold())
    elif override == "torque_off":
        env.run(rq.TorqueOff(confirmed=True))
    else:
        env.rig.robot.faults.fail_write_after = 0
        env.tick(2)
    assert not env.follow()["enabled"] and not env.talk()["active"]
    assert env.follow()["state"] == "OFF" and env.talk()["state"] == "OFF"
    n = len(env.sends())
    env.c.set_follow_target(-50.0)
    env.tick(10)
    assert len(env.sends()) == n  # nothing animates after the override
    rec, _ = env.api.follow_set(True, 0.0, TransitionProfile.NORMAL, None)
    env.c.process_requests()
    assert env.api.command(rec["id"])["state"] == "rejected"  # the API cannot restart it
    if override == "hold":
        assert "Emergency Hold is latched" in env.api.command(rec["id"])["detail"]
        stop, _ = env.api.talk_stop(None)  # turning an overlay off is always allowed
        env.c.process_requests()
        assert env.api.command(stop["id"])["state"] == "completed"


# ------------------------------------------------------------------ Talk Mode
def test_follow_talk_and_behavior_run_simultaneously_and_stop_independently(env: OverlayEnv) -> None:
    b = env.behavior("talk", NORMAL_POSE, reps=None)
    follow_on(env, 25.0)
    env.run(rq.TalkStart("normal", 60.0, seed=9))
    env.play(b)
    env.tick(100)
    window = env.sends()[-60:]
    grips = [s["gripper"] for s in window]
    assert min(grips) >= 0.0 and max(grips) <= 3.0 + 20.0 + 1e-6 and len(set(grips)) > 10
    assert env.follow()["state"] == "TRACKING" and env.talk()["state"] == "ON"
    env.run(rq.TalkStop())  # only the gripper changes owner
    assert env.talk()["state"] == "STOPPING" and env.follow()["enabled"]
    env.tick(40)
    assert env.talk()["state"] == "OFF"
    env.run(rq.SetFollow(False))  # only the pan changes owner; the behavior keeps playing
    env.tick(40)
    assert env.follow()["state"] == "OFF" and env.snap().connection == Connection.PLAYING_BEHAVIOR
    # after both handbacks the six joints are the behavior's again (gripper/pan follow the recording)
    run_pose = env.snap().extra["behavior"]
    assert run_pose["state"] == "playing"
    for a, b2 in itertools.pairwise(env.sends()):
        assert abs(b2["gripper"] - a["gripper"]) <= policy.TRANSITION_MAX_SPEED["gripper"] * PERIOD + 1e-6


def test_talk_lease_duplicates_and_idempotent_stop(env: OverlayEnv) -> None:
    env.run(rq.TalkStart("gentle", 1.0, seed=3))
    gen = env.c.talk.generator
    env.tick(10)
    env.run(rq.TalkStart("normal", 1.0))  # renew: the same generator, new intensity next segment
    assert env.c.talk.generator is gen and env.talk()["intensity"] == "normal"
    env.tick(30)  # 1.5 s > lease: stops by itself (a crashed client cannot leave it running)
    assert env.talk()["state"] in ("STOPPING", "OFF") and env.talk()["last_reason"] == "lease expired"
    env.tick(30)
    assert env.talk()["state"] == "OFF"
    rec, _ = env.api.talk_stop(None)
    env.c.process_requests()
    assert (
        env.api.command(rec["id"])["state"] == "completed"
        and "already off" in env.api.command(rec["id"])["detail"]
    )


def test_one_target_per_tick_through_the_single_controller(env: OverlayEnv) -> None:
    b = env.behavior("talk", NORMAL_POSE, reps=None)
    follow_on(env, 10.0)
    env.run(rq.TalkStart("excited", 60.0, seed=5))
    env.play(b)
    n0, t0 = len(env.sends()), env.c.tick_seq
    for i in range(200):
        env.c.set_follow_target(float((i * 37) % 300 - 150))  # a noisy 20 Hz stream
        env.tick()
    assert len(env.sends()) - n0 == env.c.tick_seq - t0  # exactly one merged send per tick
    assert_pan_continuous(env.sends()[n0:])


# ------------------------------------------------------------------ Simulation Only (D6)
def test_simulation_only_composes_on_the_simulated_arm(calibration_file: Path, tmp_path: Path) -> None:
    env = OverlayEnv(calibration_file, tmp_path, connect=False)
    assert Action.FOLLOW in env.snap().available_actions and Action.TALK in env.snap().available_actions
    env.run(rq.SetFollow(True, 45.0, TransitionProfile.FAST), rq.TalkStart("normal", 60.0, seed=1))
    env.tick(60)
    d = env.snap().displayed.values
    f = env.follow()
    assert f["state"] == "TRACKING" and d["shoulder_pan"] == pytest.approx(f["pan_target"], abs=2e-3)
    # the Studio preview feeds the controller; the preview pose is the motion, overlays compose on it
    preview = dict(REVERSED_POSE)
    rev_dir = env.service.directions.analyze_pose(REVERSED_POSE, "preview")
    env.c.set_sim_preview(preview, rev_dir, paused=False)
    env.tick(80)
    d = env.snap().displayed.values
    assert d["elbow_flex"] == pytest.approx(REVERSED_POSE["elbow_flex"])  # base joints = preview
    assert env.follow()["reference"]["classification"] == "REVERSED"
    assert d["shoulder_pan"] == pytest.approx(env.follow()["pan_target"], abs=2e-3)
    env.c.set_sim_preview(preview, rev_dir, paused=True)  # preview paused -> overlays frozen
    env.tick(2)
    g = env.snap().displayed.values["gripper"]
    env.tick(20)
    assert env.snap().displayed.values["gripper"] == pytest.approx(g)
    assert not env.rig.robots  # never any robot


# ------------------------------------------------------------------ front reference (D2)
def test_front_reference_is_set_from_the_current_facing_and_persisted(
    calibration_file: Path, tmp_path: Path
) -> None:
    env = OverlayEnv(calibration_file, tmp_path)
    rev = env.pose("Rev", REVERSED_POSE)
    env.service.move_to_pose(rev.id, TransitionProfile.FAST, confirmed=True)
    env.c.process_requests()
    env.run_while(Connection.MOVING_TO_POSE)
    env.run(rq.SetFrontReference(from_current=True))
    front = env.follow()["front"]
    assert front["set"] and front["method"] == "current facing"
    assert front["bearing_deg"] == pytest.approx(REVERSED_POSE["shoulder_pan"] + 180.0, abs=1.0)  # ≈ 105°
    follow_on(env, 0.0)
    settle(env)
    f = env.follow()
    assert f["reach"] == "REACHABLE" and f["pan_target"] == pytest.approx(
        REVERSED_POSE["shoulder_pan"], abs=1.0
    )
    assert f["reachable_range"]["from_deg"] == pytest.approx(-108.4 + 75.0, abs=1.5)  # lopsided, reported
    stored = FileFrontStore(tmp_path / "config").load(env.fp)
    assert stored is not None and stored.calibration_fingerprint == env.fp
    # reconnecting with the same calibration restores it; another calibration ignores it
    env.run(rq.Disconnect(confirmed=True))
    env.run(rq.EnterMode(Mode.SIM_TO_REAL, "/dev/fake-so101", str(calibration_file), confirmed=True))
    assert env.follow()["front"]["set"]
    env.c.front_store.save(env.fp, stored.__class__(10.0, "sha256:other", stored.model_id, "", "manual"))
    env.c._load_front()
    assert not env.follow()["front"]["set"]


def test_slider_reshaped_arm_gets_its_reference_remeasured(env: OverlayEnv) -> None:
    """Real-arm finding (2026-11-07): after Go Rest (REVERSED reference) the operator moved the arm to a
    NORMAL shape with the sliders; Follow Me kept the rest reference and faced the OPPOSITE way while
    claiming success. The angle is absolute: the reference must follow the arm's actual shape."""
    from overlay_helpers import analyzer

    rev = env.pose("Rev", REVERSED_POSE)
    env.service.move_to_pose(rev.id, TransitionProfile.FAST, confirmed=True)
    env.c.process_requests()
    env.run_while(Connection.MOVING_TO_POSE)
    env.run(rq.EnableMovement(confirmed=True))  # sliders command the arm
    assert env.snap().connection == Connection.ARMED
    follow_on(env, 100.0)
    settle(env)
    assert env.follow()["reference"]["classification"] == "REVERSED"
    normal_shape = dict(env.snap().requested.values) | {
        "shoulder_lift": -72.0,
        "elbow_flex": 48.0,
        "wrist_flex": -16.0,
    }
    for _ in range(5):  # the operator drags the sliders
        env.c.set_requested(normal_shape)
        env.tick()
    settle(env, 80)
    f = env.follow()
    assert f["reference"]["classification"] == "NORMAL" and env.events("follow_reference_remeasured")
    sent = env.sends()[-1]
    facing = wrap(analyzer().bearing_at_pan(sent) - f["front"]["bearing_deg"])
    assert f["reach"] == "REACHABLE" and abs(wrap(facing - 100.0)) < 1.0  # absolute: truly faces 100°
    # Follow OFF/ON after a slider move also measures the actual shape (never a stale owner)
    env.run(rq.SetFollow(False))
    settle(env)
    for _ in range(3):
        env.c.set_requested(
            dict(env.snap().requested.values)
            | {"elbow_flex": -51.0, "wrist_flex": -55.0, "shoulder_lift": -60.0}
        )
        env.tick()
    follow_on(env, 100.0)
    assert env.follow()["reference"]["classification"] == "REVERSED"


def wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0

"""ADR-018 reactive preemption, through the API command layer and the REAL controller loop (fake
hardware, fake clock — deterministic). Numbers refer to the Milestone 2.5 acceptance list.

The API layer submits ``rq.Tracked`` requests to the same controller queue as the Studio; the tests
step the controller by hand (``ack_timeout_s=0``: the POST-equivalent returns at once, ``queued``).
"""

from __future__ import annotations

import itertools
import threading
from pathlib import Path
from typing import Any

import pytest

from helpers import FAKE_PORT, HW_POSE, Rig, make_rig
from so101_companion.api.service import ApiCommands, InvalidRequest, NotFound, PlayOptions
from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.poses import PoseSource
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.service import BehaviorService
from so101_companion.control import requests as rq
from so101_companion.control.commands import CommandLedger, IdempotencyConflict
from so101_companion.devices.rest_pose import FileRestPoseStore
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Connection, Mode
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

PERIOD = 0.05
FAST = TransitionProfile.FAST


class Env:
    def __init__(self, calibration_file: Path, tmp_path: Path, *, physical: bool = True, **robot: Any):
        self.rig: Rig = make_rig(calibration_file, robot_kwargs=robot or None)
        self.c = self.rig.coordinator
        self.c.rest_store = FileRestPoseStore(tmp_path / "config")
        self.rig.run(rq.EnterMode(Mode.SIM_TO_REAL, FAKE_PORT, str(calibration_file), confirmed=True))
        self.rig.run(rq.RecordRestPose(confirmed=True))  # default rest pose = HW_POSE
        self.service = BehaviorService(self.c, BehaviorRepository(tmp_path / "behaviors"))
        self.api = ApiCommands(self.c, self.service, allow_physical_control=physical, ack_timeout_s=0.0)
        self.fp = self.rig.snap().extra["calibration_fingerprint"]
        self.publish()

    # -- stepping ---------------------------------------------------------------------------
    def publish(self) -> None:
        self.c.published.set(self.c.snapshot())

    def process(self) -> None:
        self.c.process_requests()
        self.publish()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.rig.tick()
            self.publish()

    def run_while(self, state: Connection, limit: int = 800) -> list[Connection]:
        seen = []
        for _ in range(limit):
            if self.rig.snap().connection != state:
                return seen
            self.tick()
            seen.append(self.rig.snap().connection)
        raise AssertionError(f"still {state}")

    def call(self, fn: Any, *args: Any) -> dict[str, Any]:
        rec, _ = fn(*args, None)
        self.process()
        return self.api.command(rec["id"])

    def rec(self, cid: str) -> dict[str, Any]:
        return self.api.command(cid)

    # -- library ----------------------------------------------------------------------------
    def behavior(self, name: str, joint: str, reps: int | None = 1, after: str = "rest", pose_id: str = ""):  # type: ignore[no-untyped-def]
        samples = tuple(
            Sample(round(i * PERIOD, 6), actual=HW_POSE | {joint: HW_POSE[joint] + 1.0 * i})
            for i in range(21)
        )
        b = self.service.repository.create(
            new_behavior(name, Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=self.fp)
        )
        b = self.service.repository.set_settings(
            b, PlaybackSettings(repeat_count=reps, after_action=after, after_pose_id=pose_id or None)
        )
        return b

    def pose(self, name: str = "Watching", **offsets: float):  # type: ignore[no-untyped-def]
        values = HW_POSE | {"shoulder_pan": HW_POSE["shoulder_pan"] + 30.0, "wrist_flex": 25.0}
        values |= {k: HW_POSE[k] + v for k, v in offsets.items()}
        return self.service.poses.create(name, values, PoseSource.REAL_ARM, calibration_fingerprint=self.fp)

    def sends(self) -> list[dict[str, float]]:
        return [a for n, a in self.rig.robot.calls if n == "send"]

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.c.events.records(exclude_ticks=True) if e["kind"] == kind]


def play_opts(**kw: Any) -> PlayOptions:
    return PlayOptions(getting_into_position=FAST, **kw)


def assert_continuous(path: list[dict[str, float]]) -> None:
    for a, b in itertools.pairwise(path):
        for j in JOINT_NAMES:
            assert abs(b[j] - a[j]) <= policy.TRANSITION_MAX_SPEED[j] * PERIOD + 1e-6, (j, a[j], b[j])


@pytest.fixture
def env(calibration_file: Path, tmp_path: Path) -> Env:
    return Env(calibration_file, tmp_path)


# ------------------------------------------------------------------ 1, 2
def test_1_api_plays_a_finite_behavior_to_completion(env: Env) -> None:
    b = env.behavior("talk", "elbow_flex", reps=2, after="hold")
    rec = env.call(env.api.play, b.id, play_opts())
    assert (
        rec["state"] == "running"
        and rec["source"] == "api"
        and env.rig.snap().connection == (Connection.PLAYING_BEHAVIOR)
    )
    env.run_while(Connection.PLAYING_BEHAVIOR)
    done = env.rec(rec["id"])
    assert done["state"] == "completed" and "holding its last position" in done["detail"]
    assert env.rig.snap().connection == Connection.HOLDING
    assert [h["state"] for h in done["history"]] == ["queued", "running", "completed"]
    assert done["latency_ms"]["dispatch"] is not None and done["latency_ms"]["first_target"] is not None


def test_2_api_plays_an_infinite_loop(env: Env) -> None:
    b = env.behavior("talk", "elbow_flex", reps=None)
    rec = env.call(env.api.play, b.id, play_opts())
    env.tick(200)  # 10 s of a 1 s gesture: it keeps looping
    s = env.rig.snap()
    assert s.connection == Connection.PLAYING_BEHAVIOR and s.extra["behavior"]["repetition"] >= 5
    assert env.rec(rec["id"])["state"] == "running" and s.extra["behavior"]["repeat_count"] is None


# ------------------------------------------------------------------ 3, 6
def test_3_6_named_pose_supersedes_an_infinite_loop_without_rest(env: Env) -> None:
    watching = env.pose()
    talk = env.call(env.api.play, env.behavior("talk", "elbow_flex", reps=None).id, play_opts())
    env.tick(30)
    k = len(env.sends()) - 1
    go = env.call(env.api.go_pose, watching.id, FAST)
    assert go["state"] == "running" and go["replaced"] == talk["id"]
    old = env.rec(talk["id"])
    assert old["state"] == "superseded" and old["superseded_by"] == go["id"]
    assert env.rig.snap().connection == Connection.MOVING_TO_POSE  # immediately: no wait for the loop
    seen = env.run_while(Connection.MOVING_TO_POSE)
    assert Connection.RETURNING_TO_REST not in seen and not env.events("stop_to_rest_started")  # 6
    assert_continuous(env.sends()[k:])  # never a jump
    assert all(abs(env.sends()[-1][j] - watching.pose[j]) < 1e-6 for j in JOINT_NAMES)
    assert env.rec(go["id"])["state"] == "completed" and env.rig.snap().connection == Connection.HOLDING


# ------------------------------------------------------------------ 4
def test_4_behavior_a_is_replaced_by_behavior_b(env: Env) -> None:
    a = env.call(env.api.play, env.behavior("celebrate", "elbow_flex", reps=None).id, play_opts())
    b_beh = env.behavior("talk", "wrist_flex", reps=1, after="hold")
    env.tick(17)
    k = len(env.sends()) - 1
    b = env.call(env.api.play, b_beh.id, play_opts())
    assert env.rec(a["id"])["state"] == "superseded" and env.rec(a["id"])["superseded_by"] == b["id"]
    s = env.rig.snap()
    assert s.connection == Connection.PLAYING_BEHAVIOR and s.extra["behavior"]["name"] == "talk"
    started = env.events("behavior_started")[-1]
    assert started["behavior_id"] == b_beh.id and started["starts_from"] == "last_target"
    seen = env.run_while(Connection.PLAYING_BEHAVIOR)
    assert Connection.RETURNING_TO_REST not in seen and not env.events("stop_to_rest_started")
    assert_continuous(env.sends()[k:])
    assert env.rec(b["id"])["state"] == "completed"


# ------------------------------------------------------------------ 5
def test_5_preemption_cancels_a_pending_after_behavior_move(env: Env) -> None:
    idle = env.pose("Idle")
    a = env.call(
        env.api.play, env.behavior("wave", "elbow_flex", 1, after="pose", pose_id=idle.id).id, play_opts()
    )
    env.run_while(Connection.PLAYING_BEHAVIOR)
    assert env.rig.snap().connection == Connection.MOVING_TO_POSE  # its ending move is in flight
    assert env.rec(a["id"])["state"] == "ending"
    b = env.call(env.api.play, env.behavior("talk", "wrist_flex", reps=None).id, play_opts())
    old = env.rec(a["id"])
    assert old["state"] == "superseded" and old["after_behavior_cancelled"] is True
    assert env.events("pose_move_superseded")[-1]["ending"] is True
    env.tick(60)
    assert env.rig.snap().connection == Connection.PLAYING_BEHAVIOR and env.rec(b["id"])["state"] == "running"
    assert not env.events("pose_move_completed")  # the cancelled ending never ran to its pose


def test_5_preemption_cancels_a_pending_finish(env: Env) -> None:
    idle = env.pose("Idle")
    a = env.call(
        env.api.play, env.behavior("loop", "elbow_flex", None, after="pose", pose_id=idle.id).id, play_opts()
    )
    env.tick(30)
    env.rig.run(rq.FinishBehavior())  # Studio: finish this loop, then the pose ending
    env.publish()
    assert env.rig.snap().extra["behavior"]["finish_after_repetition"] is not None
    env.call(env.api.go_rest)
    assert env.events("behavior_stopped")[-1]["finish_after_repetition"] is not None
    assert env.rec(a["id"])["state"] == "superseded" and not env.events("pose_move_started")
    env.run_while(Connection.RETURNING_TO_REST)
    assert env.rig.snap().connection == Connection.HOLDING


# ------------------------------------------------------------------ 7
def test_7_still_arm_starts_from_the_fresh_measured_pose(env: Env) -> None:
    env.rig.robot.move_by_hand(shoulder_pan=HW_POSE["shoulder_pan"] + 4.0)  # moved since the last read
    n_obs = sum(1 for c, _ in env.rig.robot.calls if c == "observe")
    n_sends = len(env.sends())
    env.call(env.api.play, env.behavior("talk", "elbow_flex").id, play_opts())
    assert sum(1 for c, _ in env.rig.robot.calls if c == "observe") > n_obs  # a fresh read first
    started = env.events("behavior_started")[-1]
    assert started["starts_from"] == "measured"
    assert started["start_pose"]["shoulder_pan"] == pytest.approx(HW_POSE["shoulder_pan"] + 4.0)
    env.tick()
    first = env.sends()[n_sends]["shoulder_pan"]  # one tick into the entry, from the measured start
    assert (
        abs(first - (HW_POSE["shoulder_pan"] + 4.0)) <= policy.TRANSITION_MAX_SPEED["shoulder_pan"] * PERIOD
    )


def test_7_moving_arm_reads_first_and_continues_without_a_jump(
    calibration_file: Path, tmp_path: Path
) -> None:
    env = Env(calibration_file, tmp_path, follow=0.35)  # servos lag behind their targets
    watching = env.pose()
    env.call(env.api.play, env.behavior("talk", "elbow_flex", reps=None).id, play_opts())
    env.tick(14)
    calls = len(env.rig.robot.calls)
    last_target = dict(env.sends()[-1])
    go = env.call(env.api.go_pose, watching.id, FAST)
    assert ("observe", None) in env.rig.robot.calls[calls:]  # fresh measurement before the takeover
    sup = env.events("motion_superseded")[-1]
    assert sup["by"] == go["id"] and sup["starts_from"] == "last_target"
    assert sup["start"] == pytest.approx({j: round(last_target[j], 3) for j in JOINT_NAMES})
    assert abs(sup["lag"]["elbow_flex"]) > 0.5  # the arm really lagged: restarting there would jump back
    env.run_while(Connection.MOVING_TO_POSE)
    k = next(i for i, s in enumerate(env.sends()) if s == last_target)
    assert_continuous(env.sends()[k:])


# ------------------------------------------------------------------ 8, 9
def test_8_natural_completion_performs_the_saved_ending(env: Env) -> None:
    idle = env.pose("Idle")
    b = env.behavior("wave", "elbow_flex", 1, after="pose", pose_id=idle.id)
    rec = env.call(env.api.play, b.id, play_opts())  # after_behavior = "saved"
    env.run_while(Connection.PLAYING_BEHAVIOR)
    env.run_while(Connection.MOVING_TO_POSE)
    done = env.rec(rec["id"])
    assert done["state"] == "completed" and "Idle" in done["detail"]
    assert [h["state"] for h in done["history"]] == ["queued", "running", "ending", "completed"]
    assert all(abs(env.sends()[-1][j] - idle.pose[j]) < 1e-6 for j in JOINT_NAMES)


def test_9_request_level_overrides_never_modify_the_behavior_file(env: Env) -> None:
    b = env.behavior("wave", "elbow_flex", 1, after="rest")
    path = env.service.repository.path_for(b.id)
    before = path.read_bytes()
    opts = play_opts(speed=2.0, repeat_count=3, after_behavior="hold")
    rec = env.call(env.api.play, b.id, opts)
    eff = rec["params"]["effective_settings"]
    assert eff["speed"] == 2.0 and eff["repeat_count"] == 3 and eff["after"]["action"] == "hold"
    assert env.rig.snap().extra["behavior"]["repeat_count"] == 3
    env.run_while(Connection.PLAYING_BEHAVIOR)
    assert env.rig.snap().connection == Connection.HOLDING and not env.events("stop_to_rest_started")
    assert path.read_bytes() == before and env.service.load(b.id).settings.after_action == "rest"
    idle = env.pose("Idle")
    rec2 = env.call(
        env.api.play, b.id, play_opts(after_behavior="pose", after_pose_id=idle.id, until_stopped=True)
    )
    assert rec2["params"]["effective_settings"]["repeat_count"] is None and path.read_bytes() == before
    with pytest.raises(InvalidRequest):
        env.api.play(b.id, play_opts(after_behavior="pose"), None)  # pose ending needs a pose id
    with pytest.raises(InvalidRequest):
        env.api.play(b.id, play_opts(after_behavior="pose", after_pose_id="nope"), None)
    with pytest.raises(NotFound):
        env.api.play("not-a-behavior", play_opts(), None)


# ------------------------------------------------------------------ 10
def test_10_pause_resume_and_go_rest_match_the_studio(env: Env) -> None:
    play = env.call(env.api.play, env.behavior("talk", "elbow_flex", reps=None).id, play_opts())
    env.tick(10)
    p = env.call(env.api.pause)
    assert p["state"] == "completed" and env.rec(play["id"])["state"] == "paused"
    assert env.rig.snap().extra["behavior"]["state"] == "paused" and env.events("behavior_paused")
    env.tick(10)
    r = env.call(env.api.resume)
    assert r["state"] == "completed" and env.rec(play["id"])["state"] == "running"
    assert env.events("behavior_resumed")
    env.tick(10)
    rest = env.call(env.api.go_rest)
    assert env.rec(play["id"])["state"] == "superseded" and rest["state"] == "running"
    assert env.events("behavior_stopped")[-1]["reason"] == "operator STOP"  # the Studio's Go Rest path
    env.run_while(Connection.RETURNING_TO_REST)
    assert env.rec(rest["id"])["state"] == "completed" and env.rig.snap().connection == Connection.HOLDING
    assert env.call(env.api.pause)["state"] == "rejected"  # nothing to pause


# ------------------------------------------------------------------ 11
def test_11_emergency_hold_overrides_and_latches_against_the_api(env: Env) -> None:
    play = env.call(env.api.play, env.behavior("talk", "elbow_flex", reps=None).id, play_opts())
    env.tick(10)
    env.rig.run(rq.EmergencyHold())  # the operator's button in the Studio
    env.publish()
    old = env.rec(play["id"])
    assert old["state"] == "cancelled" and env.rig.snap().connection == Connection.HOLDING
    assert old["cancelled_by"] is not None
    retry = env.call(env.api.play, env.behavior("again", "wrist_flex").id, play_opts())
    assert retry["state"] == "rejected" and "Emergency Hold is latched" in retry["detail"]
    assert env.call(env.api.go_rest)["state"] == "rejected"
    assert env.api.readiness(env.rig.snap())["play_behavior"]["ok"] is False
    env.rig.run(rq.StopToRest())  # a direct operator motion in the Studio releases the latch
    env.publish()
    assert env.rig.snap().extra["emergency_latched"] is None and env.events("emergency_latch_released")
    env.run_while(Connection.RETURNING_TO_REST)
    assert env.call(env.api.play, env.behavior("ok", "wrist_flex").id, play_opts())["state"] == "running"


def test_11_api_emergency_hold_cancels_and_faults_fail_commands(env: Env) -> None:
    play = env.call(env.api.play, env.behavior("talk", "elbow_flex", reps=None).id, play_opts())
    env.tick(5)
    hold = env.call(env.api.emergency_hold)
    assert hold["state"] == "completed" and env.rec(play["id"])["cancelled_by"] == hold["id"]
    env.rig.run(rq.StopToRest())  # operator releases (Studio)
    env.publish()
    env.run_while(Connection.RETURNING_TO_REST)
    play2 = env.call(env.api.play, env.behavior("talk2", "elbow_flex", reps=None).id, play_opts())
    env.rig.robot.faults.fail_write_after = 0  # the bus fails
    env.tick(2)
    assert env.rig.snap().connection == Connection.FAULT_LATCHED
    assert env.rec(play2["id"])["state"] == "failed"
    refused = env.call(env.api.go_pose, env.pose().id, FAST)
    assert refused["state"] == "rejected"  # a request never overrides a latched fault
    assert env.api.readiness(env.rig.snap())["go_pose"]["reason"].startswith("fault")


def test_11_torque_off_wins_and_the_api_never_enables_torque(env: Env) -> None:
    play = env.call(env.api.play, env.behavior("talk", "elbow_flex", reps=None).id, play_opts())
    env.tick(5)
    env.rig.run(rq.TorqueOff(confirmed=True))
    env.publish()
    assert env.rec(play["id"])["state"] == "cancelled" and env.rig.snap().connection == Connection.TORQUE_OFF
    again = env.call(env.api.play, env.behavior("x", "elbow_flex").id, play_opts())
    assert again["state"] == "rejected" and "torque is OFF" in again["detail"]
    assert not any(c == "enable_torque_holding_present" for c, _ in env.rig.robot.calls[-20:])
    # even a hand-built API request asking for torque is refused by the controller itself
    rec, _ = env.c.commands.create("play_behavior", "api", {})
    b = env.service.load(env.behavior("y", "elbow_flex").id)
    env.c.submit(rq.Tracked(rq.PlayBehavior(b, b.settings, None, True, True), rec["id"], "api", True))
    env.process()
    assert "never energizes" in env.rec(rec["id"])["detail"]


# ------------------------------------------------------------------ 12
def test_12_concurrent_requests_never_produce_concurrent_trajectories(env: Env) -> None:
    pose_a, pose_b = env.pose("A"), env.pose("B", elbow_flex=-15.0)
    beh = [env.behavior(f"b{i}", j, reps=None) for i, j in enumerate(["elbow_flex", "wrist_flex"])]
    calls = [
        lambda: env.api.play(beh[0].id, play_opts(), None),
        lambda: env.api.go_pose(pose_a.id, FAST, None),
        lambda: env.api.play(beh[1].id, play_opts(), None),
        lambda: env.api.go_rest(None),
        lambda: env.api.go_pose(pose_b.id, FAST, None),
    ] * 6
    ids: list[str] = []
    lock = threading.Lock()

    def worker(fn: Any) -> None:
        rec, _ = fn()
        with lock:
            ids.append(rec["id"])

    threads = [threading.Thread(target=worker, args=(fn,)) for fn in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in range(len(ids)):  # the controller interleaves ticks with the queued requests
        if i % 3:
            env.c.process_requests()
        env.tick()
    env.process()
    recs = [env.rec(i) for i in ids]
    assert len(recs) == 30 and all(r["state"] != "queued" for r in recs)
    assert sum(r["state"] in ("running", "paused", "ending") for r in recs) <= 1  # ONE authoritative motion
    n_sends, n_ticks = len(env.sends()), env.c.tick_seq
    env.tick(40)
    assert len(env.sends()) - n_sends <= env.c.tick_seq - n_ticks  # never more than one target per tick
    assert_continuous(env.sends()[1:])  # and no jumps through all the takeovers


# ------------------------------------------------------------------ 20 (+ the physical gate)
def _sequence(env: Env, via_api: bool) -> list[dict[str, float]]:
    watching = env.pose()
    loop = env.behavior("talk", "elbow_flex", reps=None)
    if via_api:
        env.call(env.api.play, loop.id, play_opts())
    else:
        env.service.play(loop, loop.settings, None, confirmed=True, enable_torque=False, entry_profile=FAST)
        env.process()
    env.tick(23)
    if via_api:
        env.call(env.api.go_pose, watching.id, FAST)
    else:
        env.service.move_to_pose(watching.id, FAST, confirmed=True)
        env.process()
    env.run_while(Connection.MOVING_TO_POSE)
    return env.sends()


def test_20_gui_and_api_commands_produce_identical_motion(calibration_file: Path, tmp_path: Path) -> None:
    studio = _sequence(Env(calibration_file, tmp_path / "studio"), via_api=False)
    api = _sequence(Env(calibration_file, tmp_path / "api"), via_api=True)
    assert len(studio) == len(api) > 30
    for s, a in zip(studio, api, strict=True):
        assert s == pytest.approx(a, abs=1e-9)


def test_physical_gate_is_enforced_by_the_controller(calibration_file: Path, tmp_path: Path) -> None:
    env = Env(calibration_file, tmp_path, physical=False)
    rec = env.call(env.api.play, env.behavior("talk", "elbow_flex").id, play_opts())
    assert rec["state"] == "rejected" and "allow_physical_control" in rec["detail"]
    assert env.api.readiness(env.rig.snap())["go_pose"]["ok"] is False
    # In Simulation Only the API moves the 3D arm only (no robot command).
    env.rig.run(rq.EnterMode(Mode.SIM_ONLY, confirmed=True))
    env.publish()
    n = len(env.sends())
    sim = env.call(env.api.go_pose, env.pose().id, FAST)
    assert sim["state"] == "running", sim["detail"]
    env.tick(40)
    assert env.rec(sim["id"])["state"] == "completed" and len(env.sends()) == n
    # Race: the HTTP layer saw Simulation Only, but the operator's mode change is handled first. The
    # controller re-checks the gate at handling time, so the command cannot move the real arm.
    env.c.submit(rq.EnterMode(Mode.SIM_TO_REAL, FAKE_PORT, str(calibration_file), confirmed=True))
    rec2, _ = env.api.go_rest(None)  # published snapshot still says SIM_ONLY
    env.process()
    assert env.rig.snap().mode == Mode.SIM_TO_REAL
    assert (
        env.rec(rec2["id"])["state"] == "rejected"
        and "allow_physical_control" in env.rec(rec2["id"])["detail"]
    )
    assert len(env.sends()) == n


def test_ledger_idempotency_and_ordering() -> None:
    ledger = CommandLedger()
    a, replay = ledger.create("go_rest", "api", {}, idempotency_key="k1")
    assert not replay
    again, replay = ledger.create("go_rest", "api", {}, idempotency_key="k1")
    assert replay and again["id"] == a["id"]
    with pytest.raises(IdempotencyConflict):
        ledger.create("go_pose", "api", {"pose_id": "x"}, idempotency_key="k1")
    b, _ = ledger.create("go_pose", "api", {"pose_id": "x"})
    ledger.activate(a["id"])
    ledger.activate(b["id"])
    assert ledger.get(a["id"])["state"] == "superseded" and ledger.get(b["id"])["replaced"] == a["id"]
    assert [r["id"] for r in ledger.list(2)] == [b["id"], a["id"]]

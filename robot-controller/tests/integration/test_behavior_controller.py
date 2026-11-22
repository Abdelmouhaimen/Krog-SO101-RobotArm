"""M2 Behavior Engine through the M1 controller, on the fake robot + fake clock (ADR-013, ADR-014).

Acceptance coverage: M2-A01, A02, A04, A14, A15, A16, A17, A18, A19, A20, A22, A24 (fixed policy) and
the owner-requested M1 changes D4 (stall-aware rest) and D5 (rest-pose calibration fingerprint).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from helpers import HW_POSE, Rig, make_rig
from so101_companion.behaviors.model import Behavior, Origin, PlaybackSettings, Sample
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.service import BehaviorService
from so101_companion.control import requests as rq
from so101_companion.devices.calibration import calibration_fingerprint, load_calibration
from so101_companion.devices.rest_pose import FileRestPoseStore, RestPose
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Action, Connection, Mode, Torque
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile

PERIOD = 0.05


def sends(rig: Rig) -> list[dict[str, float]]:
    return [args for name, args in rig.robot.calls if name == "send"]


def connected(calibration_file: Path, tmp_path: Path, **robot_kwargs: Any) -> tuple[Rig, BehaviorService]:
    rig = make_rig(calibration_file, robot_kwargs=robot_kwargs)
    rig.coordinator.rest_store = FileRestPoseStore(tmp_path / "config")
    rig.connect()
    rig.run(rq.RecordRestPose(confirmed=True))  # rest = HW_POSE (fake arm starts there)
    service = BehaviorService(rig.coordinator, BehaviorRepository(tmp_path / "behaviors"))
    return rig, service


def gesture(fp: str, amplitude: float = 8.0, seconds: float = 1.0) -> Behavior:
    """A small hand-guided-style gesture around HW_POSE (illustrative test data, not a real pose)."""
    n = round(seconds / PERIOD) + 1
    samples = []
    for i in range(n):
        t = round(i * PERIOD, 6)
        p = dict(HW_POSE)
        p["elbow_flex"] += amplitude * (t / seconds)
        p["gripper"] += 20.0 * (t / seconds)
        samples.append(Sample(t, actual=p))
    return new_behavior("wave", Origin.REAL_HAND_GUIDED, tuple(samples), calibration_fingerprint=fp)


def approve(rig: Rig, service: BehaviorService, b: Behavior, settings: PlaybackSettings) -> Any:
    rig.tick()  # fresh measured pose
    prepared = service.prepare_preview(b, settings, rig.snap())
    assert prepared.provisional_reason is None, prepared.provisional_reason
    prepared.session.step(prepared.session.duration + 0.01)
    approval = prepared.session.approval()
    assert approval is not None
    return approval


def ready_to_play(calibration_file: Path, tmp_path: Path, **kw: Any) -> tuple[Rig, BehaviorService, Behavior]:
    rig, service = connected(calibration_file, tmp_path, **kw)
    rig.run(rq.SetMode(Mode.SIM_TO_REAL))
    fp = rig.snap().extra["calibration_fingerprint"]
    return rig, service, service.repository.create(gesture(fp))


def play(rig: Rig, service: BehaviorService, b: Behavior, settings: PlaybackSettings, **kw: Any) -> Any:
    approval = approve(rig, service, b, settings)
    rig.run(
        rq.PlayBehavior(b, settings, approval, confirmed=True, enable_torque=kw.get("enable_torque", False))
    )
    return approval


def run_until_not(rig: Rig, state: Connection, limit: int = 2000) -> int:
    for i in range(limit):
        if rig.snap().connection != state:
            return i
        rig.tick()
    raise AssertionError(f"still {state} after {limit} ticks")


# ------------------------------------------------------------------ recording (A01, A02, A04)
def test_hand_guided_recording_reads_only_and_stores_measured_positions(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service = connected(calibration_file, tmp_path)
    rig.run(rq.SetMode(Mode.REAL_TO_SIM))
    assert Action.START_REAL_RECORDING not in rig.snap().available_actions  # torque still ON
    rig.run(rq.StartRecording(Origin.REAL_HAND_GUIDED))
    assert not rig.coordinator.recorder.active and "TORQUE_OFF" in rig.snap().message
    rig.run(rq.TorqueOff(confirmed=True))
    assert Action.START_REAL_RECORDING in rig.snap().available_actions
    calls_before = len(rig.robot.calls)
    rig.run(rq.StartRecording(Origin.REAL_HAND_GUIDED))
    for i in range(30):
        rig.robot.move_by_hand(elbow_flex=HW_POSE["elbow_flex"] + i * 0.5, gripper=float(i))
        rig.tick()
    assert rig.snap().extra["recording"]["state"] == "recording"
    rig.run(rq.StopRecording())
    names = {name for name, _ in rig.robot.calls[calls_before:]}
    assert names == {"observe"}  # never send(), never a torque write
    assert rig.snap().torque == Torque.OFF  # recording did not change torque
    rec = service.take_recording()
    assert rec is not None and rec.valid and rec.report.sample_count == 30
    assert [s.t for s in rec.samples][:3] == pytest.approx([0.0, 0.05, 0.10])
    assert rec.samples[-1].actual["elbow_flex"] == pytest.approx(HW_POSE["elbow_flex"] + 14.5)
    assert all(s.requested is None and s.sent is None for s in rec.samples)
    b = service.save_recording(rec, "wave", "hand-guided greeting")
    fp = calibration_fingerprint(load_calibration(calibration_file))
    assert b.calibration_fingerprint == fp and b.calibration_robot_id == "test_follower"
    assert service.load(b.id) == b


@pytest.mark.parametrize("torque_state", ["on", "unknown"])
def test_recording_refused_unless_torque_confirmed_off(
    calibration_file: Path, tmp_path: Path, torque_state: str
) -> None:
    rig, _ = connected(calibration_file, tmp_path)
    rig.run(rq.SetMode(Mode.REAL_TO_SIM), rq.TorqueOff(confirmed=True))
    if torque_state == "on":
        rig.run(rq.EnableTorque(confirmed=True))
    else:
        rig.coordinator.torque = Torque.UNKNOWN  # e.g. after a recovered fault
    assert Action.START_REAL_RECORDING not in rig.snap().available_actions
    rig.run(rq.StartRecording(Origin.REAL_HAND_GUIDED))
    assert not rig.coordinator.recorder.active
    assert rig.coordinator.events.last("request_rejected") is not None


def test_recording_with_a_bus_stall_is_rejected_with_location(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = connected(calibration_file, tmp_path)
    rig.run(
        rq.SetMode(Mode.REAL_TO_SIM), rq.TorqueOff(confirmed=True), rq.StartRecording(Origin.REAL_HAND_GUIDED)
    )
    rig.tick(10)
    rig.robot.faults.read_latency_s = 0.30  # one 300 ms read stall
    rig.tick()
    rig.robot.faults.read_latency_s = 0.0
    rig.tick(10)
    rig.run(rq.StopRecording())
    rec = service.take_recording()
    assert rec is not None and not rec.valid
    assert "350 ms gap" in rec.problems()[0]  # 50 ms tick + 300 ms stall
    assert rig.snap().extra["recording"]["valid"] is False
    with pytest.raises(ValueError, match="not playable"):
        service.save_recording(rec, "bad")
    assert service.list_behaviors()[0] == []


def test_recording_aborts_when_preconditions_break(calibration_file: Path, tmp_path: Path) -> None:
    rig, service = connected(calibration_file, tmp_path)
    rig.run(
        rq.SetMode(Mode.REAL_TO_SIM), rq.TorqueOff(confirmed=True), rq.StartRecording(Origin.REAL_HAND_GUIDED)
    )
    rig.tick(5)
    rig.run(rq.EnableTorque(confirmed=True))  # motors energized -> recorder must stop
    rec = service.take_recording()
    assert rec is not None and not rec.valid and "preconditions changed" in (rec.aborted or "")
    assert rig.coordinator.events.last("recording_aborted") is not None


def test_sim_slider_recording_needs_no_hardware(calibration_file: Path, tmp_path: Path) -> None:
    """A05: SIM_ONLY authoring records authored targets with real tick timestamps; no robot object."""
    rig = make_rig(calibration_file)
    service = BehaviorService(rig.coordinator, BehaviorRepository(tmp_path))
    rig.run(rq.StartRecording(Origin.SIM_SLIDERS))
    for i in range(20):
        rig.request(elbow_flex=float(i))
        rig.tick()
    rig.run(rq.StopRecording())
    rec = service.take_recording()
    assert rig.robots == [] and rec is not None and rec.valid
    assert rec.samples[-1].requested["elbow_flex"] == 19.0 and rec.samples[-1].actual is None
    b = service.save_recording(rec, "sim wave")
    assert b.origin is Origin.SIM_SLIDERS and b.calibration_fingerprint is None
    rig.run(rq.StartRecording(Origin.SIM_SLIDERS), rq.Pause())  # pausing the sim aborts the take
    assert service.take_recording().aborted  # type: ignore[union-attr]


# ------------------------------------------------------------------ playback gates (A14, A15, A16)
def test_preview_is_optional_but_a_supplied_preview_must_match_and_is_single_use(
    calibration_file: Path, tmp_path: Path
) -> None:
    """Owner decision D7 (Gate B 2026-11-07): no preview needed; explicit confirmation still is."""
    rig, service, b = ready_to_play(calibration_file, tmp_path)
    s = PlaybackSettings()
    approval = approve(rig, service, b, s)
    rig.run(rq.PlayBehavior(b, s, approval, confirmed=False))
    assert rig.snap().connection != Connection.PLAYING_BEHAVIOR  # explicit confirmation required
    rig.run(rq.PlayBehavior(b, PlaybackSettings(speed=1.5), approval, confirmed=True))
    assert "settings changed" in rig.snap().message
    sends_before = len(sends(rig))
    rig.run(rq.PlayBehavior(b, s, approval, confirmed=True))
    assert rig.snap().connection == Connection.PLAYING_BEHAVIOR
    run_until_not(rig, Connection.PLAYING_BEHAVIOR)
    run_until_not(rig, Connection.RETURNING_TO_REST)
    assert len(sends(rig)) > sends_before
    rig.run(rq.PlayBehavior(b, s, approval, confirmed=True))  # same preview again
    assert "already used" in rig.snap().message and rig.snap().connection == Connection.HOLDING


def test_play_without_preview_starts_from_the_fresh_measured_pose(
    calibration_file: Path, tmp_path: Path
) -> None:
    """D7: without a preview, the run still needs confirmation, re-reads the arm, enters smoothly from
    the measured pose (no jump) and returns to rest. Every other gate is unchanged."""
    rig, _service, b = ready_to_play(calibration_file, tmp_path)
    s = PlaybackSettings()
    rig.run(rq.PlayBehavior(b, s, None, confirmed=False))
    assert rig.snap().connection != Connection.PLAYING_BEHAVIOR  # explicit confirmation required
    rig.robot.move_by_hand(elbow_flex=HW_POSE["elbow_flex"] + 3.5)  # no preview pose to compare with
    n = len(rig.robot.calls)
    rig.run(rq.PlayBehavior(b, s, None, confirmed=True))
    assert rig.robot.calls[n][0] == "observe"  # fresh read before anything is commanded
    assert rig.snap().connection == Connection.PLAYING_BEHAVIOR
    started = rig.coordinator.events.last("behavior_started")
    assert started is not None and started["previewed"] is False and started["preview_approval"] is None
    rig.tick()
    first = sends(rig)[-1]
    # entry starts AT the measured pose: the first target is one eased step from it, never a jump
    one_step = policy.TRANSITION_MAX_SPEED["elbow_flex"] * PERIOD
    assert abs(first["elbow_flex"] - (HW_POSE["elbow_flex"] + 3.5)) <= one_step
    assert first["elbow_flex"] < HW_POSE["elbow_flex"] + 3.5
    run_until_not(rig, Connection.PLAYING_BEHAVIOR)
    assert rig.snap().connection == Connection.RETURNING_TO_REST


def test_disconnected_preview_never_authorizes(calibration_file: Path, tmp_path: Path) -> None:
    rig = make_rig(calibration_file)
    service = BehaviorService(rig.coordinator, BehaviorRepository(tmp_path))
    b = gesture("sha256:" + "0" * 64)
    prepared = service.prepare_preview(b, PlaybackSettings(), rig.snap())
    assert "not connected" in (prepared.provisional_reason or "")
    prepared.session.step(prepared.session.duration + 1)
    assert prepared.session.approval() is None


def test_preview_invalidated_by_arm_motion_or_calibration_change(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service, b = ready_to_play(calibration_file, tmp_path)
    s = PlaybackSettings()
    approval = approve(rig, service, b, s)
    rig.robot.move_by_hand(elbow_flex=HW_POSE["elbow_flex"] + 3.5)  # someone pushed the arm
    rig.run(rq.PlayBehavior(b, s, approval, confirmed=True))
    assert "moved since the preview" in rig.snap().message and "elbow_flex +3.5" in rig.snap().message
    assert rig.snap().connection != Connection.PLAYING_BEHAVIOR
    approval = approve(rig, service, b, s)
    rig.coordinator.calibration_fp = "sha256:recalibrated"
    rig.run(rq.PlayBehavior(b, s, approval, confirmed=True))
    assert "calibration" in rig.snap().message and rig.snap().connection != Connection.PLAYING_BEHAVIOR


def test_play_refused_without_valid_rest_pose_or_calibration(calibration_file: Path, tmp_path: Path) -> None:
    rig = make_rig(calibration_file)
    rig.connect()
    rig.run(rq.SetMode(Mode.SIM_TO_REAL))
    assert Action.PLAY_BEHAVIOR not in rig.snap().available_actions  # no rest pose recorded
    rig.run(rq.RecordRestPose(confirmed=True))
    assert Action.PLAY_BEHAVIOR in rig.snap().available_actions
    rig.coordinator.calibration_valid = False
    assert Action.PLAY_BEHAVIOR not in rig.snap().available_actions


def test_combined_confirmation_enables_torque_from_fresh_pose_first(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service, b = ready_to_play(calibration_file, tmp_path)
    rig.run(rq.TorqueOff(confirmed=True))
    rig.robot.move_by_hand(shoulder_pan=HW_POSE["shoulder_pan"] + 1.0)  # small sag, inside 3°
    s = PlaybackSettings()
    approval = approve(rig, service, b, s)
    rig.run(rq.PlayBehavior(b, s, approval, confirmed=True, enable_torque=False))
    assert "torque is OFF" in rig.snap().message
    approval = approve(rig, service, b, s)
    n = len(rig.robot.calls)
    rig.run(rq.PlayBehavior(b, s, approval, confirmed=True, enable_torque=True))
    names = [c for c, _ in rig.robot.calls[n:]]
    assert names[:2] == ["observe", "enable_torque_holding_present"]  # goal := present, then torque
    assert "send" not in names
    assert rig.snap().connection == Connection.PLAYING_BEHAVIOR and rig.snap().torque == Torque.ON
    rig.tick()
    first = sends(rig)[-1]
    # within the 1° seamless tolerance there is no entry move: the step is at most that tolerance
    seam = policy.LOOP_SEAMLESS_TOL_ARM_DEG + 1e-9
    assert abs(first["shoulder_pan"] - (HW_POSE["shoulder_pan"] + 1.0)) <= seam
    started = rig.coordinator.events.last("behavior_started")
    assert started is not None and started["torque_enabled_by_confirmation"] is True
    # Gate B finding: the log must show which completed preview authorized the run (audit of D3).
    evidence = started["preview_approval"]
    assert evidence["nonce"] == approval.nonce
    assert evidence["previewed_start"] == pytest.approx(dict(approval.start_pose), abs=1e-3)
    assert all(abs(v) <= 3.0 for v in evidence["start_offset"].values())


# ------------------------------------------------------------------ execution (A12, A17, A18)
def test_entry_from_measured_pose_then_linear_content_then_rest(
    calibration_file: Path, tmp_path: Path
) -> None:
    rig, service, b = ready_to_play(calibration_file, tmp_path)
    play(rig, service, b, PlaybackSettings())
    run_until_not(rig, Connection.PLAYING_BEHAVIOR)
    targets = sends(rig)
    # already at the first pose -> no entry move; the first target is the gesture's own first step
    assert targets[0]["elbow_flex"] == pytest.approx(HW_POSE["elbow_flex"] + 8.0 * PERIOD, abs=0.05)
    peak = b.samples[-1].actual["elbow_flex"]
    assert max(t["elbow_flex"] for t in targets) == pytest.approx(peak, abs=1e-6)
    assert rig.snap().connection == Connection.RETURNING_TO_REST
    assert rig.coordinator.events.last("behavior_completed") is not None
    run_until_not(rig, Connection.RETURNING_TO_REST)
    s = rig.snap()
    assert s.connection == Connection.HOLDING and s.torque == Torque.ON
    assert rig.coordinator.events.last("rest_reached") is not None
    assert "completed" in s.extra["behavior"]["last_result"]


def test_serial_stall_delays_playback_without_skipping_or_bursting(
    calibration_file: Path, tmp_path: Path
) -> None:
    """A17 / D1: identical target sequence with and without a 350 ms stall; one send per tick."""

    def trace(stall_at: int | None) -> tuple[list[dict[str, float]], float]:
        rig, service, b = ready_to_play(calibration_file, tmp_path / str(stall_at))
        play(rig, service, b, PlaybackSettings(repeat_count=2))
        start = len(sends(rig))
        for i in itertools.count():
            if rig.snap().connection != Connection.PLAYING_BEHAVIOR:
                break
            rig.robot.faults.write_latency_s = 0.35 if i == stall_at else 0.0
            n = len(sends(rig))
            rig.tick()
            assert len(sends(rig)) - n <= 1  # never a burst
        ev = rig.coordinator.events.last("behavior_completed")
        return sends(rig)[start:], ev["timeline_delay_ms"]

    smooth, delay0 = trace(None)
    stalled, delay1 = trace(12)
    assert len(stalled) == len(smooth)  # no target skipped, none repeated
    for a, b in zip(stalled, smooth, strict=True):
        assert a == pytest.approx(b, abs=1e-9)
    assert delay0 == 0.0 and delay1 == pytest.approx(350.0, abs=1.0)


def test_stop_behavior_returns_to_rest(calibration_file: Path, tmp_path: Path) -> None:
    rig, service, b = ready_to_play(calibration_file, tmp_path)
    play(rig, service, b, PlaybackSettings(repeat_count=None))
    rig.tick(40)
    snap = rig.snap()
    assert Action.MOVE_SLIDERS not in snap.available_actions  # A22: sliders cannot fight the player
    assert snap.extra["behavior"]["state"] == "playing"
    rig.request(elbow_flex=-60.0)
    rig.tick()
    assert all(t["elbow_flex"] > -10 for t in sends(rig))  # slider intent ignored
    rig.run(rq.StopBehavior())
    assert rig.snap().connection == Connection.RETURNING_TO_REST
    run_until_not(rig, Connection.RETURNING_TO_REST)
    assert rig.snap().connection == Connection.HOLDING
    assert "stopped" in rig.snap().extra["behavior"]["last_result"]


def test_rest_failure_is_reported_not_hidden(calibration_file: Path, tmp_path: Path) -> None:
    rig, service, b = ready_to_play(calibration_file, tmp_path)
    play(rig, service, b, PlaybackSettings())
    run_until_not(rig, Connection.PLAYING_BEHAVIOR)
    rig.robot.follow = 0.0  # elbow stuck away from rest (e.g. load)
    run_until_not(rig, Connection.RETURNING_TO_REST)
    s = rig.snap()
    assert s.connection == Connection.HOLDING and "REST NOT REACHED" in s.message
    assert rig.coordinator.events.last("rest_not_reached") is not None


@pytest.mark.parametrize("override", ["hold", "torque_off", "fault", "mode", "disconnect"])
def test_overrides_cancel_playback_without_rest(
    calibration_file: Path, tmp_path: Path, override: str
) -> None:
    """A19/A20: Emergency Hold, Torque Off, comm fault, mode change, disconnect never start rest."""
    rig, service, b = ready_to_play(calibration_file, tmp_path)
    play(rig, service, b, PlaybackSettings(repeat_count=None))
    rig.tick(10)
    if override == "hold":
        rig.run(rq.EmergencyHold())
        expected = Connection.HOLDING
    elif override == "torque_off":
        rig.run(rq.TorqueOff(confirmed=True))
        expected = Connection.TORQUE_OFF
    elif override == "fault":
        rig.robot.faults.unplugged = True
        rig.tick()
        expected = Connection.FAULT_LATCHED
    elif override == "mode":
        rig.run(rq.SetMode(Mode.REAL_TO_SIM))
        expected = Connection.CONNECTED_READ_ONLY
    else:
        rig.run(rq.Disconnect(confirmed=True))
        expected = Connection.DISCONNECTED
    snap = rig.snap()
    assert snap.connection == expected
    assert rig.coordinator.events.last("stop_to_rest_started") is None
    assert rig.coordinator.events.last("behavior_cancelled") is not None
    assert snap.extra["behavior"]["state"] == "idle" and "CANCELLED" in snap.extra["behavior"]["last_result"]
    n = len(sends(rig)) if rig.robots else 0
    rig.tick(20)
    assert (len(sends(rig)) if rig.robots else 0) == n  # nothing resumes by itself
    if override == "fault":
        assert snap.torque == Torque.UNKNOWN  # outcome not invented


def test_fixed_m1_policy_unchanged_during_playback(calibration_file: Path, tmp_path: Path) -> None:
    """A24: no step limiter / relative cap — a fast recorded flick is forwarded as recorded."""
    assert policy.MAX_RELATIVE_TARGET is None and policy.ANGULAR_MARGIN_DEG == 5.0
    rig, service = connected(calibration_file, tmp_path)
    rig.run(rq.SetMode(Mode.SIM_TO_REAL))
    fp = rig.snap().extra["calibration_fingerprint"]
    flick = [
        Sample(
            round(i * PERIOD, 6),
            actual={**HW_POSE, "elbow_flex": HW_POSE["elbow_flex"] + (25.0 if i >= 2 else 0.0)},
        )
        for i in range(5)
    ]
    b = service.repository.create(
        new_behavior("flick", Origin.REAL_HAND_GUIDED, tuple(flick), calibration_fingerprint=fp)
    )
    play(rig, service, b, PlaybackSettings())
    run_until_not(rig, Connection.PLAYING_BEHAVIOR)
    steps = [abs(b2["elbow_flex"] - a["elbow_flex"]) for a, b2 in itertools.pairwise(sends(rig))]
    assert max(steps) > 20.0  # the recorded 25° step in one 50 ms sample is NOT limited (ADR-010)


# ------------------------------------------------------------------ D4: stall-aware return to rest
def test_rest_return_does_not_jump_after_a_stall(calibration_file: Path, tmp_path: Path) -> None:
    rig, _ = connected(calibration_file, tmp_path, follow=1.0)
    rig.arm()
    rig.request(shoulder_pan=HW_POSE["shoulder_pan"] + 60.0)
    rig.tick()
    rig.run(rq.StopToRest())
    start = len(sends(rig))
    for i in itertools.count():
        if rig.snap().connection != Connection.RETURNING_TO_REST:
            break
        rig.robot.faults.write_latency_s = 0.35 if i in (5, 6, 20) else 0.0
        rig.tick()
    targets = sends(rig)[start:]
    steps = [abs(b[n] - a[n]) for a, b in itertools.pairwise(targets) for n in JOINT_NAMES]
    assert max(steps) <= policy.REST_RETURN_SPEED * PERIOD + 1e-6  # 1.5°: never a 10.5° jump
    assert rig.snap().connection == Connection.HOLDING
    ev = rig.coordinator.events.last("rest_reached")
    assert ev is not None and ev["timeline_delay_ms"] == pytest.approx(3 * 350.0, abs=1.0)


# ------------------------------------------------------------------ D5: rest pose fingerprint
def _legacy_file(tmp_path: Path, pose: dict[str, float]) -> Path:
    store = FileRestPoseStore(tmp_path / "config")
    legacy = RestPose("test_follower", pose, pose, (), "2026-11-07T01:49:00+0100")  # no fingerprint
    store.save(legacy)
    data = json.loads(store.path_for("test_follower").read_text())
    data.pop("calibration_fingerprint")
    data["note"] = "hand-shifted after wrist re-centring"
    store.path_for("test_follower").write_text(json.dumps(data))
    return store.path_for("test_follower")


def test_legacy_rest_pose_needs_explicit_adoption_with_backup(calibration_file: Path, tmp_path: Path) -> None:
    path = _legacy_file(tmp_path, dict(HW_POSE))
    original = path.read_text()
    rig = make_rig(calibration_file)
    rig.coordinator.rest_store = FileRestPoseStore(tmp_path / "config")
    rig.connect()
    snap = rig.snap()
    assert rig.coordinator.rest_pose is None and snap.extra["rest_status"]["state"] == "needs_adoption"
    assert path.read_text() == original  # nothing overwritten automatically
    rig.run(rq.SetMode(Mode.SIM_TO_REAL))
    assert (  # the PHYSICAL Normal Stop and Play need the adopted pose (checked in SIM_TO_REAL)
        Action.STOP_TO_REST not in rig.snap().available_actions
        and Action.PLAY_BEHAVIOR not in rig.snap().available_actions
    )
    rig.run(rq.AdoptRestPose(confirmed=False))
    assert rig.coordinator.rest_pose is None
    rig.run(rq.AdoptRestPose(confirmed=True))
    rest = rig.coordinator.rest_pose
    fp = calibration_fingerprint(load_calibration(calibration_file))
    assert rest is not None and rest.calibration_fingerprint == fp and rest.pose == HW_POSE
    assert rest.provenance["note"] == "hand-shifted after wrist re-centring"
    backups = list((tmp_path / "config").glob("rest_pose.test_follower.bak-*.json"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert rig.coordinator.events.last("rest_pose_adopted")["backup"] == str(backups[0])


def test_rest_pose_from_another_calibration_is_rejected(calibration_file: Path, tmp_path: Path) -> None:
    store = FileRestPoseStore(tmp_path / "config")
    store.save(RestPose("test_follower", dict(HW_POSE), dict(HW_POSE), (), "x", "sha256:old-calibration"))
    rig = make_rig(calibration_file)
    rig.coordinator.rest_store = store
    rig.connect()
    assert rig.coordinator.rest_pose is None
    assert rig.snap().extra["rest_status"]["state"] == "mismatch"
    assert Action.ADOPT_REST_POSE not in rig.snap().available_actions  # only re-recording helps
    rig.run(rq.RecordRestPose(confirmed=True))
    assert rig.coordinator.rest_pose is not None and rig.snap().extra["rest_status"]["state"] == "loaded"
    assert list((tmp_path / "config").glob("rest_pose.test_follower.bak-*.json"))  # old one kept


# ------------------------------------------------------------------ D8 responsive transitions
def test_fast_entry_and_loops_in_the_controller_stay_within_per_joint_limits_even_with_a_stall(
    calibration_file: Path, tmp_path: Path
) -> None:
    """D8 in the real 20 Hz controller loop with the fake arm: a large entry move, 4 repetitions with
    bridges and a 350 ms serial stall. No sent step may exceed what the joint's transition speed allows
    in one control period; the run then hands over to the UNCHANGED 30 °/s return to rest."""
    rig, _service, b = ready_to_play(calibration_file, tmp_path)
    far = {"shoulder_pan": HW_POSE["shoulder_pan"] + 60.0, "elbow_flex": HW_POSE["elbow_flex"] - 40.0}
    rig.robot.move_by_hand(**far)
    rig.tick()
    n0 = len(sends(rig))
    fast = TransitionProfile.FAST
    rig.run(rq.PlayBehavior(b, PlaybackSettings(repeat_count=4), None, confirmed=True, entry_profile=fast))
    assert rig.snap().connection == Connection.PLAYING_BEHAVIOR
    started = rig.coordinator.events.last("behavior_started")
    assert started is not None and 0.5 < started["entry_s"] < 1.5  # was ~3.8 s at the old 30 °/s cap
    for i in range(400):
        if rig.snap().connection != Connection.PLAYING_BEHAVIOR:
            break
        rig.robot.faults.read_latency_s = 0.35 if i == 5 else 0.0  # one stall during the entry
        rig.tick()
    rig.robot.faults.read_latency_s = 0.0
    assert rig.snap().connection == Connection.RETURNING_TO_REST
    played = sends(rig)[n0:]
    assert played[0]["shoulder_pan"] < far["shoulder_pan"]  # eased out of the measured pose
    for a, c in itertools.pairwise(played):
        for j in JOINT_NAMES:
            assert abs(c[j] - a[j]) <= policy.TRANSITION_MAX_SPEED[j] * PERIOD + 1e-6, j
    done = rig.coordinator.events.last("behavior_completed")
    assert done is not None and done["repetition"] == 4 and done["timeline_delay_ms"] > 0
    rest_targets = [sends(rig)[-1]]
    while rig.snap().connection == Connection.RETURNING_TO_REST:
        n = len(sends(rig))
        rig.tick()
        if rig.snap().connection == Connection.RETURNING_TO_REST:
            rest_targets += sends(rig)[n:]
    for a, c in itertools.pairwise(rest_targets):  # the return to rest is still the M1 30 °/s
        for j in JOINT_NAMES:
            assert abs(c[j] - a[j]) <= policy.REST_RETURN_SPEED * PERIOD + 1e-6, j
    assert len(rest_targets) > 3


def test_behavior_hands_over_to_rest_from_the_last_target_without_braking(
    calibration_file: Path, tmp_path: Path
) -> None:
    """D9 (Gate B, 2026-11-07): a fast gesture left the real arm ~25° behind its target; the return to
    rest then started from the MEASURED pose, so the target snapped back 24° and the arm braked hard.
    Now a behavior's hand-over (normal Stop or completion) starts the unchanged 30 °/s line at the last
    target sent: no target step larger than the rest speed allows. A plain M1 STOP still starts from
    the measured pose."""
    rig, _service, _b = ready_to_play(calibration_file, tmp_path)
    fp = rig.snap().extra["calibration_fingerprint"]
    fast = _service.repository.create(gesture(fp, amplitude=40.0, seconds=0.5))  # 80 °/s elbow
    rig.robot.follow = 0.2  # the arm lags its targets (load, 5 V supply)
    rig.run(rq.PlayBehavior(fast, PlaybackSettings(speed=2.0, repeat_count=None), None, confirmed=True))
    rig.tick(12)
    last = sends(rig)[-1]
    lag = abs(last["elbow_flex"] - rig.robot.observe().as_dict()["elbow_flex"])
    assert lag > 5.0  # the situation seen on the real arm
    rig.run(rq.StopBehavior())
    started = rig.coordinator.events.last("stop_to_rest_started")
    assert started is not None and started["starts_from"] == "last_target"
    assert abs(started["lag_at_start"]["elbow_flex"]) > 5.0
    rig.tick()
    first_rest = sends(rig)[-1]
    for j in JOINT_NAMES:  # continues from the last target: no snap back towards the measured pose
        assert abs(first_rest[j] - last[j]) <= policy.REST_RETURN_SPEED * PERIOD + 1e-6, j
    rig.robot.follow = 1.0
    run_until_not(rig, Connection.RETURNING_TO_REST)
    assert rig.snap().connection == Connection.HOLDING
    assert rig.coordinator.events.last("rest_reached") is not None
    # a plain M1 STOP (no behavior) keeps starting from the measured pose
    rig.run(rq.EnableMovement(confirmed=True))
    rig.run(rq.StopToRest())
    plain = rig.coordinator.events.last("stop_to_rest_started")
    assert plain is not None and plain["starts_from"] == "measured"

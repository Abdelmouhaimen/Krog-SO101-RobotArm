"""SIM_ONLY controller behaviour (A01, A04, A06, offline tests 5 & 7)."""

from __future__ import annotations

from helpers import SIM_INITIAL, make_rig
from so101_companion.control import requests as rq
from so101_companion.domain.state import Action, Connection, Mode, Torque


def test_startup_is_sim_only_and_creates_no_robot() -> None:
    rig = make_rig()
    s = rig.snap()
    assert (s.mode, s.connection, s.torque) == (Mode.SIM_ONLY, Connection.DISCONNECTED, Torque.NOT_APPLICABLE)
    assert not s.hardware_authorized and not s.calibration_valid
    rig.request(elbow_flex=40.0)
    rig.tick(50)
    assert rig.robots == []  # robot factory never called: no driver object, no port
    assert rig.coordinator.robot is None
    assert s.sent.values is None and s.actual.values is None  # never fabricated
    assert Action.ENABLE_MOVEMENT not in s.available_actions
    assert Action.MOVE_SLIDERS in s.available_actions


def test_large_jump_reaches_target_in_one_tick_in_simulation() -> None:
    """Fixed policy (ADR-010): no step limiter — a large request is staged in a single issued tick."""
    rig = make_rig()
    rig.request(elbow_flex=90.0, shoulder_pan=-90.0, gripper=100.0)
    rig.tick()
    s = rig.snap()
    assert s.staged.values["elbow_flex"] == 90.0 and s.staged.values["shoulder_pan"] == -90.0
    assert s.staged.values["gripper"] == 100.0
    assert dict(s.displayed.values) == dict(s.staged.values)
    assert s.clamp_reasons == ()


def test_consecutive_large_requests_are_each_forwarded() -> None:
    """Behavior-Engine style: successive large targets are staged exactly, tick after tick."""
    rig = make_rig()
    for target in (90.0, -90.0, 45.0, -45.0, 0.0):
        rig.request(elbow_flex=target)
        rig.tick()
        assert rig.snap().staged.values["elbow_flex"] == target


def test_displayed_follows_staged_in_sim_only() -> None:
    rig = make_rig()
    rig.request(shoulder_pan=25.0, gripper=80.0)
    rig.tick(5)
    s = rig.snap()
    assert dict(s.displayed.values) == dict(s.staged.values)
    assert s.staged.values["shoulder_pan"] == 25.0 and s.staged.values["gripper"] == 80.0


def test_sim_range_clamp() -> None:
    rig = make_rig()  # test sim bounds: ±100 with 5° margin -> ±95
    rig.request(wrist_flex=500.0, gripper=150.0)
    rig.tick(20)
    s = rig.snap()
    assert s.staged.values["wrist_flex"] == 95.0
    assert s.staged.values["gripper"] == 100.0
    assert "wrist_flex:range_max" in s.clamp_reasons


def test_latest_intent_coalescing() -> None:
    rig = make_rig()
    for v in range(1, 101):  # 100 UI events between two ticks
        rig.request(shoulder_pan=float(v) / 10)
    rig.tick()
    s = rig.snap()
    assert s.requested.values["shoulder_pan"] == 10.0  # only the latest survives
    assert s.staged.values["shoulder_pan"] == 10.0
    assert s.seq == 1


def test_invalid_intent_rejected_before_reaching_controller() -> None:
    import pytest

    from so101_companion.domain.joints import JointFrameError

    rig = make_rig()
    with pytest.raises(JointFrameError):
        rig.coordinator.set_requested({**SIM_INITIAL, "elbow_flex": float("nan")})
    with pytest.raises(JointFrameError):
        rig.coordinator.set_requested({k: v for k, v in SIM_INITIAL.items() if k != "gripper"})


def test_pause_freezes_progression_and_resume_continues() -> None:
    rig = make_rig()
    rig.request(elbow_flex=50.0)
    rig.tick()
    rig.run(rq.Pause())
    rig.request(elbow_flex=70.0)
    rig.tick(5)
    assert rig.snap().staged.values["elbow_flex"] == 50.0 and rig.snap().paused
    rig.run(rq.Resume())
    rig.tick()
    assert rig.snap().staged.values["elbow_flex"] == 70.0


def test_hardware_only_actions_rejected_in_sim_only() -> None:
    rig = make_rig()
    for req in (
        rq.EnableMovement(confirmed=True),
        rq.EmergencyHold(),
        rq.TorqueOff(confirmed=True),
        rq.Recover(confirmed=True),
        rq.Disconnect(confirmed=True),
        rq.SetMode(Mode.SIM_TO_REAL),
        rq.SetMode(Mode.REAL_TO_SIM),
    ):
        rig.run(req)
        assert "Rejected" in rig.snap().message
    assert rig.robots == []
    kinds = [r["kind"] for r in rig.coordinator.events.records()]
    assert kinds.count("request_rejected") == 7


def test_control_tick_records_follow_contract() -> None:
    rig = make_rig()
    rig.request(elbow_flex=500.0)
    rig.tick()
    rec = rig.coordinator.events.last("control_tick")
    assert rec is not None
    for key in (
        "seq",
        "t_monotonic_seconds",
        "mode",
        "authority",
        "requested",
        "staged",
        "sent",
        "actual",
        "displayed",
        "clamp_reasons",
        "last_observation_age_seconds",
        "cycle_duration_ms",
    ):
        assert key in rec
    assert rec["sent"] is None and rec["actual"] is None  # null = not available, never zero
    assert rec["clamp_reasons"] == ["elbow_flex:range_max"]

"""Fake-hardware controller tests: A07-A15, offline tests 6 and 8-14, SR-01..SR-08."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from helpers import FAKE_PORT, HW_POSE, make_rig
from so101_companion.control import requests as rq
from so101_companion.devices.fake import FaultPlan
from so101_companion.domain.state import Action, Connection, Mode, Torque


@pytest.fixture
def rig(calibration_file: Path):  # type: ignore[no-untyped-def]
    return make_rig(calibration_file)


def armed_rig(calibration_file: Path, **robot_kwargs):  # type: ignore[no-untyped-def]
    r = make_rig(calibration_file, robot_kwargs=robot_kwargs)
    r.connect()
    r.arm()
    assert r.snap().connection == Connection.ARMED
    return r


# ---------------------------------------------------------------- connection authority (A10, SR-01/08)
def test_connect_requires_explicit_confirmation(rig) -> None:  # type: ignore[no-untyped-def]
    rig.run(rq.Connect(FAKE_PORT, str(rig.calibration), confirmed=False))
    assert rig.robots == [] and rig.snap().connection == Connection.DISCONNECTED


def test_invalid_calibration_refuses_before_any_driver_object(tmp_path: Path) -> None:
    rig = make_rig(tmp_path / "missing.json")
    rig.connect()
    s = rig.snap()
    assert rig.robots == [] and s.connection == Connection.DISCONNECTED
    assert "calibration invalid" in s.message
    assert rig.coordinator.events.last("calibration_rejected") is not None


def test_connect_is_read_only_and_does_not_change_mode(rig) -> None:  # type: ignore[no-untyped-def]
    rig.connect()
    s = rig.snap()
    assert s.connection == Connection.CONNECTED_READ_ONLY
    assert s.mode == Mode.SIM_ONLY  # never auto-selects a hardware mode
    assert s.torque == Torque.ON and s.calibration_valid and s.hardware_authorized
    robot = rig.robot
    assert robot.calls[0] == ("initialize", (str(rig.calibration), FAKE_PORT))
    assert robot.count("send") == 0
    assert s.actual.values == pytest.approx(HW_POSE)  # first measured positions available (B01 shape)
    assert Action.ENABLE_MOVEMENT not in s.available_actions  # needs SIM_TO_REAL first
    assert "CALIBRATED" in rig.coordinator.hw_bounds.label
    rig.tick(20)
    assert robot.count("observe") == 1  # SIM_ONLY: no polling even while connected


def test_calibration_mismatch_latches_and_blocks_motion(calibration_file: Path) -> None:
    rig = make_rig(calibration_file, robot_kwargs={"faults": FaultPlan(calibration_mismatch=True)})
    rig.connect()
    s = rig.snap()
    assert s.connection == Connection.FAULT_LATCHED and s.fault.code == "CALIBRATION_MISMATCH"
    assert not s.calibration_valid and s.torque == Torque.ON  # no comm loss: torque state still known
    assert Action.SET_MODE_SIM_TO_REAL not in s.available_actions
    assert Action.ENABLE_MOVEMENT not in s.available_actions
    rig.run(rq.Recover(confirmed=True))
    assert rig.snap().connection == Connection.FAULT_LATCHED  # mismatch persists
    assert rig.robot.count("send") == 0


def test_connect_failure_leaves_disconnected(calibration_file: Path) -> None:
    rig = make_rig(calibration_file, robot_kwargs={"faults": FaultPlan(fail_connect=True)})
    rig.connect()
    s = rig.snap()
    assert s.connection == Connection.DISCONNECTED and rig.coordinator.robot is None
    assert "Connect failed" in s.message
    assert s.torque == Torque.UNKNOWN  # configure() may have toggled torque before failing


# ---------------------------------------------------------------- modes (A11, offline 9-10)
def test_real_to_sim_mirror_never_writes(rig) -> None:  # type: ignore[no-untyped-def]
    rig.connect()
    rig.run(rq.SetMode(Mode.REAL_TO_SIM))
    for v in range(100):
        rig.coordinator.set_requested({**HW_POSE, "elbow_flex": float(v)})
        rig.tick()
    rig.robot.move_by_hand(elbow_flex=42.0)
    rig.tick()
    s = rig.snap()
    assert rig.robot.count("send") == 0
    assert s.displayed.values["elbow_flex"] == 42.0  # pose comes from the measured observation
    assert s.actual.values["elbow_flex"] == 42.0
    assert s.requested.values is None and s.staged.values is None  # N/A, not a stale command
    assert Action.MOVE_SLIDERS not in s.available_actions


def test_sim_to_real_refuses_sends_until_enabled(rig) -> None:  # type: ignore[no-untyped-def]
    rig.connect()
    rig.run(rq.SetMode(Mode.SIM_TO_REAL))
    rig.request(elbow_flex=80.0)
    rig.tick(10)
    assert rig.robot.count("send") == 0
    rig.run(rq.EnableMovement(confirmed=False))
    assert rig.snap().connection == Connection.CONNECTED_READ_ONLY
    rig.run(rq.EnableMovement(confirmed=True))
    s = rig.snap()
    assert s.connection == Connection.ARMED
    assert dict(s.requested.values) == pytest.approx(HW_POSE)  # starts at the measured pose: no jump
    assert dict(s.staged.values) == pytest.approx(HW_POSE)
    rig.tick()
    assert rig.robot.count("send") == 1
    assert rig.robot.calls[-2][1] == pytest.approx(HW_POSE)  # first send holds the measured pose


def test_mode_exit_stops_target_progression(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, follow=0.0)
    rig.request(elbow_flex=90.0)
    rig.tick(2)
    sends = rig.robot.count("send")
    rig.run(rq.SetMode(Mode.REAL_TO_SIM))
    rig.tick(10)
    assert rig.robot.count("send") == sends
    assert rig.snap().connection == Connection.CONNECTED_READ_ONLY


# ---------------------------------------------------------------- staging & native cap (A07, A08, A12)
def test_targets_are_forwarded_directly_without_step_or_relative_cap(calibration_file: Path) -> None:
    """Fixed policy (ADR-010): a large in-range request is sent in ONE tick, unchanged."""
    rig = armed_rig(calibration_file, follow=0.0, initial_pose={**HW_POSE, "shoulder_pan": 5.0})
    rig.request(shoulder_pan=80.0, gripper=80.0)
    rig.tick()
    s = rig.snap()
    assert s.staged.values["shoulder_pan"] == 80.0 and s.sent.values["shoulder_pan"] == 80.0
    assert s.sent.values["gripper"] == 80.0
    assert s.actual.values["shoulder_pan"] == 5.0  # the servo has not moved yet (no fabricated feedback)
    assert rig.robot.calls[-2] == ("send", {**HW_POSE, "shoulder_pan": 80.0, "gripper": 80.0})
    assert s.clamp_reasons == ()


def test_consecutive_large_requests_are_forwarded_tick_by_tick(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file)
    sent = []
    for target in (80.0, -80.0, 60.0, -60.0):
        rig.request(shoulder_pan=target)
        rig.tick()
        sent.append(rig.robot.calls[-2][1]["shoulder_pan"])
    assert sent == [80.0, -80.0, 60.0, -60.0]
    assert rig.snap().connection == Connection.ARMED


def test_stage_values_stay_distinct_and_timestamped(calibration_file: Path) -> None:
    """requested (operator) != staged (clamped to calibrated bounds) == sent (forwarded) != actual
    (servo lag) — each with its own timestamp; nothing is substituted across stages."""
    rig = armed_rig(calibration_file, follow=0.5, initial_pose={**HW_POSE, "shoulder_pan": 5.0})
    upper = rig.coordinator.hw_bounds.limits["shoulder_pan"].upper
    rig.request(shoulder_pan=400.0)
    rig.tick()
    s = rig.snap()
    assert s.requested.get("shoulder_pan") == 400.0
    assert s.staged.get("shoulder_pan") == pytest.approx(upper)
    assert s.sent.get("shoulder_pan") == pytest.approx(upper)
    assert s.actual.get("shoulder_pan") == pytest.approx(5.0 + 0.5 * (upper - 5.0))
    assert "shoulder_pan:range_max" in s.clamp_reasons
    assert None not in (s.requested.t_mono, s.staged.t_mono, s.sent.t_mono, s.actual.t_mono)
    assert s.displayed.get("shoulder_pan") == s.actual.get("shoulder_pan")  # display = measured


def test_gripper_native_cap_100_does_not_clip_within_range(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, follow=0.0)  # gripper measured 0
    rig.request(gripper=80.0)
    rig.tick(8)
    s = rig.snap()
    assert s.staged.values["gripper"] == 80.0
    assert s.sent.values["gripper"] == 80.0  # 80 away from measured 0 but cap is 100
    assert s.actual.values["gripper"] == 0.0


def test_calibrated_range_clamp_applies_in_hardware_mode(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file)
    upper = rig.coordinator.hw_bounds.limits["shoulder_pan"].upper
    rig.request(shoulder_pan=400.0)
    rig.tick(30)
    s = rig.snap()
    assert s.staged.values["shoulder_pan"] == pytest.approx(upper)
    assert max(c[1]["shoulder_pan"] for c in rig.robot.calls if c[0] == "send") <= upper + 1e-9


def test_enabling_next_to_a_stop_does_not_move_the_joint(calibration_file: Path) -> None:
    """ADR-007: rest pose inside the 5° margin -> enabling movement commands the measured pose."""
    from so101_companion.devices.calibration import calibrated_degree_range, load_calibration

    lo, _ = calibrated_degree_range(load_calibration(calibration_file).motors["shoulder_lift"])
    resting = lo + 2.0  # inside the 5° margin
    rig = armed_rig(calibration_file, initial_pose={**HW_POSE, "shoulder_lift": resting})
    rig.tick(5)
    sends = [c[1]["shoulder_lift"] for c in rig.robot.calls if c[0] == "send"]
    assert sends and all(v == pytest.approx(resting) for v in sends)
    rig.request(shoulder_lift=resting - 10.0)  # further outward: refused
    rig.tick(3)
    assert rig.snap().staged.values["shoulder_lift"] == pytest.approx(resting)
    rig.request(shoulder_lift=0.0)  # inward: allowed, forwarded directly
    rig.tick(1)
    assert rig.snap().staged.values["shoulder_lift"] == pytest.approx(0.0)


# ---------------------------------------------------------------- faults (A13, offline 11-12, SR-04)
def test_read_failure_latches_and_freezes_everything(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, faults=FaultPlan(fail_read_after=4))
    rig.request(elbow_flex=60.0)
    rig.tick(10)
    s = rig.snap()
    assert s.connection == Connection.FAULT_LATCHED and s.fault.code == "READ_FAILED"
    assert s.torque == Torque.UNKNOWN and s.actual.stale
    sends, reads = rig.robot.count("send"), rig.robot.count("observe")
    for v in range(100):
        rig.coordinator.set_requested({**HW_POSE, "elbow_flex": float(v)})
        rig.tick()
    assert (rig.robot.count("send"), rig.robot.count("observe")) == (sends, reads)  # no bus activity at all
    rig.robot.faults.fail_read_after = None  # link would work again ...
    rig.tick(20)
    assert rig.snap().connection == Connection.FAULT_LATCHED  # ... but nothing clears it automatically
    assert len(rig.robots) == 1 and rig.robot.count("initialize") == 1  # no auto reconnect
    assert Action.MOVE_SLIDERS not in rig.snap().available_actions


def test_write_failure_keeps_previous_sent_and_never_fabricates_actual(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, follow=0.0, faults=FaultPlan(fail_write_after=2))
    rig.request(elbow_flex=90.0)
    rig.tick(2)
    before = rig.snap()
    rig.tick(1)
    s = rig.snap()
    assert s.connection == Connection.FAULT_LATCHED and s.fault.code == "WRITE_FAILED"
    assert s.sent.values == before.sent.values and s.sent.t_mono == before.sent.t_mono
    assert s.actual.values["elbow_flex"] == HW_POSE["elbow_flex"]  # last measured, not the target
    assert s.actual.stale


def test_corrupt_telemetry_latches(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, faults=FaultPlan(corrupt_read_after=3))
    rig.tick(5)
    assert rig.snap().fault.code == "INVALID_TELEMETRY"


def test_manual_recovery_requires_confirmation_and_fresh_read(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, faults=FaultPlan(fail_read_after=3))
    rig.tick(5)
    assert rig.snap().connection == Connection.FAULT_LATCHED
    rig.run(rq.Recover(confirmed=False))
    assert rig.snap().connection == Connection.FAULT_LATCHED
    rig.run(rq.Recover(confirmed=True))  # link still broken
    s = rig.snap()
    assert s.connection == Connection.FAULT_LATCHED and s.fault.count >= 2
    rig.robot.faults.fail_read_after = None
    rig.run(rq.Recover(confirmed=True))
    s = rig.snap()
    assert s.connection == Connection.CONNECTED_READ_ONLY and s.fault is None
    assert s.torque == Torque.UNKNOWN
    rig.run(rq.SetMode(Mode.SIM_TO_REAL))
    assert Action.ENABLE_MOVEMENT not in rig.snap().available_actions  # torque must be established first
    rig.run(rq.TorqueOff(confirmed=True), rq.EnableTorque(confirmed=True))
    s = rig.snap()
    assert s.torque == Torque.ON and Action.ENABLE_MOVEMENT in s.available_actions


# ---------------------------------------------------------------- hold / torque / disconnect (A14)
def test_emergency_hold_healthy(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, follow=0.5)
    rig.request(elbow_flex=90.0)
    rig.tick(3)
    rig.run(rq.EmergencyHold())
    s = rig.snap()
    assert s.connection == Connection.HOLDING
    measured = rig.robot.pose
    assert rig.robot.calls[-1][0] == "send" and rig.robot.calls[-1][1] == pytest.approx(measured)
    assert dict(s.requested.values) == pytest.approx(measured)
    sends = rig.robot.count("send")
    rig.request(elbow_flex=-50.0)
    rig.tick(10)
    assert rig.robot.count("send") == sends  # no progression while holding
    assert Action.ENABLE_MOVEMENT in rig.snap().available_actions  # explicit re-arm possible


def test_emergency_hold_on_dead_bus_reports_failure(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file)
    rig.robot.faults.unplugged = True
    rig.run(rq.EmergencyHold())
    s = rig.snap()
    assert s.connection == Connection.FAULT_LATCHED and s.fault.code == "HOLD_FAILED"
    assert "HOLD FAILED" in s.message and s.torque == Torque.UNKNOWN


def test_torque_off_only_after_confirmation(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file)
    rig.run(rq.TorqueOff(confirmed=False))
    assert rig.robot.count("torque_off") == 0
    rig.run(rq.TorqueOff(confirmed=True))
    s = rig.snap()
    assert rig.robot.count("torque_off") == 1 and not rig.robot.torque_enabled
    assert s.connection == Connection.TORQUE_OFF and s.torque == Torque.OFF
    rig.robot.move_by_hand(elbow_flex=12.0)  # operator moves the limp arm
    rig.run(rq.SetMode(Mode.REAL_TO_SIM))
    rig.tick()
    assert rig.snap().displayed.values["elbow_flex"] == 12.0  # mirror works with torque off


def test_torque_off_failure_latches_unknown(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, faults=FaultPlan(fail_torque_off=True))
    rig.run(rq.TorqueOff(confirmed=True))
    s = rig.snap()
    assert s.connection == Connection.FAULT_LATCHED and s.torque == Torque.UNKNOWN


def test_enable_torque_holds_present_pose(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file)
    rig.run(rq.TorqueOff(confirmed=True))
    rig.robot.move_by_hand(wrist_flex=33.0)
    rig.run(rq.EnableTorque(confirmed=True))
    s = rig.snap()
    assert s.torque == Torque.ON and s.connection == Connection.CONNECTED_READ_ONLY
    assert s.actual.values["wrist_flex"] == 33.0
    assert rig.robot.calls[-1][0] == "enable_torque_holding_present" and rig.robot.torque_enabled


@pytest.mark.parametrize("disable_torque", [True, False])
def test_disconnect_reports_torque_consequence(calibration_file: Path, disable_torque: bool) -> None:
    rig = armed_rig(calibration_file, disable_torque_on_disconnect=disable_torque)
    rig.run(rq.Disconnect(confirmed=False))
    assert rig.snap().connection == Connection.ARMED
    rig.run(rq.Disconnect(confirmed=True))
    s = rig.snap()
    assert s.connection == Connection.DISCONNECTED and s.mode == Mode.SIM_ONLY
    assert s.torque == (Torque.OFF if disable_torque else Torque.UNKNOWN)
    assert (
        rig.coordinator.events.last("disconnect_requested")["disable_torque_on_disconnect"] is disable_torque
    )
    assert not s.hardware_authorized and Action.CONNECT in s.available_actions


def test_disconnect_while_faulted_and_unplugged(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file)
    rig.robot.faults.unplugged = True
    rig.tick()
    assert rig.snap().connection == Connection.FAULT_LATCHED
    rig.run(rq.Disconnect(confirmed=True))
    s = rig.snap()
    assert s.connection == Connection.DISCONNECTED and s.torque == Torque.UNKNOWN
    assert rig.coordinator.events.last("disconnect_error") is not None


def test_stop_to_rest_unavailable_without_a_recorded_pose(calibration_file: Path) -> None:
    """A15 (as amended by T-039): no invented rest pose - Normal Stop needs a RECORDED physical pose."""
    rig = armed_rig(calibration_file)
    assert Action.STOP_TO_REST not in rig.snap().available_actions
    rig.run(rq.StopToRest())
    assert "Rejected" in rig.snap().message and rig.snap().connection == Connection.ARMED


def test_missing_lerobot_install_fails_connect_cleanly(calibration_file: Path) -> None:
    """A missing driver never breaks SIM_ONLY: Connect reports the problem and stays DISCONNECTED."""
    from helpers import SIM_INITIAL, sim_bounds
    from so101_companion.config import AppConfig
    from so101_companion.control.coordinator import ControllerCoordinator
    from so101_companion.devices.lerobot_so101 import LeRobotSO101Adapter
    from so101_companion.devices.protocol import UnsupportedDriverError

    def no_lerobot():  # type: ignore[no-untyped-def]
        raise UnsupportedDriverError("LeRobot is not installed/importable")

    cfg = AppConfig()
    c = ControllerCoordinator(
        cfg,
        sim_bounds(),
        SIM_INITIAL,
        lambda: LeRobotSO101Adapter(allowed_versions=("0.4.4",), api_loader=no_lerobot),
    )
    c.submit(rq.Connect(FAKE_PORT, str(calibration_file), confirmed=True))
    c.process_requests()
    s = c.snapshot()
    assert s.connection == Connection.DISCONNECTED and "not installed" in s.message
    assert s.torque != Torque.UNKNOWN  # failed before opening the port: nothing changed on the bus
    c.set_requested({**SIM_INITIAL, "elbow_flex": 20.0})
    c.tick()
    assert c.snapshot().staged.values["elbow_flex"] == 20.0  # simulation keeps working (forwarded directly)


# ---------------------------------------------------------------- runaway guard (ADR-009, hardware incident)
def test_runaway_is_reported_as_diagnostic_only(calibration_file: Path) -> None:
    """Fixed policy (ADR-010): divergence is logged and shown, but no fault, no torque change and
    sending continues. Stopping it is a manual operator decision (Emergency Hold / Torque Off)."""
    rig = armed_rig(calibration_file, faults=FaultPlan(runaway={"wrist_roll": -10.0}))
    rig.tick(6)
    s = rig.snap()
    assert s.connection == Connection.ARMED and s.fault is None and s.torque == Torque.ON
    assert "wrist_roll" in (s.extra.get("divergence_warning") or "")
    ev = rig.coordinator.events.last("tracking_divergence")
    assert ev is not None and ev["joints"] == ["wrist_roll"] and ev["action"] == "none (diagnostic)"
    assert not any(c[0].startswith("torque_off") for c in rig.robot.calls)
    sends = rig.robot.count("send")
    rig.tick(3)
    assert rig.robot.count("send") == sends + 3
    rig.run(rq.EmergencyHold())  # the operator's manual stop still works
    assert rig.snap().connection == Connection.HOLDING


def test_normal_lagging_motion_never_trips_the_guard(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, follow=0.3)
    for target in (60.0, -40.0, 80.0, 0.0):
        rig.request(elbow_flex=target, shoulder_pan=target / 2, gripper=abs(target))
        rig.tick(30)
        assert rig.snap().connection == Connection.ARMED


def test_slow_sag_below_threshold_does_not_trip(calibration_file: Path) -> None:
    rig = armed_rig(calibration_file, faults=FaultPlan(runaway={"shoulder_lift": -1.0}))
    rig.tick(5)
    assert rig.snap().connection == Connection.ARMED


def test_fast_slider_reversal_with_momentum_is_not_a_runaway(calibration_file: Path) -> None:
    """2026-11-07 hardware false trip: shoulder swept down fast, target reversed upward, the arm kept
    moving ~6° down for one tick (inertia/lag). The guard must judge against the goal in flight."""
    rig = armed_rig(calibration_file, follow=0.35, momentum=0.75)
    rig.request(shoulder_lift=-17.0)
    rig.tick(12)
    for target in (-76.5, -37.6, -80.0, -10.0):  # rapid reversals, like dragging the slider back and forth
        rig.request(shoulder_lift=target)
        rig.tick(4)
        assert rig.snap().connection == Connection.ARMED, rig.snap().message


# ---------------------------------------------------------------- rest pose + Normal Stop (T-039, ADR-011)
def _rest_rig(calibration_file: Path, **robot_kwargs):  # type: ignore[no-untyped-def]
    from so101_companion.devices.calibration import calibrated_degree_range, load_calibration

    lo, _ = calibrated_degree_range(load_calibration(calibration_file).motors["shoulder_lift"])
    resting = {**HW_POSE, "shoulder_lift": lo + 2.0}  # folded against the stop: inside the 5° margin
    return armed_rig(calibration_file, initial_pose=resting, **robot_kwargs), resting


def test_record_rest_pose_clamps_into_bounds_and_persists(calibration_file: Path) -> None:
    rig, resting = _rest_rig(calibration_file)
    rig.run(rq.RecordRestPose(confirmed=False))
    assert rig.coordinator.rest_pose is None
    rig.run(rq.RecordRestPose(confirmed=True))
    rest = rig.coordinator.rest_pose
    bound = rig.coordinator.hw_bounds.limits["shoulder_lift"].lower
    assert rest is not None and rest.robot_id == "test_follower"
    assert rest.measured["shoulder_lift"] == pytest.approx(resting["shoulder_lift"])
    assert rest.pose["shoulder_lift"] == pytest.approx(bound)  # owner decision: clamp to bounds
    assert rest.clamped_joints == ("shoulder_lift",)
    assert rig.coordinator.rest_store.load("test_follower") == rest
    assert "clamped" in rig.snap().message
    assert (
        rig.robot.count("send") == 0 or rig.snap().connection == Connection.ARMED
    )  # recording moves nothing


def test_normal_stop_returns_smoothly_then_holds_with_torque(calibration_file: Path) -> None:
    rig, _ = _rest_rig(calibration_file, follow=1.0)
    rig.run(rq.RecordRestPose(confirmed=True))
    goal = dict(rig.coordinator.rest_pose.pose)
    rig.request(shoulder_pan=60.0, elbow_flex=10.0)
    rig.tick()  # moved away from rest (forwarded directly)
    sends_before = rig.robot.count("send")
    rig.run(rq.StopToRest())
    s = rig.snap()
    assert s.connection == Connection.RETURNING_TO_REST
    assert Action.MOVE_SLIDERS not in s.available_actions  # sliders cannot fight the return
    rig.request(shoulder_pan=-80.0)  # ignored while returning
    targets = []
    for _ in range(200):
        rig.tick()
        if rig.snap().connection != Connection.RETURNING_TO_REST:
            break
        targets.append(rig.robot.calls[-2][1])
    s = rig.snap()
    assert s.connection == Connection.HOLDING and s.torque == Torque.ON
    assert rig.coordinator.events.last("rest_reached") is not None
    assert all(abs(s.actual.values[n] - goal[n]) <= 3.0 for n in goal)
    steps = [abs(b[n] - a[n]) for a, b in itertools.pairwise(targets) for n in goal]
    assert max(steps) <= 30.0 * 0.05 + 1e-6  # 30°/s at 20 Hz: smooth, synchronized
    assert -80.0 not in [t["shoulder_pan"] for t in targets]
    assert rig.robot.count("send") > sends_before + 10
    sends = rig.robot.count("send")
    rig.tick(10)
    assert rig.robot.count("send") == sends  # holding: no further targets


def test_normal_stop_timeout_holds_where_it_is(calibration_file: Path) -> None:
    rig, _ = _rest_rig(calibration_file, follow=1.0)
    rig.run(rq.RecordRestPose(confirmed=True))
    rig.request(shoulder_pan=60.0)
    rig.tick()
    rig.robot.follow = 0.0  # servo no longer follows (e.g. blocked)
    rig.run(rq.StopToRest())
    for _ in range(400):
        rig.tick()
        if rig.snap().connection != Connection.RETURNING_TO_REST:
            break
    s = rig.snap()
    assert s.connection == Connection.HOLDING and "REST NOT REACHED" in s.message
    ev = rig.coordinator.events.last("rest_not_reached")
    assert ev is not None and abs(ev["errors"]["shoulder_pan"]) > 3.0
    assert rig.robot.calls[-1][0] in ("send", "observe")


def test_emergency_hold_and_torque_off_abort_the_return(calibration_file: Path) -> None:
    rig, _ = _rest_rig(calibration_file, follow=0.2)
    rig.run(rq.RecordRestPose(confirmed=True))
    rig.request(shoulder_pan=60.0)
    rig.tick(5)
    rig.run(rq.StopToRest())
    rig.tick(3)
    rig.run(rq.EmergencyHold())
    assert rig.snap().connection == Connection.HOLDING
    sends = rig.robot.count("send")
    rig.tick(10)
    assert rig.robot.count("send") == sends
    rig.run(rq.EnableMovement(confirmed=True), rq.StopToRest())
    rig.tick(2)
    rig.run(rq.TorqueOff(confirmed=True))
    assert rig.snap().connection == Connection.TORQUE_OFF


def test_rest_pose_is_reloaded_on_connect_only_for_the_same_calibration(calibration_file: Path) -> None:
    from so101_companion.devices.rest_pose import MemoryRestPoseStore

    store = MemoryRestPoseStore()
    rig, _ = _rest_rig(calibration_file)
    rig.coordinator.rest_store = store
    rig.run(rq.RecordRestPose(confirmed=True), rq.Disconnect(confirmed=True))
    rig.coordinator.rest_pose = None
    rig.connect()
    assert rig.coordinator.rest_pose == store.load("test_follower")
    assert rig.coordinator.events.last("rest_pose_loaded") is not None

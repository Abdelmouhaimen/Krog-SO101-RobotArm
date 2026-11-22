"""SO-101 Studio panel in the single window (offscreen Qt, fake robot, fake clock).

Covers the redesign's Definition of Done with fake hardware: one mode dropdown (with its workflow and
confirmations), the four context-aware primary controls (Record | Play | Pause/Resume | Go Rest),
Emergency Hold always visible, behavior library (New keyframes, Rename, Duplicate, Delete), speeds and
transition presets, timeline (trim, scrub), status text, reasons near disabled controls, and the M2
acceptance items previously tested on the old panel (A02, A05, A13–A16, A18, A22, requirement 5).
"""

from __future__ import annotations

from typing import Any

import pytest
from test_main_window import Gui, gui  # noqa: F401  (fixture re-export)

from helpers import HW_POSE
from so101_companion.behaviors.model import Origin, Sample
from so101_companion.behaviors.repository import new_behavior
from so101_companion.control import requests as rq
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Action, Connection, Mode, Torque
from so101_companion.safety.policy import TransitionProfile

pytestmark = pytest.mark.gui


def control_ready(g: Gui) -> None:
    """Simulation → Real through the dropdown, with a recorded rest pose."""
    g.select_mode(Mode.SIM_TO_REAL)
    g.click(Action.RECORD_REST_POSE)


def add_gesture(g: Gui, *, closed: bool = False) -> Any:
    fp = g.rig.snap().extra["calibration_fingerprint"]
    n = 21

    def elbow(i: int) -> float:
        return HW_POSE["elbow_flex"] + (8.0 * (1 - abs(10 - i) / 10) if closed else 0.4 * i)

    samples = tuple(Sample(round(i * 0.05, 6), actual=HW_POSE | {"elbow_flex": elbow(i)}) for i in range(n))
    b = g.w.behavior_service.repository.create(
        new_behavior("wave", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=fp)
    )
    g.w.studio.refresh_library(select=b.id)
    return b


def finish_preview(g: Gui) -> None:
    p = g.w.studio
    p.preview_button.click() if p.preview_button.isVisible() else p.play_button.click()
    assert p.preview is not None
    p.preview_step(p.preview.duration + 0.1)
    g.pump(0)


# ------------------------------------------------------------------ layout and defaults
def test_studio_defaults_are_safe_and_simple(gui: Gui) -> None:  # noqa: F811
    p = gui.w.studio
    assert p.window() is gui.w and p.mode_combo.currentData() == Mode.SIM_ONLY
    assert [p.mode_combo.itemText(i) for i in range(3)] == [
        "Simulation Only", "Real → Simulation", "Simulation → Real",
    ]  # fmt: skip
    assert p.record_button.isEnabled()  # Simulation Only: record the sliders
    assert not p.play_button.isEnabled() and not p.pause_button.isEnabled()  # nothing to play yet
    assert p.rest_button.isEnabled() and not p.hold_button.isEnabled()  # no arm connected
    assert p.hold_button.isVisibleTo(gui.w)  # always visible
    assert p.entry_preset.currentData() == TransitionProfile.NORMAL
    assert p.bridge_preset.currentData() == TransitionProfile.FAST
    assert "Record" in p.reason_label.text() and "Ready" in p.status_pill.text()
    assert not p.preview_button.isVisibleTo(gui.w)  # in simulation, Play is the 3D playback


def test_viewport_badge_follows_mode_and_preview(gui: Gui) -> None:  # noqa: F811
    w = gui.w
    gui.pump()
    assert w.viewport.badge is not None and "SIMULATION ONLY" in w.viewport.badge[0]
    gui.select_mode(Mode.REAL_TO_SIM)
    assert "REAL → SIM" in w.viewport.badge[0]
    gui.select_mode(Mode.SIM_TO_REAL)
    assert "SIM → REAL" in w.viewport.badge[0]


# ------------------------------------------------------------------ mode dropdown workflow
def test_mode_dropdown_confirms_and_runs_the_workflow(gui: Gui) -> None:  # noqa: F811
    p = gui.w.studio
    gui.confirmer.answer = False
    gui.select_mode(Mode.REAL_TO_SIM)
    assert gui.rig.robots == [] and p.mode_combo.currentData() == Mode.SIM_ONLY  # declined: untouched
    assert "First the app connects" in gui.confirmer.mode_texts[-1]
    gui.confirmer.answer = True
    gui.select_mode(Mode.REAL_TO_SIM)
    s = gui.rig.snap()
    assert (
        s.mode == Mode.REAL_TO_SIM
        and s.torque == Torque.OFF
        and p.mode_combo.currentData() == Mode.REAL_TO_SIM
    )
    assert p.record_button.isEnabled()  # hand-guided recording is ready
    gui.select_mode(Mode.SIM_TO_REAL)
    s = gui.rig.snap()
    assert s.connection == Connection.ARMED and s.torque == Torque.ON
    assert (
        "ENERGIZED" in gui.confirmer.mode_texts[-1]
        and "First the app connects" not in gui.confirmer.mode_texts[-1]
    )
    n = len(gui.confirmer.asked)
    gui.select_mode(Mode.SIM_ONLY)  # no confirmation: stops motion targets, the arm keeps holding
    assert len(gui.confirmer.asked) == n and gui.rig.snap().torque == Torque.ON


def test_mode_change_without_connection_settings_explains_and_reverts(gui: Gui) -> None:  # noqa: F811
    gui.w.port_edit.setText("")
    gui.select_mode(Mode.SIM_TO_REAL)
    assert (
        gui.confirmer.errors[-1][0] == "Connection not set up" and "Advanced" in gui.confirmer.errors[-1][1]
    )
    assert gui.w.studio.mode_combo.currentData() == Mode.SIM_ONLY and gui.rig.robots == []


def test_mode_dropdown_is_locked_while_the_arm_plays(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    add_gesture(gui)
    p = gui.w.studio
    gui.rig.coordinator.submit(rq.PlayBehavior(p.behavior, p.settings(), None, confirmed=True))
    gui.pump()
    assert gui.rig.snap().connection == Connection.PLAYING_BEHAVIOR and not p.mode_combo.isEnabled()


# ------------------------------------------------------------------ record
def test_slider_take_is_recorded_and_auto_saved_without_a_popup(gui: Gui) -> None:  # noqa: F811
    p, w = gui.w.studio, gui.w
    p.record_button.click()
    gui.pump()
    assert "Stop" in p.record_button.text() and "Recording" in p.status_pill.text()
    assert w.advanced.expanded  # Simulation Only: the joint sliders are revealed
    for v in range(0, 200, 20):
        w.sliders["elbow_flex"].setValue(v)
        gui.pump()
    p.record_button.click()
    gui.pump()
    assert "details" not in gui.confirmer.asked  # no naming pop-up
    assert p.behavior is not None and p.behavior.origin is Origin.SIM_SLIDERS
    assert p.behavior.name.startswith("Slider take") and "Saved" in p.status_text.text()
    assert gui.rig.robots == []


def test_invalid_hand_guided_take_is_reported_and_not_saved(gui: Gui) -> None:  # noqa: F811
    gui.select_mode(Mode.REAL_TO_SIM)
    p = gui.w.studio
    p.record_button.click()
    gui.pump(5)
    gui.rig.robot.faults.read_latency_s = 0.4
    gui.pump()
    gui.rig.robot.faults.read_latency_s = 0.0
    gui.pump(3)
    p.record_button.click()
    gui.pump()
    assert gui.confirmer.errors and gui.confirmer.errors[-1][0] == "Recording not playable"
    assert "ms gap" in gui.confirmer.errors[-1][1] and p.behavior is None
    assert gui.rig.snap().torque == Torque.OFF


def test_record_is_explained_when_unavailable(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    add_gesture(gui)
    p = gui.w.studio
    assert not p.record_button.isEnabled() and "To record" in p.reason_label.text()


# ------------------------------------------------------------------ play / pause / go rest (simulation)
def test_play_in_simulation_pause_resume_and_go_rest(gui: Gui) -> None:  # noqa: F811
    p = gui.w.studio
    b = gui.w.behavior_service.create_keyframe_behavior(
        "nod",
        [
            (0.0, dict.fromkeys(JOINT_NAMES, 0.0)),
            (1.0, dict.fromkeys(JOINT_NAMES, 0.0) | {"elbow_flex": 30.0}),
        ],
    )
    p.refresh_library(select=b.id)
    gui.pump()
    p.play_button.click()
    assert p.preview is not None and "Previewing" in p.status_pill.text()
    assert gui.w.viewport.badge is not None and "PREVIEW" in gui.w.viewport.badge[0]
    p.preview_step(0.3)
    assert p.pause_button.isEnabled() and "Pause" in p.pause_button.text()
    p.pause_button.click()
    assert (
        p.preview_paused_at is not None
        and "Resume" in p.pause_button.text()
        and "Paused" in p.status_pill.text()
    )
    p.pause_button.click()
    assert p.preview_paused_at is None
    p.rest_button.click()  # Go Rest ends the 3D playback and moves the simulated arm to rest
    gui.pump()
    assert p.preview is None and gui.rig.coordinator.events.last("sim_rest_started") is not None
    assert gui.rig.robots == []


def test_physical_pose_beyond_the_model_range_is_noted_not_clamped(gui: Gui) -> None:  # noqa: F811
    """Requirement 5: drawn at the model limit WITH a visible note; the trajectory is not clamped."""
    p = gui.w.studio
    deep = dict.fromkeys(JOINT_NAMES, 0.0) | {"wrist_flex": -97.0}
    b = gui.w.behavior_service.create_keyframe_behavior("deep", [(0.0, deep), (1.0, deep)])
    p.refresh_library(select=b.id)
    gui.pump()
    p.play_button.click()
    assert p.preview is not None
    p.preview_step(p.preview.plan.entry.duration + 0.5)
    assert "wrist_flex -97.0° drawn at" in p.display_limit_label.text()
    assert "NOT clamped" in p.display_limit_label.text()
    assert gui.w.preview_pose is not None and gui.w.preview_pose["wrist_flex"] == -97.0


# ------------------------------------------------------------------ play / pause / go rest (real arm)
def test_real_play_without_preview_then_pause_resume_and_go_rest(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    add_gesture(gui)
    p = gui.w.studio
    assert p.play_button.isEnabled() and p.preview_button.isVisibleTo(gui.w)
    p.play_button.click()
    text = gui.confirmer.play_texts[-1]
    assert gui.confirmer.asked[-1] == "play" and "NO 3D preview" in text and "Known open risks" in text
    assert "Getting into position: Normal" in text and "ENERGIZES" not in text
    gui.pump(3)
    assert gui.rig.snap().connection == Connection.PLAYING_BEHAVIOR and "Playing" in p.status_pill.text()
    assert not any(s.isEnabled() for s in gui.w.sliders.values())  # A22: sliders cannot fight the player
    p.pause_button.click()
    gui.pump(3)
    assert gui.rig.snap().extra["behavior"]["state"] == "paused" and "Paused" in p.status_pill.text()
    assert "Resume" in p.pause_button.text() and gui.rig.snap().connection == Connection.PLAYING_BEHAVIOR
    p.pause_button.click()
    gui.pump(2)
    assert gui.rig.snap().extra["behavior"]["state"] == "playing"
    p.rest_button.click()  # interrupting motion: no extra confirmation
    gui.pump()
    assert gui.rig.snap().connection == Connection.RETURNING_TO_REST and "go_rest" not in gui.confirmer.asked
    assert "stopped" in (gui.rig.snap().extra["behavior"]["last_result"] or "")


def test_preview_authorizes_one_run_and_any_change_invalidates_it(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    add_gesture(gui)
    p = gui.w.studio
    sends_before = gui.rig.robot.count("send")
    finish_preview(gui)
    assert (
        gui.rig.robot.count("send") - sends_before <= 2
    )  # only the ARMED hold ticks: preview never commands
    assert "authorizes ONE real run" in p.preview_label.text() and p._approval_valid()
    p.set_speed(1.5)
    assert not p._approval_valid() and "no longer valid" in p.preview_label.text()
    finish_preview(gui)
    p.bridge_preset.setCurrentIndex(p.bridge_preset.findData(TransitionProfile.GENTLE))
    assert not p._approval_valid()  # presets are part of the previewed trajectory
    finish_preview(gui)
    p.play_button.click()
    assert "Preview authorized" in gui.confirmer.play_texts[-1]
    gui.pump(2)
    started = gui.rig.coordinator.events.last("behavior_started")
    assert started is not None and started["previewed"] is True and started["bridge_profile"] == "gentle"
    assert p.approval is None  # consumed


def test_declined_play_consumes_the_preview(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    add_gesture(gui)
    p = gui.w.studio
    finish_preview(gui)
    gui.confirmer.answer = False
    p.play_button.click()
    gui.pump()
    assert gui.rig.snap().connection != Connection.PLAYING_BEHAVIOR and p.approval is None


def test_play_with_torque_off_discloses_energizing(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    add_gesture(gui)
    gui.click(Action.TORQUE_OFF)  # manual torque control stays available in Advanced
    p = gui.w.studio
    p.play_button.click()
    assert "ENERGIZES all six motors" in gui.confirmer.play_texts[-1]
    gui.pump()
    assert gui.rig.snap().torque == Torque.ON and gui.rig.snap().connection == Connection.PLAYING_BEHAVIOR


def test_go_rest_from_a_still_arm_asks_first(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    gui.w.hold_button.click()
    gui.pump()
    assert gui.rig.snap().connection == Connection.HOLDING
    gui.confirmer.answer = False
    gui.w.studio.rest_button.click()
    gui.pump()
    assert gui.confirmer.asked[-1] == "go_rest" and gui.rig.snap().connection == Connection.HOLDING
    gui.confirmer.answer = True
    gui.w.studio.rest_button.click()
    gui.pump()
    assert gui.rig.snap().connection == Connection.RETURNING_TO_REST


def test_seamless_gesture_is_announced_in_the_confirmation(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    add_gesture(gui, closed=True)
    p = gui.w.studio
    p.repeats.setValue(3)
    p.play_button.click()
    assert "no bridge" in gui.confirmer.play_texts[-1]


# ------------------------------------------------------------------ library, timeline, keyframes
def test_rename_duplicate_delete_inline(gui: Gui) -> None:  # noqa: F811
    p = gui.w.studio
    b = gui.w.behavior_service.create_keyframe_behavior(
        "nod", [(0.0, dict.fromkeys(JOINT_NAMES, 0.0)), (1.0, dict.fromkeys(JOINT_NAMES, 0.0))]
    )
    p.refresh_library(select=b.id)
    p.act_rename.trigger()
    assert p.rename_edit.isVisibleTo(p) and not p.behavior_combo.isVisibleTo(p)
    p.rename_edit.setText("Excited")
    p.rename_edit.returnPressed.emit()
    assert p.behavior is not None and p.behavior.name == "Excited" and p.behavior.revision == 2
    p.act_duplicate.trigger()
    assert p.behavior.name == "Excited copy" and p.behavior.id != b.id and p.behavior_combo.count() == 2
    p.act_delete.trigger()
    assert gui.confirmer.asked[-1] == "delete" and p.behavior_combo.count() == 1


def test_trim_handles_and_scrub(gui: Gui) -> None:  # noqa: F811
    p = gui.w.studio
    b = gui.w.behavior_service.create_keyframe_behavior(
        "nod",
        [
            (0.0, dict.fromkeys(JOINT_NAMES, 0.0)),
            (2.0, dict.fromkeys(JOINT_NAMES, 0.0) | {"elbow_flex": 40.0}),
        ],
    )
    p.refresh_library(select=b.id)
    gui.pump()
    p.timeline.trim_changed.emit(0.5, 1.5)
    s = p.settings()
    assert (s.trim_start_s, s.trim_end_s) == (0.5, 1.5) and "Trim 0.50 s – 1.50 s" in p.trim_label.text()
    p.timeline.scrubbed.emit(1.0)
    assert (
        p.scrubbing
        and gui.w.preview_pose is not None
        and gui.w.preview_pose["elbow_flex"] == pytest.approx(20.0)
    )
    p.live_button.click()
    assert not p.scrubbing and gui.w.preview_pose is None


def test_moving_only_the_start_handle_saves_on_a_non_millisecond_duration(gui: Gui) -> None:  # noqa: F811
    """Real-arm report 2026-11-08: a 3.6496 s recording, start handle moved to 0.603 s -> the untouched end
    was rounded to 3.650 s (past the source) and saving failed with 'trim ... invalid'."""
    p = gui.w.studio
    dur = 3.6496
    b = gui.w.behavior_service.create_keyframe_behavior(
        "talk", [(0.0, dict.fromkeys(JOINT_NAMES, 0.0)), (dur, dict.fromkeys(JOINT_NAMES, 0.0))]
    )
    p.refresh_library(select=b.id)
    gui.pump()
    tl = p.timeline
    assert tl.duration == pytest.approx(dur)
    tl.trim_start = 0.6031  # what a drag of the start handle leaves; the end handle sits at the duration
    start, end = tl.rounded_trim()
    assert start == 0.603 and end == dur  # never rounded past the source
    p.timeline.trim_changed.emit(0.603, 3.650)  # an old-style emit must still be accepted
    s = p.settings()
    assert s.trim_start_s == 0.603 and s.trim_end_s is None  # "to the end"
    p._on_save_settings()
    assert not gui.confirmer.errors and p.behavior is not None and p.behavior.settings.trim_start_s == 0.603


def test_keyframe_editing_creates_a_new_revision(gui: Gui) -> None:  # noqa: F811
    p = gui.w.studio
    p.act_new_keyframes.trigger()
    assert p.behavior is not None and p.behavior.origin is Origin.SIM_KEYFRAMES
    assert p.keyframes_toggle.isChecked() and p.kf_table.isVisibleTo(p) and gui.w.advanced.expanded
    p.kf_add.click()
    assert p.kf_table.rowCount() == 3
    p.kf_table.item(2, 3).setText("40.0")  # elbow_flex of the new keyframe
    p.kf_save.click()
    assert p.behavior.revision == 2 and p.behavior.samples[2].requested["elbow_flex"] == 40.0
    p.kf_table.item(1, 0).setText("5.0")  # out of order
    p.kf_save.click()
    assert gui.confirmer.errors[-1][0] == "Keyframes not saved"


# ------------------------------------------------------------------ in-window confirmation cards
def test_overlay_confirmer_stays_inside_the_window(qapp: Any) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMainWindow

    from so101_companion.ui.overlay import OverlayConfirmer

    host = QMainWindow()
    host.resize(800, 600)
    host.show()
    try:
        conf = OverlayConfirmer(host)
        tops_before = [w for w in qapp.topLevelWidgets() if w.isVisible()]
        seen: list[Any] = []

        def answer(ok: bool) -> None:
            seen.append([w for w in qapp.topLevelWidgets() if w.isVisible()])
            (conf.card.ok if ok else conf.card.cancel).click()

        QTimer.singleShot(20, lambda: answer(True))
        assert conf.confirm_go_rest("The arm will move.") is True
        QTimer.singleShot(20, lambda: answer(False))
        assert conf.confirm_torque_off() is False
        assert all(tops == tops_before for tops in seen)  # no separate pop-up window
        assert not conf.card.isVisible()
    finally:
        host.close()


def test_save_rest_replaces_go_rest_in_real_to_sim(gui: Gui) -> None:  # noqa: F811
    """Owner request: in Real → Simulation the fourth control saves the hand-placed pose as rest pose."""
    gui.select_mode(Mode.REAL_TO_SIM)
    p = gui.w.studio
    assert "Save Rest" in p.rest_button.text() and p.rest_button.isEnabled()
    parked = HW_POSE | {"shoulder_pan": 12.0, "elbow_flex": 40.0}
    gui.rig.robot.move_by_hand(**parked)
    gui.pump(2)
    sends_before = gui.rig.robot.count("send")
    gui.confirmer.answer = False
    p.rest_button.click()
    gui.pump()
    assert gui.confirmer.asked[-1] == "record_rest" and gui.rig.snap().extra["rest_pose"] is None
    gui.confirmer.answer = True
    p.rest_button.click()
    gui.pump()
    rest = gui.rig.snap().extra["rest_pose"]
    assert rest is not None and rest["pose"]["shoulder_pan"] == pytest.approx(12.0, abs=0.01)
    assert gui.rig.robot.count("send") == sends_before  # nothing moved
    assert gui.rig.snap().torque == Torque.OFF
    assert "Rest pose saved" in p.status_text.text()
    gui.select_mode(Mode.SIM_TO_REAL)
    assert "Go Rest" in p.rest_button.text()


# ------------------------------------------------------------------ ADR-017: endings and saved poses
def run_until(g: Gui, state: Connection, limit: int = 400) -> None:
    for _ in range(limit):
        if g.rig.snap().connection != state:
            return
        g.pump()
    raise AssertionError(f"still {state}")


def test_after_behavior_is_saved_with_the_behavior_and_held_after_play(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    b = add_gesture(gui)
    p = gui.w.studio
    assert p.after_combo.currentData() == "rest" and not p.after_pose_combo.isVisibleTo(p)
    p.after_combo.setCurrentIndex(p.after_combo.findData("pose"))
    p.after_combo.activated.emit(p.after_combo.currentIndex())  # no poses yet
    assert gui.confirmer.errors[-1][0] == "No saved poses yet" and p.after_combo.currentData() == "rest"
    p.after_combo.setCurrentIndex(p.after_combo.findData("hold"))
    p.after_combo.activated.emit(p.after_combo.currentIndex())
    stored = gui.w.behavior_service.load(b.id)
    assert stored.settings.after_action == "hold" and stored.revision == b.revision + 1  # saved in the file
    assert "Hold last position" in p.status_text.text()
    p.play_button.click()
    assert "HOLDS its last position" in gui.confirmer.play_texts[-1]
    gui.pump()
    run_until(gui, Connection.PLAYING_BEHAVIOR)
    assert gui.rig.snap().connection == Connection.HOLDING
    assert gui.rig.coordinator.events.last("stop_to_rest_started") is None
    assert "holding its last position" in p.status_text.text()


def test_saved_poses_save_preview_go_rename_delete_in_simulation(gui: Gui) -> None:  # noqa: F811
    p, w = gui.w.studio, gui.w
    w.sliders["elbow_flex"].setValue(300)
    gui.pump(20)
    p.save_pose_button.click()
    assert p._selected_pose() is not None and p._selected_pose().source.value == "simulation"
    assert p._selected_pose().pose["elbow_flex"] == pytest.approx(30.0) and "from the 3D arm" in p.notice
    p.act_preview_pose.trigger()
    assert p.scrubbing and w.preview_pose is not None and w.preview_pose["elbow_flex"] == pytest.approx(30.0)
    assert w.viewport.badge is not None and "PREVIEW" in w.viewport.badge[0]
    p.live_button.click()
    w.sliders["elbow_flex"].setValue(0)
    gui.pump(30)
    p.go_pose_button.click()  # Simulation Only: the 3D arm, no confirmation, no hardware
    gui.pump(80)
    assert gui.rig.coordinator.events.last("sim_pose_reached") is not None and gui.rig.robots == []
    p.act_rename_pose.trigger()
    assert p.pose_rename_edit.isVisibleTo(p)
    p.pose_rename_edit.setText("Talking Idle")
    p.pose_rename_edit.returnPressed.emit()
    assert p._selected_pose().name == "Talking Idle"
    p.act_delete_pose.trigger()
    assert gui.confirmer.asked[-1] == "delete" and p._selected_pose() is None


def test_ending_at_a_saved_pose_and_go_to_pose_on_the_real_arm(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    p = gui.w.studio
    gui.rig.robot.move_by_hand(shoulder_pan=HW_POSE["shoulder_pan"] + 25.0)
    gui.pump(2)
    p.save_pose_button.click()
    pose = p._selected_pose()
    assert pose is not None and pose.source.value == "real_arm" and pose.calibration_fingerprint
    gui.confirmer.answer = False
    p.go_pose_button.click()
    gui.pump()
    assert gui.confirmer.asked[-1] == "move_pose" and gui.rig.snap().connection != Connection.MOVING_TO_POSE
    gui.confirmer.answer = True
    b = add_gesture(gui)
    p.after_combo.setCurrentIndex(p.after_combo.findData("pose"))
    p.after_combo.activated.emit(p.after_combo.currentIndex())
    assert p.after_pose_combo.isVisibleTo(p)
    assert gui.w.behavior_service.load(b.id).settings.after_pose_id == pose.id
    p.play_button.click()
    assert f"moves smoothly to pose '{pose.name}'" in gui.confirmer.play_texts[-1]
    gui.pump()
    run_until(gui, Connection.PLAYING_BEHAVIOR)
    assert gui.rig.snap().connection == Connection.MOVING_TO_POSE
    assert "Moving to pose" in p.status_pill.text() and p.chips.chips[2].text() == "Moving to pose"
    run_until(gui, Connection.MOVING_TO_POSE)
    assert gui.rig.snap().connection == Connection.HOLDING


def test_deleted_ending_pose_blocks_play_and_explains(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    p = gui.w.studio
    p.save_pose_button.click()
    pose = p._selected_pose()
    add_gesture(gui)
    p.after_combo.setCurrentIndex(p.after_combo.findData("pose"))
    p.after_combo.activated.emit(p.after_combo.currentIndex())
    gui.w.behavior_service.delete_pose(pose.id)
    p.refresh_poses()
    gui.pump()
    assert p.ending_pose_missing() and not p.play_button.isEnabled()
    assert "deleted" in p.reason_label.text() and "missing pose" in p.after_pose_combo.currentText()


def test_finish_button_and_go_to_pose_while_playing(gui: Gui) -> None:  # noqa: F811
    control_ready(gui)
    p = gui.w.studio
    p.save_pose_button.click()
    add_gesture(gui)
    assert not p.finish_button.isVisibleTo(p)
    p.forever.setChecked(True)
    p.play_button.click()
    gui.pump(5)
    assert p.finish_button.isVisibleTo(p) and p.finish_button.isEnabled()
    p.finish_button.click()
    gui.pump()
    assert "finishing after loop 1" in p.status_text.text() and not p.finish_button.isEnabled()
    run_until(gui, Connection.PLAYING_BEHAVIOR)
    assert gui.rig.snap().connection == Connection.RETURNING_TO_REST  # its ending is the default rest
    run_until(gui, Connection.RETURNING_TO_REST)
    p.play_button.click()
    gui.pump(5)
    n = len(gui.confirmer.asked)
    p.go_pose_button.click()  # already moving: like Go Rest, no extra card
    gui.pump()
    assert len(gui.confirmer.asked) == n and gui.rig.snap().connection == Connection.MOVING_TO_POSE


def test_poses_saved_earlier_appear_in_both_selectors_at_startup(
    qapp, pose_scene, calibration_file, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Owner finding: after a restart the Poses dropdown (Go) was empty while the After-behavior selector
    listed the pose. Both must show the poses on disk from the start."""
    from test_main_window import FakeFrameSource, ScriptedConfirmer

    from helpers import make_rig
    from so101_companion.behaviors.poses import PoseRepository, PoseSource
    from so101_companion.config import AppConfig
    from so101_companion.simulation.mapping import mapping_from_config
    from so101_companion.ui.main_window import MainWindow

    zero = dict.fromkeys(JOINT_NAMES, 0.0)
    saved = PoseRepository(tmp_path / "poses").create("Talking Idle", zero, PoseSource.SIMULATION)
    cfg = AppConfig()
    rig = make_rig(calibration_file)
    w = MainWindow(
        pose_scene,
        mapping_from_config(pose_scene.model_ranges_rad(), cfg.simulation),
        rig.coordinator,
        FakeFrameSource,
        cfg,
        confirmer=ScriptedConfirmer(),
        start_worker=False,
        behaviors_dir=tmp_path / "behaviors",
        poses_dir=tmp_path / "poses",
    )
    try:
        p = w.studio
        assert p.pose_combo.findData(saved.id) >= 0 and p.pose_combo.currentText() == "Talking Idle"
        assert p._selected_pose() is not None
        w.apply_snapshot(rig.snap())
        assert p.go_pose_button.isEnabled()  # Simulation Only: Go moves the 3D arm
    finally:
        w.close()


def test_another_behavior_can_be_played_while_one_runs(gui: Gui) -> None:  # noqa: F811
    """ADR-018 in the Studio: the same supersede semantics as the API (no return to rest in between)."""
    control_ready(gui)
    p = gui.w.studio
    first = add_gesture(gui)
    p.forever.setChecked(True)
    p.play_button.click()
    gui.pump(6)
    assert gui.rig.snap().connection == Connection.PLAYING_BEHAVIOR
    second = add_gesture(gui, closed=True)  # selected while the first one plays
    assert p.behavior_combo.isEnabled() and p.behavior is not None and p.behavior.id == second.id
    assert p.play_button.isEnabled() and p.after_combo.isEnabled()
    p.forever.setChecked(False)
    p.play_button.click()
    assert "REPLACES the motion" in gui.confirmer.play_texts[-1]
    gui.pump()
    s = gui.rig.snap()
    assert s.connection == Connection.PLAYING_BEHAVIOR and s.extra["behavior"]["behavior_id"] == second.id
    superseded = gui.rig.coordinator.events.last("behavior_superseded")
    assert superseded is not None and superseded["behavior_id"] == first.id
    assert gui.rig.coordinator.events.last("stop_to_rest_started") is None
    rec = [r for r in gui.rig.coordinator.commands.list() if r["kind"] == "play_behavior"]
    assert [r["source"] for r in rec] == ["studio", "studio"] and rec[1]["superseded_by"] == rec[0]["id"]


def test_follow_me_and_talk_controls_in_simulation_and_on_the_arm(gui: Gui) -> None:  # noqa: F811
    """ADR-019 Studio card: compact Follow Me / Talk controls, pan/gripper sliders yield to the overlays,
    technical reference in Advanced; on the real arm, starting an overlay asks first."""
    from overlay_helpers import analyzer, fresh_analyzer

    gui.rig.coordinator.directions = fresh_analyzer()
    gui.w.behavior_service.directions = analyzer()
    p = gui.w.studio
    gui.pump()
    assert (
        p.follow_button.isEnabled() and p.talk_button.isEnabled() and not p.stop_follow_button.isVisibleTo(p)
    )
    p.follow_target.setValue(30.0)
    p.follow_button.click()  # Simulation Only: no confirmation (the 3D arm only)
    gui.pump(40)
    f = gui.rig.snap().extra["follow"]
    assert f["enabled"] and f["state"] == "TRACKING" and "overlay:Follow Me" not in gui.confirmer.asked
    assert p.follow_button.text() == "Follow Me: ON" and p.stop_follow_button.isVisibleTo(p)
    assert "Facing +30°" in p.follow_status.text() and "NORMAL" in p.follow_status.text()
    assert not gui.w.sliders["shoulder_pan"].isEnabled() and gui.w.sliders["elbow_flex"].isEnabled()
    p.follow_target.setValue(-20.0)  # manual angle changes in Simulation Only
    gui.pump(40)
    assert gui.rig.snap().extra["follow"]["target_deg"] == -20.0
    assert "Facing -20°" in p.follow_status.text()
    p.talk_button.click()
    gui.pump(10)
    assert gui.rig.snap().extra["talk"]["active"] and "talking" in p.talk_indicator.text()
    assert not gui.w.sliders["gripper"].isEnabled()
    assert "Front (angle 0)" in gui.w.front_label.text() and "Reference:" in gui.w.follow_details.text()
    p.stop_follow_button.click()
    p.talk_button.click()
    gui.pump(40)
    assert not gui.rig.snap().extra["follow"]["enabled"] and not gui.rig.snap().extra["talk"]["active"]
    assert gui.w.sliders["shoulder_pan"].isEnabled() and gui.w.sliders["gripper"].isEnabled()
    # on the real arm the operator confirms before an overlay starts moving it
    control_ready(gui)
    gui.pump()
    p.follow_button.click()
    gui.pump(5)
    assert gui.confirmer.asked[-1] == "overlay:Follow Me" and gui.rig.snap().extra["follow"]["enabled"]
    gui.w.set_front_button.click()
    gui.pump()
    assert gui.rig.snap().extra["follow"]["front"]["set"]

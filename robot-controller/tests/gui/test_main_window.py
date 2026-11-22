"""P3 GUI tests (T-018..T-023, A02/A10/A12/A13/A15/A16): offscreen Qt, fake frames, fake robot."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from helpers import FAKE_PORT, HW_POSE, make_rig
from so101_companion.config import AppConfig
from so101_companion.devices.fake import FaultPlan
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Action, Connection, Mode

pytestmark = pytest.mark.gui


class FakeCamera:
    azimuth = 160.0
    elevation = -20.0
    distance = 1.0


class FakeFrameSource:
    def __init__(self) -> None:
        self.camera = FakeCamera()
        self.closed = False

    def render(self, data: Any) -> np.ndarray:
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        frame[..., 0] = int(abs(float(data.qpos[0])) * 100) % 256
        return frame

    def info(self) -> dict[str, Any]:
        return {"mujoco_gl": "fake", "render_size": "64x48", "mujoco_version": "fake"}

    def close(self) -> None:
        self.closed = True


class ScriptedConfirmer:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.export_path: str | None = None
        self.mapping_path: str | None = None
        self.infos: list[str] = []
        self.details: tuple[str, str] | None = ("take", "recorded in a GUI test")
        self.play_texts: list[str] = []
        self.mode_texts: list[str] = []

    def _ask(self, what: str) -> bool:
        self.asked.append(what)
        return self.answer

    def confirm_connect(self, port: str, calibration: str) -> bool:
        return self._ask("connect")

    def confirm_enable_movement(self) -> bool:
        return self._ask("enable")

    def confirm_torque_off(self) -> bool:
        return self._ask("torque_off")

    def confirm_record_rest_pose(self) -> bool:
        return self._ask("record_rest")

    def confirm_enable_torque(self) -> bool:
        return self._ask("enable_torque")

    def confirm_recover(self, code: str, details: str) -> bool:
        return self._ask("recover")

    def confirm_disconnect(self, disable_torque_on_disconnect: bool) -> bool:
        return self._ask(f"disconnect:{disable_torque_on_disconnect}")

    def confirm_quit_while_connected(self, disable_torque_on_disconnect: bool) -> bool:
        return self._ask("quit")

    def confirm_adopt_rest_pose(self, text: str) -> bool:
        return self._ask("adopt_rest")

    def confirm_play_behavior(self, text: str) -> bool:
        self.play_texts.append(text)
        return self._ask("play")

    def confirm_delete_behavior(self, name: str) -> bool:
        return self._ask("delete")

    def confirm_mode_change(self, title: str, text: str) -> bool:
        self.mode_texts.append(text)
        return self._ask(f"mode:{title}")

    def confirm_go_rest(self, text: str) -> bool:
        return self._ask("go_rest")

    def confirm_move_to_pose(self, text: str) -> bool:
        return self._ask("move_pose")

    def confirm_overlay(self, title: str, text: str) -> bool:
        return self._ask("overlay:" + title)

    def ask_behavior_details(self, default_name: str) -> tuple[str, str] | None:
        self.asked.append("details")
        return self.details

    def choose_calibration_file(self, start_dir: str) -> str | None:
        return None

    def choose_export_path(self) -> str | None:
        return self.export_path

    def choose_mapping_path(self, default: str) -> str | None:
        return self.mapping_path

    def info(self, title: str, text: str) -> None:
        self.infos.append(title)

    def error(self, title: str, text: str) -> None:
        self.errors.append((title, text))


class Gui:
    def __init__(self, qapp: Any, window: Any, rig: Any, confirmer: ScriptedConfirmer) -> None:
        self.qapp, self.w, self.rig, self.confirmer = qapp, window, rig, confirmer

    def pump(self, ticks: int = 1) -> None:
        """Deterministic controller step (no worker thread) + UI refresh."""
        self.rig.coordinator.process_requests()
        for _ in range(ticks):
            self.rig.tick()
        self.w.apply_snapshot(self.rig.snap())
        self.qapp.processEvents()

    def click(self, action: Action) -> None:
        btn = self.w.action_buttons[action]
        assert btn.isEnabled(), f"{action} button disabled"
        btn.click()
        self.pump()

    def select_mode(self, mode: Mode) -> None:
        """Pick ``mode`` in the Studio dropdown (one confirmed workflow) and run the controller."""
        self.w.studio.select_mode(mode)
        self.pump()


@pytest.fixture
def gui(qapp, pose_scene, calibration_file: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    from so101_companion.simulation.mapping import mapping_from_config
    from so101_companion.ui.main_window import MainWindow

    cfg = AppConfig()
    rig = make_rig(calibration_file)
    mapping = mapping_from_config(pose_scene.model_ranges_rad(), cfg.simulation)
    confirmer = ScriptedConfirmer()
    w = MainWindow(
        pose_scene,
        mapping,
        rig.coordinator,
        FakeFrameSource,
        cfg,
        confirmer=confirmer,
        start_worker=False,
        behaviors_dir=tmp_path / "behaviors",
    )
    w.port_edit.setText(FAKE_PORT)
    w.cal_edit.setText(str(calibration_file))
    w.show()
    qapp.processEvents()
    g = Gui(qapp, w, rig, confirmer)
    yield g
    confirmer.answer = True
    w.close()
    qapp.processEvents()


def visible_top_levels(qapp: Any) -> list[Any]:
    return [x for x in qapp.topLevelWidgets() if x.isVisible()]


# ----------------------------------------------------------------------- structure (A02, A15)
def test_single_window_contains_everything(gui: Gui) -> None:
    from so101_companion.ui.viewport import MujocoViewport

    w = gui.w
    assert visible_top_levels(gui.qapp) == [w]
    assert w.findChild(MujocoViewport, "mujocoViewport").window() is w
    assert tuple(w.sliders) == JOINT_NAMES
    headers = [w.table.horizontalHeaderItem(i).text() for i in range(w.table.columnCount())]
    assert headers[:5] == ["REQUESTED", "STAGED", "SENT", "ACTUAL", "DISPLAYED"]
    assert w.findChild(type(w.log_view), "eventLog") is w.log_view
    assert "Emergency Hold" in w.hold_button.text()
    assert w.windowTitle() == "SO-101 Studio"
    # engineering controls live in ONE collapsed Advanced section; the everyday controls are visible
    assert not w.advanced.expanded and not w.sliders_box.isVisibleTo(w) and not w.table.isVisibleTo(w)
    assert w.studio.isVisibleTo(w) and w.hold_button.isVisibleTo(w) and w.studio.mode_combo.isVisibleTo(w)
    for widget in (w.sliders_box, w.table, w.log_view, w.port_edit, *w.action_buttons.values()):
        assert w.advanced.body.isAncestorOf(widget)
    w.advanced.set_expanded(True)
    assert w.sliders_box.isVisibleTo(w) and w.table.isVisibleTo(w)
    left, right = w.splitter.sizes()
    assert left > 1.8 * right  # large 3D viewport, compact panel


def test_three_distinct_stop_controls(gui: Gui) -> None:
    w = gui.w
    assert "Go Rest" in w.stop_button.text()
    assert w.stop_button.isEnabled()  # Simulation Only: Go Rest moves the SIMULATED arm
    assert "Emergency Hold" in w.hold_button.text() and not w.hold_button.isEnabled()  # no arm
    assert w.action_buttons[Action.TORQUE_OFF].text().startswith("Torque OFF")
    assert "Three distinct stops" in w.findChild(type(w.message_label), "restNote").text()
    assert "none recorded" in w.rest_label.text()


def test_startup_permissions(gui: Gui) -> None:
    w = gui.w
    enabled = {a for a, b in w.action_buttons.items() if b.isEnabled()}
    assert enabled == {Action.CONNECT}
    assert not w.hold_button.isEnabled()
    assert w.studio.mode_combo.currentData() == Mode.SIM_ONLY and w.studio.mode_combo.isEnabled()
    assert all(s.isEnabled() for s in w.sliders.values())
    assert w.table.item(0, 2).text() == "N/A" and w.table.item(0, 3).text() == "N/A"  # sent/actual unknown


def test_slider_moves_sim_model_through_controller(gui: Gui) -> None:
    w = gui.w
    before = gui.w.scene.logical_pose(w.mapping)["elbow_flex"]
    w.sliders["elbow_flex"].setValue(round(30.0 * 10))
    gui.pump(5)
    after = gui.w.scene.logical_pose(w.mapping)["elbow_flex"]
    assert before == pytest.approx(0.0, abs=1e-6) and after == pytest.approx(30.0, abs=1e-6)
    assert gui.rig.robots == []
    assert w.sliders["elbow_flex"].maximum() / 10 <= 95.0 + 0.1  # sim bounds incl. 5° margin


def test_connect_declined_touches_nothing(gui: Gui) -> None:
    gui.confirmer.answer = False
    gui.click(Action.CONNECT)
    assert gui.confirmer.asked == ["connect"] and gui.rig.robots == []


def test_invalid_calibration_or_port_shows_error_without_prompt(gui: Gui, tmp_path: Path) -> None:
    gui.w.cal_edit.setText(str(tmp_path / "missing.json"))
    gui.click(Action.CONNECT)
    gui.w.cal_edit.setText(str(gui.rig.calibration))
    gui.w.port_edit.setText("")
    gui.click(Action.CONNECT)
    assert [t for t, _ in gui.confirmer.errors] == ["Calibration invalid", "Port required"]
    assert gui.confirmer.asked == [] and gui.rig.robots == []


def test_full_operator_flow_connect_mirror_arm_hold(gui: Gui) -> None:
    w = gui.w
    gui.click(Action.CONNECT)
    snap = w.last_snapshot
    assert snap.connection == Connection.CONNECTED_READ_ONLY and snap.mode == Mode.SIM_ONLY
    assert not w.action_buttons[Action.ENABLE_MOVEMENT].isEnabled()
    assert not w.port_edit.isEnabled()
    # mirror (one confirmed workflow: torque OFF so the arm can be moved by hand)
    gui.select_mode(Mode.REAL_TO_SIM)
    assert gui.confirmer.asked[-1] == "mode:Real → Simulation"
    assert "support it" in gui.confirmer.mode_texts[-1]
    assert w.last_snapshot.torque.value == "OFF"
    gui.rig.robot.move_by_hand(elbow_flex=44.0)
    gui.pump(2)
    assert not any(s.isEnabled() for s in w.sliders.values())  # display-only sliders
    assert w.sliders["elbow_flex"].value() == 440
    assert gui.w.scene.logical_pose(w.mapping)["elbow_flex"] == pytest.approx(44.0, abs=1e-6)
    assert gui.rig.robot.count("send") == 0
    # control (one confirmed workflow: fresh read, torque on holding present, movement enabled)
    gui.select_mode(Mode.SIM_TO_REAL)
    assert "ENERGIZED" in gui.confirmer.mode_texts[-1]
    assert w.last_snapshot.connection == Connection.ARMED
    assert all(s.isEnabled() for s in w.sliders.values())
    sends_before = gui.rig.robot.count("send")
    w.sliders["elbow_flex"].setValue(round(80.0 * 10))
    gui.pump(1)
    staged_text = w.table.item(JOINT_NAMES.index("elbow_flex"), 1).text()
    assert "+80.0°" in staged_text  # fixed policy: forwarded directly, no step limiter
    sent_text = w.table.item(JOINT_NAMES.index("elbow_flex"), 2).text()
    assert "+80.0°" in sent_text  # SENT == STAGED (max_relative_target=None)
    assert gui.rig.robot.count("send") == sends_before + 1
    w.hold_button.click()
    gui.pump(3)
    assert w.last_snapshot.connection == Connection.HOLDING
    assert gui.rig.robot.count("send") == sends_before + 2  # only the hold target was added


def test_fault_banner_and_permissions(gui: Gui, calibration_file: Path) -> None:
    gui.click(Action.CONNECT)
    gui.select_mode(Mode.REAL_TO_SIM)
    gui.rig.robot.faults.unplugged = True
    gui.pump(2)
    w = gui.w
    assert w.fault_banner.isVisibleTo(w) and "READ_FAILED" in w.studio.fault_text.text()
    assert "Fault" in w.studio.status_pill.text()
    assert "STALE" in w.table.item(0, 3).text()
    enabled = {a for a, b in w.action_buttons.items() if b.isEnabled()}
    assert enabled == {Action.TORQUE_OFF, Action.RECOVER, Action.DISCONNECT}
    assert w.hold_button.isEnabled()  # best-effort hold attempt stays available
    gui.confirmer.answer = False
    w.action_buttons[Action.RECOVER].click()
    gui.pump()
    assert w.last_snapshot.connection == Connection.FAULT_LATCHED
    gui.rig.robot.faults.unplugged = False
    gui.confirmer.answer = True
    gui.click(Action.RECOVER)
    assert w.last_snapshot.connection == Connection.CONNECTED_READ_ONLY
    assert not w.fault_banner.isVisibleTo(w)


def test_torque_off_requires_confirmation(gui: Gui) -> None:
    gui.click(Action.CONNECT)
    gui.confirmer.answer = False
    gui.click(Action.TORQUE_OFF)
    assert gui.rig.robot.count("torque_off") == 0
    gui.confirmer.answer = True
    gui.click(Action.TORQUE_OFF)
    assert gui.rig.robot.count("torque_off") == 1
    assert gui.w.last_snapshot.connection == Connection.TORQUE_OFF
    assert gui.w.action_buttons[Action.ENABLE_TORQUE].isEnabled()


def test_disconnect_and_quit_warn_about_torque(gui: Gui) -> None:
    gui.click(Action.CONNECT)
    gui.click(Action.DISCONNECT)
    assert gui.confirmer.asked[-1] == "disconnect:True"
    assert gui.w.last_snapshot.connection == Connection.DISCONNECTED
    gui.click(Action.CONNECT)
    gui.confirmer.answer = False
    gui.w.close()
    assert gui.w.isVisible() and gui.confirmer.asked[-1] == "quit"  # quit cancelled, still connected
    gui.confirmer.answer = True
    gui.w.close()
    assert gui.rig.robot.count("disconnect") == 1


def test_log_view_and_export(gui: Gui, tmp_path: Path) -> None:
    gui.click(Action.CONNECT)
    text = gui.w.log_view.toPlainText()
    assert "hardware_connected" in text and "control_tick" not in text
    gui.confirmer.export_path = str(tmp_path / "events.jsonl")
    from PySide6.QtWidgets import QPushButton

    gui.w.findChild(QPushButton, "exportButton").click()
    assert (tmp_path / "events.jsonl").read_text().count("\n") > 3


def test_timing_panel_reports_rates(gui: Gui) -> None:
    gui.pump(10)
    text = gui.w.timing_labels["rates"].text()
    assert "Hz achieved" in text and "nominal 20 Hz" in text


# ----------------------------------------------------------------------- responsiveness (A16, UX-03)
def test_ui_stays_responsive_during_1s_serial_stall(qapp, pose_scene, calibration_file: Path) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtCore import QTimer

    from so101_companion.control import requests as rq
    from so101_companion.simulation.mapping import mapping_from_config
    from so101_companion.ui.main_window import MainWindow

    cfg = AppConfig()
    rig = make_rig(
        calibration_file,
        robot_kwargs={"faults": FaultPlan(read_latency_s=1.0), "sleep": time.sleep, "clock": time.monotonic},
    )
    rig.coordinator.clock = time.monotonic
    mapping = mapping_from_config(pose_scene.model_ranges_rad(), cfg.simulation)
    w = MainWindow(
        pose_scene,
        mapping,
        rig.coordinator,
        FakeFrameSource,
        cfg,
        confirmer=ScriptedConfirmer(),
        start_worker=True,
    )
    w.show()
    try:
        rig.coordinator.submit(rq.Connect(FAKE_PORT, str(calibration_file), confirmed=True))
        rig.coordinator.submit(rq.SetMode(Mode.REAL_TO_SIM))
        beats: list[float] = []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start()
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.005)
        timer.stop()
        gaps = np.diff(beats)
        assert len(beats) > 60 and gaps.max() < 0.25  # the GUI never blocked on the 1 s serial reads
        assert rig.robot.count("observe") >= 1
        assert w.viewport.stats.frames_total > 10  # rendering continued
    finally:
        w.close()
        qapp.processEvents()


def test_render_failure_shown_inside_window(qapp, pose_scene) -> None:  # type: ignore[no-untyped-def]
    from so101_companion.simulation.mapping import mapping_from_config
    from so101_companion.ui.main_window import MainWindow

    class Broken(FakeFrameSource):
        def render(self, data: Any) -> np.ndarray:
            raise RuntimeError("simulated EGL failure")

    cfg = AppConfig()
    rig = make_rig()
    mapping = mapping_from_config(pose_scene.model_ranges_rad(), cfg.simulation)
    w = MainWindow(
        pose_scene, mapping, rig.coordinator, Broken, cfg, confirmer=ScriptedConfirmer(), start_worker=False
    )
    w.show()
    try:
        assert not w.viewport.render_once()
        w.apply_snapshot(rig.snap())
        assert "FAILED" in w.timing_labels["render"].text()
        assert "simulated EGL failure" in rig.coordinator.events.last("render_fault")["error"]
        assert visible_top_levels(qapp) == [w]
    finally:
        w.close()


def test_hw_pose_constant_sane() -> None:
    assert set(HW_POSE) == set(JOINT_NAMES)


def test_display_mapping_panel_changes_only_the_3d_view_and_saves(gui: Gui, tmp_path: Path) -> None:
    from so101_companion.config import load_config

    w = gui.w
    gui.pump(2)
    before_q = w.scene.model_joint_rad()["wrist_flex"]
    before_cmd = dict(w.last_snapshot.requested.values)
    w.offset_boxes["wrist_flex"].setValue(30.0)
    assert w.scene.model_joint_rad()["wrist_flex"] == pytest.approx(before_q + np.radians(30.0), abs=1e-6)
    gui.pump(1)
    assert dict(w.last_snapshot.requested.values) == before_cmd  # commands untouched
    w.reverse_boxes["elbow_flex"].setChecked(True)
    assert w.mapping.sign["elbow_flex"] == -1.0
    gui.confirmer.mapping_path = str(tmp_path / "mine.toml")
    from PySide6.QtWidgets import QPushButton

    w.findChild(QPushButton, "saveMappingButton").click()
    sim = load_config(tmp_path / "mine.toml").simulation
    assert sim.joint_offset_deg["wrist_flex"] == 30.0 and sim.joint_sign["elbow_flex"] == -1.0
    assert gui.confirmer.infos == ["Mapping saved"]
    assert gui.rig.coordinator.events.last("display_mapping_saved") is not None


def test_record_rest_then_stop_button_returns_and_holds(gui: Gui) -> None:
    w = gui.w
    gui.click(Action.CONNECT)
    gui.select_mode(Mode.SIM_TO_REAL)
    assert w.last_snapshot.connection == Connection.ARMED
    gui.click(Action.RECORD_REST_POSE)
    assert "record_rest" in gui.confirmer.asked and "Rest pose (" in w.rest_label.text()
    w.sliders["shoulder_pan"].setValue(round(40.0 * 10))
    gui.pump(1)
    assert w.stop_button.isEnabled()
    w.stop_button.click()
    gui.pump(1)
    assert w.last_snapshot.connection == Connection.RETURNING_TO_REST
    assert "Resting" in w.stop_button.text() and "go_rest" not in gui.confirmer.asked  # interrupts motion
    for _ in range(100):
        gui.pump(1)
        if w.last_snapshot.connection != Connection.RETURNING_TO_REST:
            break
    assert w.last_snapshot.connection == Connection.HOLDING
    assert "Go Rest" in w.stop_button.text()
    w.stop_button.click()  # from a still arm: asks first
    gui.pump()
    assert gui.confirmer.asked[-1] == "go_rest"

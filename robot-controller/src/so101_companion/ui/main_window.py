"""The single integrated main window — SO-101 Studio (FR-02, UX-01..06; Studio redesign 2026-11-07).

Left: the large embedded MuJoCo viewport (with a mode badge). Right: the Studio panel (mode dropdown,
Record / Play / Pause / Go Rest, Emergency Hold, behavior library, speeds, timeline) and ONE collapsed
"Advanced settings & diagnostics" section holding every engineering control: connection, hardware
authority and torque, rest pose, joint sliders, 3D display mapping, telemetry table (requested / staged /
sent / actual / displayed), timing and the event log. Bottom: a one-line status bar.

Widgets never call the robot: they post requests / slider intents to the controller thread and render
immutable snapshots. All buttons are enabled strictly from ``Snapshot.available_actions``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from so101_companion.behaviors.poses import PoseRepository
from so101_companion.behaviors.repository import BehaviorRepository
from so101_companion.behaviors.service import BehaviorService
from so101_companion.config import AppConfig, ConfigError, SimulationSection, write_simulation_section
from so101_companion.control import requests as rq
from so101_companion.control.coordinator import ControllerCoordinator
from so101_companion.control.worker import ControllerWorker
from so101_companion.devices.calibration import CalibrationError, default_calibration_hint, load_calibration
from so101_companion.domain.joints import ARM_JOINTS, JOINT_NAMES, format_value, unit_label
from so101_companion.domain.state import Action, Connection, Mode, Snapshot, StageValue
from so101_companion.safety import policy
from so101_companion.simulation.mapping import JointMapping
from so101_companion.simulation.mujoco_pose import PoseScene
from so101_companion.telemetry.events import TICK_KIND
from so101_companion.ui import theme
from so101_companion.ui.dialogs import ADOPT_REST_TEXT, Confirmer
from so101_companion.ui.overlay import OverlayConfirmer
from so101_companion.ui.studio_panel import StudioPanel
from so101_companion.ui.viewport import MujocoViewport, RendererFactory
from so101_companion.ui.widgets import CollapsibleSection, label

STEPS_PER_UNIT = 10  # 0.1° / 0.1 % slider resolution
STAGE_COLUMNS = ("requested", "staged", "sent", "actual", "displayed")
REST_NOTE = (
    "Three distinct stops: STOP returns smoothly to the recorded rest pose and holds it (torque on); "
    "EMERGENCY HOLD freezes at the measured pose; Torque OFF lets the arm go limp. None of them is a "
    "physical emergency stop."
)


class SnapshotBridge(QObject):
    snapshot = Signal(object)
    event_record = Signal(object)  # NOT "event": that would shadow QObject.event()


class MainWindow(QMainWindow):
    def __init__(
        self,
        scene: PoseScene,
        mapping: JointMapping,
        coordinator: ControllerCoordinator,
        renderer_factory: RendererFactory,
        cfg: AppConfig,
        *,
        confirmer: Confirmer | None = None,
        build_info: dict[str, Any] | None = None,
        start_worker: bool = True,
        behaviors_dir: Path | None = None,
        poses_dir: Path | None = None,
        directions: Any = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("SO-101 Studio")
        self.setStyleSheet(theme.STYLESHEET)
        self.scene = scene
        self.mapping = mapping
        self.coordinator = coordinator
        self.cfg = cfg
        self.build_info = dict(build_info or {})
        self.confirmer: Confirmer = confirmer or OverlayConfirmer(self)
        self.last_snapshot: Snapshot | None = None
        self._seen_epoch = -1
        self._syncing = False
        self.display_reasons: list[str] = []
        self.preview_pose: dict[str, float] | None = None  # M2 preview owns the 3D view while set
        from so101_companion.simulation.model_assets import project_root

        behaviors = behaviors_dir or project_root() / "behaviors"
        self.behavior_service = BehaviorService(
            coordinator,
            BehaviorRepository(behaviors),
            poses=PoseRepository(poses_dir or behaviors.parent / "poses"),  # named poses (ADR-017)
            directions=directions,  # Follow Me direction metadata (ADR-019)
        )

        self.viewport = MujocoViewport(scene.data, renderer_factory, target_fps=cfg.app.render_target_fps)
        self.viewport.render_failed.connect(lambda msg: coordinator.events.emit("render_fault", error=msg))
        if self.viewport.error is not None:  # renderer construction already failed
            coordinator.events.emit("render_fault", error=self.viewport.error)
        camera = getattr(self.viewport.renderer, "camera", None)
        if camera is not None and hasattr(camera, "distance"):
            camera.distance = float(camera.distance) * 1.3  # studio framing: the whole arm with headroom
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.viewport)
        self.panel_scroll = QScrollArea()
        self.panel_scroll.setWidgetResizable(True)
        # Never let long fault texts widen the panel beyond the window (text wraps instead).
        self.panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.panel_scroll.setWidget(self._build_panel())
        self.panel_scroll.setMinimumWidth(500)
        splitter.addWidget(self.panel_scroll)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)
        root = QWidget()
        root.setObjectName("studioRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(splitter, 1)
        root_layout.addWidget(self._build_status_bar())
        self.setCentralWidget(root)
        self.resize(1600, 960)
        splitter.setSizes([1050, 550])
        self.splitter = splitter

        self.bridge = SnapshotBridge()
        self.bridge.snapshot.connect(self.apply_snapshot, Qt.ConnectionType.QueuedConnection)
        self.bridge.event_record.connect(self._append_event, Qt.ConnectionType.QueuedConnection)
        coordinator.events.subscribe(self._on_event_from_any_thread)
        self.worker: ControllerWorker | None = None
        if start_worker:
            self.worker = ControllerWorker(coordinator, self.bridge.snapshot.emit, cfg.app.command_hz)
            self.worker.start()
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(250)
        self._ui_timer.timeout.connect(self._refresh_timing)
        self._ui_timer.start()
        for record in coordinator.events.records(exclude_ticks=True):
            self._append_event(record)
        self.apply_snapshot(coordinator.snapshot())

    # ===================================================================== construction
    def _build_panel(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        self.studio = StudioPanel(
            self.behavior_service,
            confirmer=self.confirmer,
            show_preview_pose=self.set_preview_pose,
            model_range_logical=self._model_range_logical,
            full_turn=self.mapping.full_turn,
            current_slider_pose=self._current_slider_values,
            connection_settings=lambda: (self.port_edit.text().strip(), self.cal_edit.text().strip()),
            reveal_sliders=self.reveal_sliders,
            open_diagnostics=self.open_diagnostics,
        )
        outer.addWidget(self.studio)
        # everyday aliases (the controls live in the Studio panel)
        self.hold_button = self.studio.hold_button
        self.stop_button = self.studio.rest_button
        self.fault_banner = self.studio.fault_banner
        self.divergence_label = self.studio.warning_label

        self.advanced = CollapsibleSection(
            "Advanced settings & diagnostics",
            "Connection, torque, rest pose, joint sliders, telemetry, event log",
            name="advancedSection",
        )
        adv = QWidget()
        layout = QVBoxLayout(adv)
        layout.setContentsMargins(14, 0, 14, 14)
        self.advanced.body_layout.addWidget(adv)
        outer.addWidget(self.advanced)
        outer.addStretch(1)

        self.banner = QLabel()
        self.banner.setObjectName("statusBanner")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)
        self.banner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.message_label = QLabel()
        self.message_label.setObjectName("controllerMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.message_label)

        # -- hardware -------------------------------------------------------------------------
        hw_box = QGroupBox("Connection && hardware (explicit, confirmed actions only)")
        self.hw_box = hw_box
        hw = QGridLayout(hw_box)
        self.port_edit = QLineEdit(self.cfg.hardware.port)
        self.port_edit.setObjectName("portEdit")
        self.port_edit.setPlaceholderText("/dev/serial/by-id/usb-…  (select the servo bus explicitly)")
        self.cal_edit = QLineEdit(self.cfg.hardware.calibration_path)
        self.cal_edit.setObjectName("calibrationEdit")
        self.cal_edit.setPlaceholderText(
            f"existing LeRobot calibration, e.g. {default_calibration_hint()}/<id>.json"
        )
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse_calibration)
        hw.addWidget(QLabel("Port"), 0, 0)
        hw.addWidget(self.port_edit, 0, 1, 1, 3)
        hw.addWidget(QLabel("Calibration"), 1, 0)
        hw.addWidget(self.cal_edit, 1, 1, 1, 2)
        hw.addWidget(browse, 1, 3)
        self.action_buttons: dict[Action, QPushButton] = {}
        specs: list[tuple[Action, str, Callable[[], None]]] = [
            (Action.CONNECT, "Connect…", self._on_connect_clicked),
            (Action.ENABLE_MOVEMENT, "Enable movement…", self._on_enable_clicked),
            (Action.DISARM, "Disarm", lambda: self.coordinator.submit(rq.Disarm())),
            (Action.TORQUE_OFF, "Torque OFF…", self._on_torque_off_clicked),
            (Action.ENABLE_TORQUE, "Enable torque…", self._on_enable_torque_clicked),
            (Action.RECOVER, "Recover fault…", self._on_recover_clicked),
            (Action.DISCONNECT, "Disconnect…", self._on_disconnect_clicked),
        ]
        for i, (action, text, fn) in enumerate(specs):
            btn = QPushButton(text)
            btn.setObjectName(f"btn_{action.value}")
            btn.clicked.connect(fn)
            hw.addWidget(btn, 2 + i // 3, i % 3)
            self.action_buttons[action] = btn
        self.pause_button = QPushButton("Freeze simulation")
        self.pause_button.setObjectName("pauseButton")
        self.pause_button.setToolTip("Simulation Only: freeze / unfreeze the simulated arm's targets")
        self.pause_button.clicked.connect(self._on_pause_clicked)
        hw.addWidget(self.pause_button, 2 + len(specs) // 3, len(specs) % 3)
        layout.addWidget(hw_box)

        # -- rest pose ------------------------------------------------------------------------
        rest_box = QGroupBox("Rest pose (Go Rest target, per calibration)")
        rest_grid = QGridLayout(rest_box)
        for col, (action, text, fn) in enumerate(
            (
                (Action.RECORD_REST_POSE, "Record rest pose…", self._on_record_rest_clicked),
                (Action.ADOPT_REST_POSE, "Adopt rest pose…", self._on_adopt_rest_clicked),
            )
        ):
            btn = QPushButton(text)
            btn.setObjectName(f"btn_{action.value}")
            btn.clicked.connect(fn)
            rest_grid.addWidget(btn, 0, col)
            self.action_buttons[action] = btn
        rest_note = QLabel(REST_NOTE)
        rest_note.setObjectName("restNote")
        rest_note.setWordWrap(True)
        rest_note.setProperty("role", "muted")
        self.rest_label = QLabel("Rest pose: none recorded")
        self.rest_label.setObjectName("restPoseStatus")
        self.rest_label.setWordWrap(True)
        rest_grid.addWidget(self.rest_label, 1, 0, 1, 2)
        rest_grid.addWidget(rest_note, 2, 0, 1, 2)
        layout.addWidget(rest_box)

        # -- sliders --------------------------------------------------------------------------
        self.sliders_box = QGroupBox("Joint targets")
        grid = QGridLayout(self.sliders_box)
        self.sliders: dict[str, QSlider] = {}
        self.value_labels: dict[str, QLabel] = {}
        for row, name in enumerate(JOINT_NAMES):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setObjectName(f"slider_{name}")
            slider.valueChanged.connect(self._on_slider_changed)
            label = QLabel()
            label.setMinimumWidth(80)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(QLabel(f"{name} [{unit_label(name).strip()}]"), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(label, row, 2)
            self.sliders[name] = slider
            self.value_labels[name] = label
        self.bounds_label = QLabel()
        self.bounds_label.setWordWrap(True)
        grid.addWidget(self.bounds_label, len(JOINT_NAMES), 0, 1, 3)
        layout.addWidget(self.sliders_box)
        layout.addWidget(self._build_follow_box())
        layout.addWidget(self._build_mapping_box())

        # -- telemetry ------------------------------------------------------------------------
        tel_box = QGroupBox("Telemetry — values are never substituted across stages")
        tel = QVBoxLayout(tel_box)
        self.table = QTableWidget(len(JOINT_NAMES), len(STAGE_COLUMNS) + 1)
        self.table.setObjectName("telemetryTable")
        self.table.setHorizontalHeaderLabels([c.upper() for c in STAGE_COLUMNS] + ["ALLOWED"])
        self.table.setVerticalHeaderLabels(list(JOINT_NAMES))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(len(STAGE_COLUMNS), QHeaderView.ResizeMode.ResizeToContents)
        for r in range(len(JOINT_NAMES)):
            for c in range(len(STAGE_COLUMNS) + 1):
                self.table.setItem(r, c, QTableWidgetItem("—"))
        self.table.setMinimumHeight(220)
        tel.addWidget(self.table)
        form = QFormLayout()
        self.timing_labels: dict[str, QLabel] = {}
        for key, title in (
            ("ages", "Stage timestamps"),
            ("rates", "Controller"),
            ("render", "3D render"),
            ("driver", "Driver"),
            ("model", "Model"),
        ):
            lbl = QLabel("—")
            lbl.setWordWrap(True)
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(title, lbl)
            self.timing_labels[key] = lbl
        tel.addLayout(form)
        layout.addWidget(tel_box)

        # -- log --------------------------------------------------------------------------------
        log_box = QGroupBox("Event log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("eventLog")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setMinimumHeight(140)
        log_layout.addWidget(self.log_view)
        export_row = QHBoxLayout()
        self.include_ticks = QCheckBox("include 20 Hz control ticks in export")
        self.include_ticks.setChecked(True)
        export = QPushButton("Export log…")
        export.setObjectName("exportButton")
        export.clicked.connect(self._on_export_clicked)
        export_row.addWidget(self.include_ticks)
        export_row.addStretch(1)
        export_row.addWidget(export)
        log_layout.addLayout(export_row)
        layout.addWidget(log_box)
        return panel

    def _build_status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("studioStatusBar")
        bar.setProperty("role", "statusbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 6, 16, 6)
        self.status_items: dict[str, QLabel] = {}
        for key in ("system", "robot", "connection", "calibration", "rest"):
            lbl = label("", name=f"status_{key}")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            self.status_items[key] = lbl
            row.addWidget(lbl)
            if key != "rest":
                sep = label("|", "muted")
                row.addWidget(sep)
        row.addStretch(1)
        self.status_items["rate"] = label("", "muted", name="status_rate")
        row.addWidget(self.status_items["rate"])
        return bar

    def reveal_sliders(self) -> None:
        """Open Advanced on the joint sliders (slider recording / keyframes in Simulation Only)."""
        self.advanced.set_expanded(True)
        QTimer.singleShot(0, lambda: self.panel_scroll.ensureWidgetVisible(self.sliders_box, 0, 40))

    def open_diagnostics(self) -> None:
        self.advanced.set_expanded(True)
        QTimer.singleShot(0, lambda: self.panel_scroll.ensureWidgetVisible(self.hw_box, 0, 40))

    def _build_follow_box(self) -> QGroupBox:
        """Follow Me reference and technical details (D2): which physical direction means angle 0."""
        box = QGroupBox("Follow Me && Talk Mode — reference and details")
        lay = QVBoxLayout(box)
        self.front_label = QLabel()
        self.front_label.setObjectName("frontLabel")
        self.front_label.setWordWrap(True)
        lay.addWidget(self.front_label)
        row = QHBoxLayout()
        self.set_front_button = QPushButton("Set front = current facing")
        self.set_front_button.setObjectName("setFrontButton")
        self.set_front_button.setToolTip(
            "Point the gripper at the direction that should mean 0°, then press this. Stored per calibration "
            "and model; uses the real shoulder-pan axis (38.8 mm in front of the base origin)."
        )
        self.set_front_button.clicked.connect(
            lambda: self.coordinator.submit(rq.SetFrontReference(from_current=True))
        )
        row.addWidget(self.set_front_button)
        self.front_spin = QDoubleSpinBox()
        self.front_spin.setObjectName("frontSpin")
        self.front_spin.setRange(-180.0, 180.0)
        self.front_spin.setDecimals(1)
        self.front_spin.setSuffix("°")
        row.addWidget(self.front_spin)
        apply_front = QPushButton("Apply")
        apply_front.setObjectName("applyFrontButton")
        apply_front.clicked.connect(
            lambda: self.coordinator.submit(rq.SetFrontReference(bearing_deg=float(self.front_spin.value())))
        )
        row.addWidget(apply_front)
        reset = QPushButton("Pan center")
        reset.setObjectName("resetFrontButton")
        reset.clicked.connect(lambda: self.coordinator.submit(rq.SetFrontReference(reset=True)))
        row.addWidget(reset)
        row.addStretch(1)
        lay.addLayout(row)
        self.follow_details = QLabel()
        self.follow_details.setObjectName("followDetails")
        self.follow_details.setWordWrap(True)
        self.follow_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.follow_details)
        return box

    def _refresh_follow_box(self, snap: Snapshot) -> None:
        f = snap.extra.get("follow") or {}
        t = snap.extra.get("talk") or {}
        front = f.get("front") or {}
        where = (
            f" — set by {front.get('method')} at {front.get('set_at')}"
            if front.get("set")
            else " — default (not set for this calibration)"
        )
        self.front_label.setText(
            f"Front (angle 0) = bearing {front.get('bearing_deg', 0.0):+.1f}° clockwise from the "
            "pan-travel center" + where
        )
        self.set_front_button.setEnabled(Action.SET_FRONT in snap.available_actions)
        ref = f.get("reference") or {}
        rng = f.get("reachable_range")
        ref_line = f"Reference: {ref.get('source', '—')} · {ref.get('classification', '—')}"
        if ref.get("psi_deg") is not None:
            ref_line += f" ψ {ref['psi_deg']:+.1f}°"
        if ref:
            ref_line += (
                f" · usable {ref.get('usable_fraction', 0) * 100:.0f}%"
                f" · spread p90 {ref.get('spread_p90_deg', 0):.1f}°"
            )
        reach_line = (
            f"Reachable targets for this reference: {rng['from_deg']:+.0f}° → {rng['to_deg']:+.0f}° "
            f"clockwise ({rng['span_deg']:.0f}° of pan travel)"
            if rng
            else "Reachable targets: — (no compatible reference)"
        )
        follow_line = (
            f"Follow: {f.get('state', 'OFF')} · pan target {f.get('pan_target')} · "
            f"pan command {f.get('pan_command')}"
        )
        if f.get("residual_deg"):
            follow_line += f" · residual {f['residual_deg']:+.1f}°"
        if f.get("target_updates"):
            follow_line += f" · {f['target_updates']} target updates"
        lines = [
            "Angles: clockwise seen from above (= +shoulder_pan, the robot's right); FK about the "
            "shoulder-pan axis; one fixed reference per behavior / pose (never recomputed per tick).",
            ref_line,
            reach_line,
            follow_line,
            f"Talk: {t.get('state', 'OFF')} · {t.get('intensity')} · gripper {t.get('gripper_command')} · "
            f"lease {t.get('lease_remaining_s')} s · max rhythm {t.get('max_rhythm_hz')} Hz",
        ]
        self.follow_details.setText("\n".join(lines))

    def _build_mapping_box(self) -> QGroupBox:
        box = QGroupBox("3D display mapping — affects ONLY the 3D view, never commands or limits")
        grid = QGridLayout(box)
        help_label = QLabel(
            "Use in REAL_TO_SIM (torque off, move the arm by hand): adjust until the 3D arm matches the real "
            "arm, then Save. model angle = (reverse ? −1 : +1) × measured + offset."
        )
        help_label.setWordWrap(True)
        grid.addWidget(help_label, 0, 0, 1, 4)
        self.offset_boxes: dict[str, QDoubleSpinBox] = {}
        self.reverse_boxes: dict[str, QCheckBox] = {}
        for row, name in enumerate(ARM_JOINTS, start=1):
            spin = QDoubleSpinBox()
            spin.setObjectName(f"offset_{name}")
            spin.setRange(-180.0, 180.0)
            spin.setDecimals(1)
            spin.setSingleStep(1.0)
            spin.setWrapping(True)
            spin.setSuffix(" °")
            spin.setValue(float(self.mapping.offset_deg[name]))
            spin.valueChanged.connect(self._on_mapping_changed)
            rev = QCheckBox("reverse")
            rev.setObjectName(f"reverse_{name}")
            rev.setChecked(self.mapping.sign[name] < 0)
            rev.toggled.connect(self._on_mapping_changed)
            grid.addWidget(QLabel(name + (" (full turn)" if name in self.mapping.full_turn else "")), row, 0)
            grid.addWidget(QLabel("offset"), row, 1)
            grid.addWidget(spin, row, 2)
            grid.addWidget(rev, row, 3)
            self.offset_boxes[name] = spin
            self.reverse_boxes[name] = rev
        self.gripper_reverse = QCheckBox("gripper: reverse (LeRobot 0 = model's OPEN end)")
        self.gripper_reverse.setObjectName("reverse_gripper")
        self.gripper_reverse.setChecked(self.mapping.gripper_0_maps_to == "model_max")
        self.gripper_reverse.toggled.connect(self._on_mapping_changed)
        grid.addWidget(self.gripper_reverse, len(ARM_JOINTS) + 1, 0, 1, 3)
        save = QPushButton("Save mapping…")
        save.setObjectName("saveMappingButton")
        save.clicked.connect(self._on_save_mapping)
        grid.addWidget(save, len(ARM_JOINTS) + 1, 3)
        return box

    def _on_mapping_changed(self, *_args: object) -> None:
        self.mapping = self.mapping.with_changes(
            sign={n: (-1.0 if self.reverse_boxes[n].isChecked() else 1.0) for n in ARM_JOINTS},
            offset_deg={n: float(self.offset_boxes[n].value()) for n in ARM_JOINTS},
            gripper_0_maps_to="model_max" if self.gripper_reverse.isChecked() else "model_min",
        )
        self.coordinator.events.emit(
            "display_mapping_changed",
            sign=dict(self.mapping.sign),
            offset_deg={k: round(v, 1) for k, v in self.mapping.offset_deg.items()},
            gripper_0_maps_to=self.mapping.gripper_0_maps_to,
        )
        if self.last_snapshot is not None and self.last_snapshot.displayed.values is not None:
            self.display_reasons = self.scene.set_logical_pose(
                self.last_snapshot.displayed.values, self.mapping
            )

    def _current_simulation_section(self) -> SimulationSection:
        from dataclasses import replace

        return replace(
            self.cfg.simulation,
            joint_sign=dict(self.mapping.sign),
            joint_offset_deg={k: round(v, 1) for k, v in self.mapping.offset_deg.items()},
            logical_gripper_0_maps_to="model_max"
            if self.mapping.gripper_0_maps_to == "model_max"
            else "model_min",
            full_turn_joints=tuple(sorted(self.mapping.full_turn)),
        )

    def _on_save_mapping(self) -> None:
        from pathlib import Path

        default = (
            self.cfg.source
            if self.cfg.source.endswith(".toml")
            else str(Path.home() / "so101_companion.toml")
        )
        path = self.confirmer.choose_mapping_path(default)
        if not path:
            return
        try:
            write_simulation_section(Path(path), self._current_simulation_section())
        except (OSError, ConfigError) as exc:
            self.confirmer.error("Mapping not saved", str(exc))
            return
        self.coordinator.events.emit("display_mapping_saved", path=path)
        self.confirmer.info(
            "Mapping saved", f"Saved to {path}. Start the app with --config {path} to use it."
        )

    # ===================================================================== snapshot rendering
    def apply_snapshot(self, snap: Snapshot) -> None:
        self.last_snapshot = snap
        acts = snap.available_actions
        color = {
            Connection.FAULT_LATCHED: "#7b1d1d",
            Connection.ARMED: "#7a4b00",
            Connection.HOLDING: "#5b3a7a",
            Connection.PLAYING_BEHAVIOR: "#1e6b45",
        }.get(snap.connection, "#20354a")
        self.banner.setStyleSheet(f"background:{color};color:white;padding:8px;border-radius:8px;")
        self.banner.setText(
            f"<b>Mode: {snap.mode.value}</b> · Connection: <b>{snap.connection.value}</b> · "
            f"Torque: <b>{snap.torque.value}</b> · Calibration: "
            f"{'VALID' if snap.calibration_valid else 'not validated'} · "
            f"Hardware authorized: {'yes' if snap.hardware_authorized else 'no'}"
            + (" · <b>PAUSED</b>" if snap.paused else "")
        )
        self.message_label.setText(snap.message)

        rest = snap.extra.get("rest_pose")
        progress = snap.extra.get("rest_progress")
        status = snap.extra.get("rest_status") or {}
        if rest is None and status.get("state") in {"needs_adoption", "mismatch", "rejected"}:
            self.rest_label.setText(f"Rest pose NOT usable: {status.get('detail', '')}")
        elif rest is None:
            self.rest_label.setText(
                "Rest pose: none recorded for this calibration (Record rest pose… when connected)"
            )
        else:
            pose = ", ".join(f"{k} {format_value(k, v)}" for k, v in rest["pose"].items())
            clamped = f" — clamped: {', '.join(rest['clamped_joints'])}" if rest["clamped_joints"] else ""
            self.rest_label.setText(f"Rest pose ({rest['recorded_at']}): {pose}{clamped}")
        del progress  # shown on the Go Rest button by the Studio panel
        for action, btn in self.action_buttons.items():
            btn.setEnabled(action in acts)
        hw_idle = snap.connection == Connection.DISCONNECTED
        self.port_edit.setEnabled(hw_idle)
        self.cal_edit.setEnabled(hw_idle)
        self.pause_button.setEnabled(Action.PAUSE in acts or Action.RESUME in acts)
        self.pause_button.setText("Unfreeze simulation" if Action.RESUME in acts else "Freeze simulation")

        self._sync_sliders(snap)
        self._refresh_follow_box(snap)
        self._fill_table(snap)
        if snap.connection in {Connection.PLAYING_BEHAVIOR, Connection.RETURNING_TO_REST}:
            self.preview_pose = None  # the real arm is moving: always show it (the panel cancels previews)
        if self.preview_pose is not None:
            self.display_reasons = self.scene.set_logical_pose(
                self._composed_preview(self.preview_pose), self.mapping
            )
        elif snap.displayed.values is not None:
            self.display_reasons = self.scene.set_logical_pose(snap.displayed.values, self.mapping)
        self.studio.apply_snapshot(snap)
        self._refresh_status_bar(snap)
        self._refresh_badge(snap)
        self._refresh_timing()

    def _refresh_badge(self, snap: Snapshot) -> None:
        if self.studio.preview is not None or self.studio.scrubbing:
            self.viewport.set_badge("PREVIEW — not the real arm", theme.BLUE)
        elif snap.fault is not None:
            self.viewport.set_badge("FAULT — no commands are sent", theme.RED)
        elif snap.mode == Mode.SIM_TO_REAL:
            self.viewport.set_badge("SIM → REAL — controlling the real arm", theme.GREEN)
        elif snap.mode == Mode.REAL_TO_SIM:
            self.viewport.set_badge("REAL → SIM — mirroring the real arm", theme.AMBER)
        else:
            self.viewport.set_badge("SIMULATION ONLY — the real arm is not commanded", theme.PURPLE)

    def _refresh_status_bar(self, snap: Snapshot) -> None:
        def item(ok: bool | None, text: str) -> str:
            color = theme.GREEN if ok else theme.MUTED if ok is None else theme.AMBER
            return f"<span style='color:{color}'>●</span> {text}"

        fault = snap.fault is not None
        self.status_items["system"].setText(
            f"<span style='color:{theme.RED};font-weight:700'>● Fault latched</span>"
            if fault
            else f"<span style='color:{theme.GREEN};font-weight:700'>● System ready</span>"
        )
        robot = snap.extra.get("robot") or {}
        self.status_items["robot"].setText(f"SO-101 {robot.get('robot_id', '')}".strip())
        connected = snap.connection not in {Connection.DISCONNECTED, Connection.CONNECTING}
        self.status_items["connection"].setText(
            item(True, "Connected") if connected else item(None, "Not connected")
        )
        self.status_items["calibration"].setText(
            item(True, "Calibration valid") if snap.calibration_valid else item(None, "Calibration —")
        )
        state = (snap.extra.get("rest_status") or {}).get("state")
        rest_text = {
            "loaded": item(True, "Rest pose ready"),
            "needs_adoption": item(False, "Rest pose needs adoption"),
            "mismatch": item(False, "Rest pose from another calibration"),
            "rejected": item(False, "Rest pose rejected"),
        }.get(str(state), item(None, "Rest pose not recorded"))
        self.status_items["rest"].setText(rest_text)
        self.status_items["rate"].setText(f"{snap.tick_hz_actual:.0f} Hz")

    def _model_range_logical(self) -> dict[str, tuple[float, float]]:
        return self.mapping.model_range_logical()  # the live (possibly edited) display mapping

    def set_preview_pose(self, pose: Any) -> list[str]:
        """Show a PREVIEW pose (display only, never commanded); ``None`` returns to the live view."""
        self.preview_pose = None if pose is None else dict(pose)
        snap = self.last_snapshot
        self._route_sim_preview(pose)
        values = (
            self._composed_preview(self.preview_pose)
            if self.preview_pose is not None
            else (snap.displayed.values if snap is not None else None)
        )
        if values is not None:
            self.display_reasons = self.scene.set_logical_pose(values, self.mapping)
        if self.preview_pose is not None:
            self.viewport.set_badge("PREVIEW — not the real arm", theme.BLUE)
        elif snap is not None:
            self._refresh_badge(snap)
        return self.display_reasons

    def _sim_overlays_engaged(self) -> bool:
        snap = self.last_snapshot
        return snap is not None and snap.mode == Mode.SIM_ONLY and bool(_overlay_owned(snap))

    def _route_sim_preview(self, pose: Any) -> None:
        """D6: in Simulation Only, while an overlay is engaged, the Studio's existing 3D playback feeds the
        controller, which composes Follow Me / Talk Mode exactly as on the real arm (one composer)."""
        if pose is not None and self._sim_overlays_engaged() and self.studio.preview is not None:
            self.coordinator.set_sim_preview(
                pose, self.studio.preview_direction(), paused=self.studio.preview_frozen()
            )
        else:
            self.coordinator.set_sim_preview(None)

    def _composed_preview(self, pose: dict[str, float]) -> dict[str, float]:
        """The preview pose with the overlay-owned joints taken from the controller's composed target."""
        snap = self.last_snapshot
        if snap is None or snap.mode != Mode.SIM_ONLY or snap.displayed.values is None:
            return pose
        if self.studio.preview is None:
            return pose
        out = dict(pose)
        for j in _overlay_owned(snap):
            out[j] = snap.displayed.values[j]
        return out

    def _sync_sliders(self, snap: Snapshot) -> None:
        interactive = Action.MOVE_SLIDERS in snap.available_actions
        source = snap.requested if interactive else (snap.actual if snap.actual.values else snap.requested)
        epoch_changed = snap.requested_epoch != self._seen_epoch
        self._seen_epoch = snap.requested_epoch
        owned = _overlay_owned(snap)
        self._syncing = True
        try:
            for name, slider in self.sliders.items():
                lo, hi = snap.bounds[name]
                current = source.get(name)
                if current is not None:
                    lo, hi = min(lo, current), max(hi, current)
                slider.setRange(math.floor(lo * STEPS_PER_UNIT), math.ceil(hi * STEPS_PER_UNIT))
                # an overlay owns this joint (Follow Me: pan, Talk: gripper): the slider cannot fight it
                slider.setEnabled(interactive and name not in owned)
                if current is not None and (not interactive or epoch_changed) and not slider.isSliderDown():
                    slider.setValue(round(current * STEPS_PER_UNIT))
                self.value_labels[name].setText(format_value(name, slider.value() / STEPS_PER_UNIT))
        finally:
            self._syncing = False
        self.sliders_box.setTitle("Joint targets — ACTIVE" if interactive else "Joint targets — display only")
        self.bounds_label.setText(
            (
                "Sliders command the current mode. "
                if interactive
                else "No command authority: sliders show the measured/requested pose. "
            )
            + f"Units: arm = calibrated degrees, gripper = LeRobot 0–100. Allowed ranges: {snap.bounds_label}"
        )

    def _fill_table(self, snap: Snapshot) -> None:
        stages: dict[str, StageValue] = {
            "requested": snap.requested,
            "staged": snap.staged,
            "sent": snap.sent,
            "actual": snap.actual,
            "displayed": snap.displayed,
        }
        clipped = {r.split(":")[0]: r.split(":")[1] for r in snap.clamp_reasons}
        for r, name in enumerate(JOINT_NAMES):
            for c, stage in enumerate(STAGE_COLUMNS):
                sv = stages[stage]
                text = format_value(name, sv.get(name))
                item = self._cell(r, c)
                brush = QBrush()
                font = QFont()
                if sv.values is None:
                    text = "N/A"
                    brush = QBrush(QColor("#888888"))
                elif stage == "actual" and sv.stale:
                    text += "  STALE"
                    brush = QBrush(QColor("#b03a2e"))
                    font.setItalic(True)
                elif stage == "staged" and name in clipped:
                    text += {"range_min": " [min]", "range_max": " [max]"}.get(
                        clipped[name], f" [{clipped[name]}]"
                    )
                    brush = QBrush(QColor("#b9770e"))
                item.setText(text)
                item.setForeground(brush)
                item.setFont(font)
            lo, hi = snap.bounds[name]
            self._cell(r, len(STAGE_COLUMNS)).setText(f"{lo:+.0f} … {hi:+.0f}")

    def _cell(self, row: int, col: int) -> QTableWidgetItem:
        item = self.table.item(row, col)
        assert item is not None
        return item

    def _refresh_timing(self) -> None:
        snap = self.last_snapshot
        if snap is None:
            return
        now = self.coordinator.clock()

        def age(t: float | None) -> str:
            return "never" if t is None else f"{max(0.0, now - t):.2f}s ago"

        self.timing_labels["ages"].setText(
            f"requested {age(snap.requested.t_mono)} · staged {age(snap.staged.t_mono)} · "
            f"sent {age(snap.last_success_send_at)} (cmd #{snap.command_seq}) · "
            f"actual {age(snap.last_success_read_at)}"
        )
        self.timing_labels["rates"].setText(
            f"{snap.tick_hz_actual:.1f} Hz achieved (nominal {self.cfg.app.command_hz:g} Hz) · cycle "
            f"{snap.cycle_ms:.1f} ms · skipped deadlines {snap.overruns} · tick #{snap.seq}"
        )
        stats = self.viewport.stats.summary()
        info = self.viewport.renderer_info()
        render = (
            f"FAILED: {self.viewport.error}"
            if self.viewport.error
            else f"{stats['fps']:.1f} fps · p50 {stats['render_ms_p50']:.1f} ms · "
            f"MUJOCO_GL={info.get('mujoco_gl', '?')} · {info.get('gl_renderer', '')}"
        )
        if self.display_reasons:
            render += " · display clamped: " + ", ".join(r.split(":")[0] for r in self.display_reasons)
        self.timing_labels["render"].setText(render)
        robot = snap.extra.get("robot") or {}
        self.timing_labels["driver"].setText(
            ", ".join(f"{k}={v}" for k, v in robot.items()) if robot else "no hardware driver loaded"
        )
        self.timing_labels["model"].setText(str(self.build_info.get("model", "—")))

    # ===================================================================== event log
    def _on_event_from_any_thread(self, record: dict[str, Any]) -> None:
        if record["kind"] != TICK_KIND:
            self.bridge.event_record.emit(record)

    def _append_event(self, record: dict[str, Any]) -> None:
        details = {
            k: v
            for k, v in record.items()
            if k not in ("kind", "event_seq", "t_monotonic_seconds", "wall_time")
        }
        self.log_view.appendPlainText(f"{record['t_monotonic_seconds']:.3f}  {record['kind']}  {details}")

    # ===================================================================== user actions
    def _current_slider_values(self) -> dict[str, float]:
        return {n: s.value() / STEPS_PER_UNIT for n, s in self.sliders.items()}

    def _on_slider_changed(self, _value: int) -> None:
        if self._syncing:
            return
        values = self._current_slider_values()
        for name in JOINT_NAMES:
            self.value_labels[name].setText(format_value(name, values[name]))
        snap = self.last_snapshot
        if snap is not None and Action.MOVE_SLIDERS in snap.available_actions:
            self.coordinator.set_requested(values)

    def _on_pause_clicked(self) -> None:
        snap = self.last_snapshot
        if snap is not None and Action.RESUME in snap.available_actions:
            self.coordinator.submit(rq.Resume())
        else:
            self.coordinator.submit(rq.Pause())

    def _on_browse_calibration(self) -> None:
        start = self.cal_edit.text() or str(default_calibration_hint())
        path = self.confirmer.choose_calibration_file(start)
        if path:
            self.cal_edit.setText(path)

    def _on_connect_clicked(self) -> None:
        port = self.port_edit.text().strip()
        cal = self.cal_edit.text().strip()
        if not port.startswith("/dev/"):
            self.confirmer.error(
                "Port required", "Enter the servo bus device explicitly (e.g. /dev/serial/by-id/…)."
            )
            return
        try:
            load_calibration(cal)
        except CalibrationError as exc:
            self.confirmer.error("Calibration invalid", f"{exc}\n\nNo hardware was touched.")
            return
        if self.confirmer.confirm_connect(port, cal):
            self.coordinator.submit(rq.Connect(port, cal, confirmed=True))

    def _on_enable_clicked(self) -> None:
        if self.confirmer.confirm_enable_movement():
            self.coordinator.submit(rq.EnableMovement(confirmed=True))

    def _on_record_rest_clicked(self) -> None:
        if self.confirmer.confirm_record_rest_pose():
            self.coordinator.submit(rq.RecordRestPose(confirmed=True))

    def _on_adopt_rest_clicked(self) -> None:
        snap = self.last_snapshot
        legacy = snap.extra.get("legacy_rest_pose") if snap is not None else None
        if legacy is None:
            return
        pose = ", ".join(f"{k} {format_value(k, v)}" for k, v in legacy["pose"].items())
        text = ADOPT_REST_TEXT.format(recorded_at=legacy["recorded_at"], pose=pose)
        if self.confirmer.confirm_adopt_rest_pose(text):
            self.coordinator.submit(rq.AdoptRestPose(confirmed=True))

    def _on_torque_off_clicked(self) -> None:
        if self.confirmer.confirm_torque_off():
            self.coordinator.submit(rq.TorqueOff(confirmed=True))

    def _on_enable_torque_clicked(self) -> None:
        if self.confirmer.confirm_enable_torque():
            self.coordinator.submit(rq.EnableTorque(confirmed=True))

    def _on_recover_clicked(self) -> None:
        snap = self.last_snapshot
        fault = snap.fault if snap else None
        if fault is not None and self.confirmer.confirm_recover(fault.code, fault.details):
            self.coordinator.submit(rq.Recover(confirmed=True))

    def _disable_torque_on_disconnect(self) -> bool:
        return policy.DISABLE_TORQUE_ON_DISCONNECT

    def _on_disconnect_clicked(self) -> None:
        if self.confirmer.confirm_disconnect(self._disable_torque_on_disconnect()):
            self.coordinator.submit(rq.Disconnect(confirmed=True))

    def _on_export_clicked(self) -> None:
        path = self.confirmer.choose_export_path()
        if path:
            from pathlib import Path

            n = self.coordinator.events.export_jsonl(Path(path), include_ticks=self.include_ticks.isChecked())
            self.coordinator.events.emit("log_exported", records=n)

    # ===================================================================== shutdown
    def closeEvent(self, event: QCloseEvent) -> None:
        snap = self.last_snapshot
        if snap is not None and Action.DISCONNECT in snap.available_actions:
            if not self.confirmer.confirm_quit_while_connected(self._disable_torque_on_disconnect()):
                event.ignore()
                return
            self.coordinator.submit(rq.Disconnect(confirmed=True))
            if self.worker is None:
                self.coordinator.process_requests()
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        if self.worker is not None:
            # The worker drains pending requests (e.g. the confirmed Disconnect) before exiting.
            deadline_ok = self._drain_worker()
            self.worker.stop()
            if not deadline_ok:
                self.coordinator.events.emit("shutdown_warning", detail="controller did not drain in time")
            self.worker = None
        self._ui_timer.stop()
        self.viewport.shutdown()

    def _drain_worker(self, timeout_s: float = 5.0) -> bool:
        import time as _time

        end = _time.monotonic() + timeout_s
        while self.coordinator.pending_requests() and _time.monotonic() < end:
            _time.sleep(0.01)
        return not self.coordinator.pending_requests()


def _overlay_owned(snap: Snapshot) -> set[str]:
    owned = set()
    if (snap.extra.get("follow") or {}).get("owns"):
        owned.add("shoulder_pan")
    if (snap.extra.get("talk") or {}).get("owns"):
        owned.add("gripper")
    return owned

"""SO-101 Studio behavior panel: the everyday controls of the redesigned single window.

Mode dropdown (one confirmed workflow per hardware mode) · four context-aware primary controls
(Record | Play | Pause/Resume | Go Rest) · Emergency Hold (always visible) · behavior library (select,
New, Rename, Duplicate, Delete) · playback status · Behavior speed + "Getting into position" and
"Between repetitions" presets · timeline (scrub, trim handles, playhead, repeats, keyframes).

Presentation only: it reuses the M2 ``BehaviorService`` (recording, storage, validation, preview, plan)
and submits requests to the single controller; it never talks to the robot. Every button is enabled from
``Snapshot.available_actions``; a short reason is shown next to disabled controls.
"""

from __future__ import annotations

import bisect
import time
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeyEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from so101_companion.behaviors.model import Behavior, Origin, PlaybackSettings, Sample
from so101_companion.behaviors.plan import CompatibilityError
from so101_companion.behaviors.poses import NamedPose, PoseFormatError
from so101_companion.behaviors.preview import PreviewApproval, PreviewSession
from so101_companion.behaviors.service import BehaviorService
from so101_companion.behaviors.validate import BehaviorFormatError
from so101_companion.control import requests as rq
from so101_companion.devices.calibration import CalibrationError, load_calibration
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.domain.state import Action, Connection, Mode, Snapshot, Torque
from so101_companion.safety import policy
from so101_companion.safety.policy import TransitionProfile
from so101_companion.ui import dialogs, theme
from so101_companion.ui.dialogs import Confirmer
from so101_companion.ui.widgets import StatusChips, TimelineBar, action_icon, card, label, mode_icon

MODE_ITEMS: tuple[tuple[Mode, str, str], ...] = (
    (Mode.SIM_ONLY, "Simulation Only", "pc"),
    (Mode.REAL_TO_SIM, "Real → Simulation", "robot_to_pc"),
    (Mode.SIM_TO_REAL, "Simulation → Real", "pc_to_robot"),
)
ORIGIN_LABEL = {
    Origin.REAL_HAND_GUIDED: "hand-guided",
    Origin.SIM_SLIDERS: "slider take",
    Origin.SIM_KEYFRAMES: "keyframes",
}
SPEED_STEP = 0.1
AFTER_ITEMS: tuple[tuple[str, str], ...] = (
    ("rest", "Return to default rest"),
    ("hold", "Hold last position"),
    ("pose", "Move to saved pose"),
)
ENDING_CHIP = {"rest": "Returning to rest", "hold": "Holding last position", "pose": "Moving to pose"}


STUDIO_TALK_LEASE_S = 30.0  # the panel renews every 10 s while Talk is on


class StudioPanel(QWidget):
    def __init__(
        self,
        service: BehaviorService,
        *,
        confirmer: Confirmer,
        show_preview_pose: Callable[[Mapping[str, float] | None], list[str]],
        model_range_logical: Callable[[], dict[str, tuple[float, float]]],
        full_turn: frozenset[str],
        current_slider_pose: Callable[[], dict[str, float]],
        connection_settings: Callable[[], tuple[str, str]],
        reveal_sliders: Callable[[], None] = lambda: None,
        open_diagnostics: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self.setObjectName("studioPanel")
        self.service = service
        self.coordinator = service.coordinator
        self.confirmer = confirmer
        self.show_preview_pose = show_preview_pose
        self.model_range_logical = model_range_logical
        self.full_turn = full_turn
        self.current_slider_pose = current_slider_pose
        self.connection_settings = connection_settings
        self.reveal_sliders = reveal_sliders
        self.open_diagnostics = open_diagnostics
        self.clock = clock
        self.snapshot: Snapshot | None = None
        self.behavior: Behavior | None = None
        self.preview: PreviewSession | None = None
        self.preview_reason: str | None = None
        self.preview_paused_at: float | None = None
        self._preview_started = 0.0
        self.approval: PreviewApproval | None = None
        self._approval_key: tuple[Any, ...] | None = None
        self.scrubbing = False
        self._last_display_pose: dict[str, float] | None = None
        self.notice = ""  # last short user-facing result ("Saved 'Take 13:04'", ...)
        self.poses: list[NamedPose] = []
        self._speed = 1.0
        self._trim = (0.0, 0.0)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._on_preview_timer)
        self._build()
        self.refresh_poses()  # fills the Poses dropdown AND the After-behavior pose list from disk
        self.refresh_library()

    # ================================================================ construction
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        # -- header: title + status pill
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("SO-101 Studio", "title"))
        titles.addWidget(label("Robot Animation Studio for SO-101", "subtitle"))
        head.addLayout(titles, 1)
        self.status_pill = label("", name="statusPill")
        self.status_pill.setTextFormat(Qt.TextFormat.RichText)
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.status_pill)
        lay.addLayout(head)

        # -- critical banners (always visible when active)
        self.fault_banner = card("faultBanner")
        self.fault_banner.setStyleSheet(
            f"QFrame#faultBanner{{background:#3a1212;border:1px solid {theme.RED};border-radius:12px;}}"
        )
        fb = QHBoxLayout(self.fault_banner)
        self.fault_text = label("", wrap=True, name="faultText")
        self.fault_text.setStyleSheet("font-weight:600;")
        fb.addWidget(self.fault_text, 1)
        self.diag_button = QPushButton("Open diagnostics")
        self.diag_button.setProperty("role", "link")
        self.diag_button.clicked.connect(self.open_diagnostics)
        fb.addWidget(self.diag_button)
        self.fault_banner.hide()
        lay.addWidget(self.fault_banner)
        self.warning_label = label("", wrap=True, name="divergenceWarning")
        self.warning_label.setStyleSheet(
            f"background:#3a2a0a;border:1px solid {theme.AMBER};border-radius:10px;padding:8px;"
        )
        self.warning_label.hide()
        lay.addWidget(self.warning_label)

        # -- mode dropdown
        mode_card = card()
        mc = QHBoxLayout(mode_card)
        mc.setContentsMargins(14, 10, 10, 10)
        mc.addWidget(label("Control mode", "section"))
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("modeCombo")
        self.mode_combo.setProperty("role", "mode")
        self.mode_combo.setIconSize(QSize(66, 22))
        for mode, text, icon in MODE_ITEMS:
            self.mode_combo.addItem(mode_icon(icon), text, mode)
        self.mode_combo.activated.connect(self._on_mode_activated)
        mc.addWidget(self.mode_combo, 1)
        lay.addWidget(mode_card)

        # -- primary controls
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        self.record_button = self._primary("Record", "record", "recordButton")
        self.play_button = self._primary("Play", "play", "playButton", role="play")
        self.pause_button = self._primary("Pause", "pause", "pauseButton")
        self.rest_button = self._primary("Go Rest", "home", "goRestButton", role="rest")
        for i, b in enumerate((self.record_button, self.play_button, self.pause_button, self.rest_button)):
            grid.addWidget(b, 0, i)
        self.record_button.clicked.connect(self._on_record)
        self.play_button.clicked.connect(self._on_play)
        self.pause_button.clicked.connect(self._on_pause)
        self.rest_button.clicked.connect(self._on_go_rest)
        lay.addLayout(grid)
        self.hold_button = QPushButton("  Emergency Hold")
        self.hold_button.setObjectName("holdButton")
        self.hold_button.setProperty("role", "hold")
        self.hold_button.setIcon(action_icon("warning", "#ffffff", 22))
        self.hold_button.setIconSize(QSize(22, 22))
        self.hold_button.setToolTip(
            "Freezes the arm at its measured pose (best effort — not a physical emergency stop). "
            "Cut the servo power if needed."
        )
        self.hold_button.clicked.connect(lambda: self.coordinator.submit(rq.EmergencyHold()))
        lay.addWidget(self.hold_button)
        self.reason_label = label("", "reason", name="reasonLabel", wrap=True, autohide=True)
        lay.addWidget(self.reason_label)

        # -- behavior selector
        beh_card = card()
        bc = QVBoxLayout(beh_card)
        bc.setContentsMargins(14, 10, 10, 10)
        row = QHBoxLayout()
        row.addWidget(label("Behavior", "section"))
        self.behavior_combo = QComboBox()
        self.behavior_combo.setObjectName("behaviorCombo")
        self.behavior_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.behavior_combo.currentIndexChanged.connect(self._on_select)
        row.addWidget(self.behavior_combo, 1)
        self.rename_edit = QLineEdit()
        self.rename_edit.setObjectName("renameEdit")
        self.rename_edit.setPlaceholderText("New name — Enter to save, Esc to cancel")
        self.rename_edit.returnPressed.connect(self._commit_rename)
        self.rename_edit.installEventFilter(self)
        self.rename_edit.hide()
        row.addWidget(self.rename_edit, 1)
        self.more_button = QToolButton()
        self.more_button.setObjectName("moreButton")
        self.more_button.setIcon(action_icon("more"))
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_button)
        self.act_new_keyframes = QAction("New keyframe behavior", self)
        self.act_rename = QAction("Rename", self)
        self.act_duplicate = QAction("Duplicate", self)
        self.act_save_settings = QAction("Save playback settings to behavior", self)
        self.act_delete = QAction("Delete…", self)
        for a in (self.act_new_keyframes, self.act_rename, self.act_duplicate, self.act_save_settings):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.act_delete)
        self.act_new_keyframes.triggered.connect(self._on_new_keyframes)
        self.act_rename.triggered.connect(self._start_rename)
        self.act_duplicate.triggered.connect(self._on_duplicate)
        self.act_save_settings.triggered.connect(self._on_save_settings)
        self.act_delete.triggered.connect(self._on_delete)
        self.more_button.setMenu(menu)
        row.addWidget(self.more_button)
        bc.addLayout(row)
        self.info_label = label("—", "muted", name="behaviorInfo", wrap=True)
        bc.addWidget(self.info_label)
        after_row = QHBoxLayout()
        after_row.addWidget(label("After behavior", "muted"))
        self.after_combo = QComboBox()
        self.after_combo.setObjectName("afterCombo")
        for key, text in AFTER_ITEMS:
            self.after_combo.addItem(text, key)
        self.after_combo.setToolTip(
            "What the arm does when ALL repetitions are done (saved with the behavior). Stopping early "
            "(Go Rest) always returns to the default rest pose."
        )
        self.after_combo.activated.connect(self._on_after_changed)
        after_row.addWidget(self.after_combo, 1)
        self.after_pose_combo = QComboBox()
        self.after_pose_combo.setObjectName("afterPoseCombo")
        self.after_pose_combo.activated.connect(self._on_after_changed)
        self.after_pose_combo.hide()
        after_row.addWidget(self.after_pose_combo, 1)
        bc.addLayout(after_row)
        lay.addWidget(beh_card)

        # -- saved poses (compact)
        pose_card = card("posesCard")
        pc = QHBoxLayout(pose_card)
        pc.setContentsMargins(14, 8, 10, 8)
        pc.addWidget(label("Poses", "section"))
        self.pose_combo = QComboBox()
        self.pose_combo.setObjectName("poseCombo")
        self.pose_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.pose_combo.currentIndexChanged.connect(lambda *_: self._update_enabled())
        pc.addWidget(self.pose_combo, 1)
        self.pose_rename_edit = QLineEdit()
        self.pose_rename_edit.setObjectName("poseRenameEdit")
        self.pose_rename_edit.setPlaceholderText("Pose name — Enter to save, Esc to cancel")
        self.pose_rename_edit.returnPressed.connect(self._commit_pose_rename)
        self.pose_rename_edit.installEventFilter(self)
        self.pose_rename_edit.hide()
        pc.addWidget(self.pose_rename_edit, 1)
        self.save_pose_button = QPushButton("Save current")
        self.save_pose_button.setObjectName("savePoseButton")
        self.save_pose_button.setToolTip(
            "Save the current pose: the real arm's measured joints when connected (Real → Simulation / "
            "Simulation → Real), otherwise the 3D arm's pose."
        )
        self.save_pose_button.clicked.connect(self._on_save_pose)
        pc.addWidget(self.save_pose_button)
        self.go_pose_button = QPushButton("Go")
        self.go_pose_button.setObjectName("goPoseButton")
        self.go_pose_button.setToolTip("Move smoothly to the selected pose ('Getting into position' speed)")
        self.go_pose_button.clicked.connect(self._on_go_pose)
        pc.addWidget(self.go_pose_button)
        self.pose_more = QToolButton()
        self.pose_more.setObjectName("poseMoreButton")
        self.pose_more.setIcon(action_icon("more"))
        self.pose_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        pose_menu = QMenu(self.pose_more)
        self.act_preview_pose = QAction("Preview pose in 3D", self)
        self.act_rename_pose = QAction("Rename pose", self)
        self.act_delete_pose = QAction("Delete pose…", self)
        pose_menu.addAction(self.act_preview_pose)
        pose_menu.addAction(self.act_rename_pose)
        pose_menu.addSeparator()
        pose_menu.addAction(self.act_delete_pose)
        self.act_preview_pose.triggered.connect(self._on_preview_pose)
        self.act_rename_pose.triggered.connect(self._start_pose_rename)
        self.act_delete_pose.triggered.connect(self._on_delete_pose)
        self.pose_more.setMenu(pose_menu)
        pc.addWidget(self.pose_more)
        lay.addWidget(pose_card)

        # -- Follow Me & Talk Mode (M2.6, ADR-019): two independent overlays, compact
        ov_card = card("overlayCard")
        oc = QVBoxLayout(ov_card)
        oc.setContentsMargins(14, 8, 10, 8)
        oc.setSpacing(4)
        frow = QHBoxLayout()
        self.follow_button = QPushButton("Follow Me")
        self.follow_button.setObjectName("followButton")
        self.follow_button.setCheckable(True)
        self.follow_button.setToolTip(
            "Turn the shoulder pan toward the target angle while behaviors and poses keep the other joints "
            "(clockwise = the robot's right, from the configured front — Advanced → Follow Me)."
        )
        self.follow_button.clicked.connect(self._on_follow_clicked)
        frow.addWidget(self.follow_button)
        frow.addWidget(label("Target", "muted"))
        self.follow_target = QDoubleSpinBox()
        self.follow_target.setObjectName("followTarget")
        self.follow_target.setRange(-180.0, 180.0)
        self.follow_target.setDecimals(0)
        self.follow_target.setSingleStep(5.0)
        self.follow_target.setSuffix("°")
        self.follow_target.setKeyboardTracking(False)
        self.follow_target.valueChanged.connect(self._on_follow_target)
        frow.addWidget(self.follow_target)
        self.stop_follow_button = QPushButton("Stop following")
        self.stop_follow_button.setObjectName("stopFollowButton")
        self.stop_follow_button.clicked.connect(lambda: self.coordinator.submit(rq.SetFollow(False)))
        self.stop_follow_button.hide()
        frow.addWidget(self.stop_follow_button)
        frow.addStretch(1)
        oc.addLayout(frow)
        self.follow_status = label("", "muted", name="followStatus", wrap=True, autohide=True)
        oc.addWidget(self.follow_status)
        trow = QHBoxLayout()
        self.talk_button = QPushButton("Talk")
        self.talk_button.setObjectName("talkButton")
        self.talk_button.setCheckable(True)
        self.talk_button.setToolTip("Animate the gripper like a speaking mouth (independent of the behavior)")
        self.talk_button.clicked.connect(self._on_talk_clicked)
        trow.addWidget(self.talk_button)
        self.talk_intensity = QComboBox()
        self.talk_intensity.setObjectName("talkIntensity")
        for name in ("gentle", "normal", "excited"):
            self.talk_intensity.addItem(name.capitalize(), name)
        self.talk_intensity.setCurrentIndex(1)
        self.talk_intensity.currentIndexChanged.connect(lambda *_: self._renew_talk(changed=True))
        trow.addWidget(self.talk_intensity)
        self.talk_indicator = label("", name="talkIndicator")
        trow.addWidget(self.talk_indicator, 1)
        oc.addLayout(trow)
        oc.addWidget(label("Follow Me owns the shoulder pan only · Talk owns the gripper only", "muted"))
        lay.addWidget(ov_card)
        self._talk_renew_timer = QTimer(self)
        self._talk_renew_timer.setInterval(int(STUDIO_TALK_LEASE_S * 1000 / 3))
        self._talk_renew_timer.timeout.connect(self._renew_talk)

        # -- playback status
        st_card = card()
        sc = QVBoxLayout(st_card)
        sc.setContentsMargins(14, 10, 14, 10)
        sc.setSpacing(6)
        self.status_text = label("", wrap=True, name="statusText")
        self.status_text.setStyleSheet("font-weight:600;")
        sc.addWidget(self.status_text)
        self.chips = StatusChips()
        sc.addWidget(self.chips)
        self.progress = QProgressBar()
        self.progress.setObjectName("playbackProgress")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        sc.addWidget(self.progress)
        self.finish_button = QPushButton("Finish after this loop")
        self.finish_button.setObjectName("finishButton")
        self.finish_button.setToolTip(
            "Let the current repetition complete, then run this behavior's 'After behavior' ending "
            "(hold, move to its pose, or return to rest)."
        )
        self.finish_button.clicked.connect(lambda: self.coordinator.submit(rq.FinishBehavior()))
        self.finish_button.hide()
        sc.addWidget(self.finish_button)
        lay.addWidget(st_card)

        # -- speeds
        speeds = QHBoxLayout()
        speeds.setSpacing(8)
        sp = card("speedCard")
        sp_l = QVBoxLayout(sp)
        sp_l.setContentsMargins(10, 8, 10, 8)
        sp_l.addWidget(label("Behavior speed", "muted"), alignment=Qt.AlignmentFlag.AlignHCenter)
        sp_row = QHBoxLayout()
        self.speed_minus = QPushButton("−")
        self.speed_plus = QPushButton("+")
        for b in (self.speed_minus, self.speed_plus):
            b.setFixedSize(34, 34)
        self.speed_value = label("1.0×", "big", name="speedValue")
        self.speed_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sp_row.addWidget(self.speed_minus)
        sp_row.addWidget(self.speed_value, 1)
        sp_row.addWidget(self.speed_plus)
        sp_l.addLayout(sp_row)
        self.speed_minus.clicked.connect(lambda: self.set_speed(self._speed - SPEED_STEP))
        self.speed_plus.clicked.connect(lambda: self.set_speed(self._speed + SPEED_STEP))
        speeds.addWidget(sp, 1)
        self.entry_preset = self._preset_card(speeds, "Getting into position", policy.DEFAULT_ENTRY_PROFILE)
        self.entry_preset.setObjectName("entryPreset")
        self.bridge_preset = self._preset_card(speeds, "Between repetitions", policy.DEFAULT_BRIDGE_PROFILE)
        self.bridge_preset.setObjectName("bridgePreset")
        lay.addLayout(speeds)

        # -- timeline
        tl_card = card()
        tl = QVBoxLayout(tl_card)
        tl.setContentsMargins(14, 10, 14, 10)
        head_row = QHBoxLayout()
        head_row.addWidget(label("Timeline & looping", "section"))
        head_row.addStretch(1)
        self.time_label = label("0.0 s / 0.0 s", "muted", name="timeLabel")
        head_row.addWidget(self.time_label)
        tl.addLayout(head_row)
        self.timeline = TimelineBar()
        self.timeline.scrubbed.connect(self._on_scrub)
        self.timeline.trim_changed.connect(self._on_trim)
        tl.addWidget(self.timeline)
        self.trim_label = label("", "muted", name="trimLabel", autohide=True)
        tl.addWidget(self.trim_label)
        opts = QHBoxLayout()
        opts.addWidget(label("Repeat", "muted"))
        self.repeats = QSpinBox()
        self.repeats.setObjectName("repeatSpin")
        self.repeats.setRange(1, 1000)
        self.repeats.setSuffix(" ×")
        self.repeats.valueChanged.connect(self._on_settings_changed)
        opts.addWidget(self.repeats)
        self.forever = QPushButton("Until stopped")
        self.forever.setObjectName("untilStopped")
        self.forever.setCheckable(True)
        self.forever.toggled.connect(self._on_settings_changed)
        opts.addWidget(self.forever)
        opts.addStretch(1)
        self.keyframes_toggle = QPushButton("Keyframes")
        self.keyframes_toggle.setObjectName("keyframesToggle")
        self.keyframes_toggle.setCheckable(True)
        self.keyframes_toggle.toggled.connect(self._on_keyframes_toggled)
        opts.addWidget(self.keyframes_toggle)
        tl.addLayout(opts)
        prev_row = QHBoxLayout()
        self.preview_button = QPushButton("Preview in 3D")
        self.preview_button.setObjectName("previewButton")
        self.preview_button.setToolTip(
            "Plays the exact run in the 3D view only. A complete preview authorizes ONE real run "
            "(optional, owner decision D7)."
        )
        self.preview_button.clicked.connect(self._start_preview)
        self.live_button = QPushButton("Back to live view")
        self.live_button.setProperty("role", "link")
        self.live_button.clicked.connect(self._end_scrub)
        self.live_button.hide()
        prev_row.addWidget(self.preview_button)
        prev_row.addStretch(1)
        prev_row.addWidget(self.live_button)
        tl.addLayout(prev_row)
        self.preview_label = label("", "muted", name="previewStatus", wrap=True, autohide=True)
        self.clamp_label = label("", "muted", name="clampReport", wrap=True, autohide=True)
        self.display_limit_label = label("", "reason", name="displayLimit", wrap=True, autohide=True)
        for w in (self.preview_label, self.clamp_label, self.display_limit_label):
            tl.addWidget(w)

        # keyframe editor (keyframe behaviors)
        self.kf_box = QFrame()
        self.kf_box.setObjectName("keyframeEditor")
        kf = QVBoxLayout(self.kf_box)
        kf.setContentsMargins(0, 6, 0, 0)
        self.kf_note = label(
            "Keyframes are edited on keyframe behaviors (⋯ → New keyframe behavior). Recorded takes are "
            "kept as recorded — use the trim handles.",
            "muted",
            wrap=True,
        )
        kf.addWidget(self.kf_note)
        self.kf_table = QTableWidget(0, 1 + len(JOINT_NAMES))
        self.kf_table.setObjectName("keyframeTable")
        self.kf_table.setHorizontalHeaderLabels(["t [s]", *[n.replace("_", " ") for n in JOINT_NAMES]])
        self.kf_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.kf_table.setMinimumHeight(140)
        kf.addWidget(self.kf_table)
        kf_buttons = QHBoxLayout()
        self.kf_add = QPushButton("Add keyframe (slider pose, +1 s)")
        self.kf_del = QPushButton("Delete keyframe")
        self.kf_save = QPushButton("Save keyframes")
        self.kf_add.clicked.connect(self._on_add_keyframe)
        self.kf_del.clicked.connect(self._on_delete_keyframe)
        self.kf_save.clicked.connect(self._on_save_keyframes)
        for b in (self.kf_add, self.kf_del, self.kf_save):
            kf_buttons.addWidget(b)
        kf.addLayout(kf_buttons)
        self.kf_box.hide()
        tl.addWidget(self.kf_box)
        lay.addWidget(tl_card)

    def _primary(self, text: str, icon: str, name: str, role: str = "") -> QPushButton:
        b = QPushButton(f"  {text}")
        b.setObjectName(name)
        b.setProperty("role", "primary")
        if role:
            b.setProperty("variant", role)
            b.setProperty("role", role)
            b.setStyleSheet("font-size:15px;font-weight:600;min-height:46px;")
        b.setIcon(action_icon(icon, "#ffffff" if role else theme.TEXT, 20))
        b.setIconSize(QSize(20, 20))
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return b

    def _preset_card(self, row: QHBoxLayout, title: str, default: TransitionProfile) -> QComboBox:
        c = card()
        c.setStyleSheet(f"QFrame{{border-color:{theme.GREEN_DARK};}}")
        lay = QVBoxLayout(c)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.addWidget(label(title, "muted"), alignment=Qt.AlignmentFlag.AlignHCenter)
        combo = QComboBox()
        combo.setProperty("role", "preset")
        for p in (TransitionProfile.GENTLE, TransitionProfile.NORMAL, TransitionProfile.FAST):
            combo.addItem(p.label, p)
        combo.setCurrentIndex(combo.findData(default))
        combo.setToolTip(
            "Gentle ≈ the old 30 °/s · Normal · Fast = the per-joint limits verified on the arm (ADR-015). "
            "A gesture that ends where it starts loops with no bridge at all."
        )
        combo.currentIndexChanged.connect(self._on_settings_changed)
        lay.addWidget(combo)
        row.addWidget(c, 1)
        return combo

    # ================================================================ small state helpers
    @property
    def entry_profile(self) -> TransitionProfile:
        return TransitionProfile(self.entry_preset.currentData())

    @property
    def bridge_profile(self) -> TransitionProfile:
        return TransitionProfile(self.bridge_preset.currentData())

    def set_speed(self, value: float) -> None:
        v = round(min(policy.PLAYBACK_SPEED_MAX, max(policy.PLAYBACK_SPEED_MIN, value)), 2)
        if abs(v - self._speed) < 1e-9:
            return
        self._speed = v
        self.speed_value.setText(f"{v:.1f}×")
        self._on_settings_changed()

    @property
    def speed(self) -> float:
        return self._speed

    def settings(self) -> PlaybackSettings:
        b = self.behavior
        start, end = self._trim
        if b is not None:  # an end within 1 ms of the source's end means "to the end", never past it
            end = min(end, b.duration_s)
        return PlaybackSettings(
            trim_start_s=round(start, 6),
            trim_end_s=None if b is None or b.duration_s - end < 0.001 else round(end, 6),
            speed=round(self._speed, 6),
            repeat_count=None if self.forever.isChecked() else self.repeats.value(),
            # the saved "After behavior" ending travels with every run (ADR-017)
            after_action=b.settings.after_action if b is not None else "rest",
            after_pose_id=b.settings.after_pose_id if b is not None else None,
        )

    def _current_key(self) -> tuple[Any, ...]:
        b = self.behavior
        return (
            None if b is None else b.id,
            None if b is None else b.revision,
            self.settings(),
            self.entry_profile,
            self.bridge_profile,
        )

    def _approval_valid(self) -> bool:
        return self.approval is not None and self._approval_key == self._current_key()

    def _acts(self) -> frozenset[Action]:
        return self.snapshot.available_actions if self.snapshot is not None else frozenset()

    def _mode(self) -> Mode:
        return self.snapshot.mode if self.snapshot is not None else Mode.SIM_ONLY

    def eventFilter(self, obj: Any, event: Any) -> bool:
        if obj is self.rename_edit and isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
            self._end_rename()
            return True
        pose_edit = getattr(self, "pose_rename_edit", None)  # built after the behavior rename field
        if obj is pose_edit and isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
            self._end_pose_rename()
            return True
        return super().eventFilter(obj, event)

    # ================================================================ library
    def refresh_poses(self, select: str | None = None) -> None:
        current = select or self._selected_pose_id()
        self.poses, problems = self.service.list_poses()
        self.pose_combo.blockSignals(True)
        self.pose_combo.clear()
        for pz in self.poses:
            src = "real arm" if pz.source.value == "real_arm" else "3D"
            self.pose_combo.addItem(pz.name, pz.id)
            self.pose_combo.setItemData(
                self.pose_combo.count() - 1, f"saved from the {src}", Qt.ItemDataRole.ToolTipRole
            )
        if not self.poses:
            self.pose_combo.addItem("No saved poses — Save current", None)
        idx = self.pose_combo.findData(current) if current else 0
        self.pose_combo.setCurrentIndex(max(0, idx))
        self.pose_combo.blockSignals(False)
        if problems:
            self.notice = "Unreadable pose files left untouched: " + " | ".join(problems)
        self._load_after_widgets()
        self._update_enabled()
        self._refresh_status()

    def _selected_pose_id(self) -> str | None:
        data = self.pose_combo.currentData() if hasattr(self, "pose_combo") else None
        return str(data) if data else None

    def _selected_pose(self) -> NamedPose | None:
        pid = self._selected_pose_id()
        return next((pz for pz in self.poses if pz.id == pid), None)

    def refresh_library(self, select: str | None = None) -> None:
        current = select or (self.behavior.id if self.behavior else None)
        summaries, problems = self.service.list_behaviors()
        self.behavior_combo.blockSignals(True)
        self.behavior_combo.clear()
        for s in summaries:
            self.behavior_combo.addItem(s.name, s.id)
            self.behavior_combo.setItemData(
                self.behavior_combo.count() - 1,
                f"{ORIGIN_LABEL.get(s.origin, s.origin.value)} · {s.duration_s:.1f} s · rev {s.revision}",
                Qt.ItemDataRole.ToolTipRole,
            )
        if not summaries:
            self.behavior_combo.addItem("No behaviors yet — press Record", None)
        idx = self.behavior_combo.findData(current) if current else 0
        self.behavior_combo.setCurrentIndex(max(0, idx))
        self.behavior_combo.blockSignals(False)
        self._load_problems = [p.error for p in problems]
        self._load_selected()

    def _on_select(self, *_: Any) -> None:
        self._load_selected()

    def _load_selected(self) -> None:
        bid = self.behavior_combo.currentData()
        self.behavior = None
        if bid:
            try:
                self.behavior = self.service.load(str(bid))
            except (BehaviorFormatError, OSError) as exc:
                self.confirmer.error("Behavior unreadable", str(exc))
        self._invalidate_preview("selection changed")
        self._end_scrub()
        b = self.behavior
        if b is None:
            self.info_label.setText("Record a movement to create your first behavior.")
        else:
            fp = "" if b.origin is not Origin.REAL_HAND_GUIDED else " · tied to this calibration"
            self.info_label.setText(
                f"{b.duration_s:.1f} s · {len(JOINT_NAMES)} joints · {ORIGIN_LABEL[b.origin]} · "
                f"rev {b.revision}{fp}"
            )
        if getattr(self, "_load_problems", None):
            self.info_label.setText(
                self.info_label.text()
                + "\nUnreadable files left untouched: "
                + " | ".join(self._load_problems)
            )
        self._load_settings_widgets()
        self._load_after_widgets()
        self._load_keyframes()
        self._update_enabled()
        self._refresh_status()

    def _load_settings_widgets(self) -> None:
        b = self.behavior
        s = b.settings if b else PlaybackSettings()
        dur = b.duration_s if b else 0.0
        self._trim = (s.trim_start_s, dur if s.trim_end_s is None else s.trim_end_s)
        self._speed = s.speed
        self.speed_value.setText(f"{s.speed:.1f}×")
        for w in (self.repeats, self.forever):
            w.blockSignals(True)
        self.repeats.setValue(s.repeat_count or 1)
        self.forever.setChecked(s.repeat_count is None)
        self.repeats.setEnabled(s.repeat_count is not None)
        for w in (self.repeats, self.forever):
            w.blockSignals(False)
        keyframes = [x.t for x in b.samples] if b is not None and b.origin is Origin.SIM_KEYFRAMES else []
        self.timeline.set_behavior(dur, self._trim[0], self._trim[1], keyframes)
        self._update_time_labels(None)

    def _on_trim(self, start: float, end: float) -> None:
        self._trim = (start, end)
        self._on_settings_changed()

    def _on_settings_changed(self, *_: Any) -> None:
        self.repeats.setEnabled(not self.forever.isChecked())
        if self.approval is not None or self.preview is not None:
            self._invalidate_preview("playback settings changed")
        self._update_time_labels(None)
        self._update_enabled()

    def _update_time_labels(self, t_src: float | None) -> None:
        b = self.behavior
        dur = b.duration_s if b else 0.0
        start, end = self._trim
        shown = start if t_src is None else t_src
        self.time_label.setText(f"{shown:.1f} s / {dur:.1f} s")
        self.trim_label.setText(
            f"Trim {start:.2f} s – {end:.2f} s · plays {(end - start) / max(self._speed, 1e-9):.1f} s at "
            f"{self._speed:.1f}×"
            if b is not None
            else ""
        )

    # -- ⋯ menu
    def _start_rename(self) -> None:
        if self.behavior is None:
            return
        self.rename_edit.setText(self.behavior.name)
        self.behavior_combo.hide()
        self.rename_edit.show()
        self.rename_edit.setFocus()
        self.rename_edit.selectAll()

    def _end_rename(self) -> None:
        self.rename_edit.hide()
        self.behavior_combo.show()

    def _commit_rename(self) -> None:
        b = self.behavior
        if b is None:
            return
        try:
            b = self.service.rename(b, self.rename_edit.text(), b.description)
        except BehaviorFormatError as exc:
            self.confirmer.error("Not renamed", str(exc))
            return
        self._end_rename()
        self.notice = f"Renamed to '{b.name}'."
        self.refresh_library(select=b.id)

    def _on_duplicate(self) -> None:
        if self.behavior is None:
            return
        try:
            b = self.service.duplicate(self.behavior)
        except BehaviorFormatError as exc:
            self.confirmer.error("Not duplicated", str(exc))
            return
        self.notice = f"Duplicated as '{b.name}'."
        self.refresh_library(select=b.id)

    def _on_save_settings(self) -> None:
        if self.behavior is None:
            return
        try:
            b = self.service.set_settings(self.behavior, self.settings())
        except BehaviorFormatError as exc:
            self.confirmer.error("Settings not saved", str(exc))
            return
        self.notice = "Playback settings saved to the behavior."
        self.refresh_library(select=b.id)

    def _on_delete(self) -> None:
        if self.behavior is not None and self.confirmer.confirm_delete_behavior(self.behavior.name):
            self.service.delete(self.behavior.id)
            self.notice = f"Deleted '{self.behavior.name}'."
            self.behavior = None
            self.refresh_library()

    def _on_new_keyframes(self) -> None:
        pose = self.current_slider_pose()
        name = f"Keyframes {time.strftime('%H:%M:%S')}"
        try:
            b = self.service.create_keyframe_behavior(name, [(0.0, pose), (2.0, pose)])
        except BehaviorFormatError as exc:
            self.confirmer.error("Not created", str(exc))
            return
        self.notice = f"Created '{name}' — add keyframes from the joint sliders, rename with ⋯."
        self.refresh_library(select=b.id)
        self.keyframes_toggle.setChecked(True)
        self.reveal_sliders()

    # ================================================================ after behavior (ADR-017)
    def _load_after_widgets(self) -> None:
        b = self.behavior
        action = b.settings.after_action if b is not None else "rest"
        self.after_combo.blockSignals(True)
        self.after_combo.setCurrentIndex(max(0, self.after_combo.findData(action)))
        self.after_combo.blockSignals(False)
        self.after_pose_combo.blockSignals(True)
        self.after_pose_combo.clear()
        for pz in self.poses:
            self.after_pose_combo.addItem(pz.name, pz.id)
        pose_id = b.settings.after_pose_id if b is not None else None
        if action == "pose" and pose_id and self.after_pose_combo.findData(pose_id) < 0:
            self.after_pose_combo.addItem("⚠ missing pose — choose another", pose_id)
        if pose_id:
            self.after_pose_combo.setCurrentIndex(max(0, self.after_pose_combo.findData(pose_id)))
        self.after_pose_combo.blockSignals(False)
        self.after_pose_combo.setVisible(action == "pose")

    def ending_pose_missing(self) -> bool:
        b = self.behavior
        return (
            b is not None
            and b.settings.after_action == "pose"
            and not any(pz.id == b.settings.after_pose_id for pz in self.poses)
        )

    def _on_after_changed(self, *_: Any) -> None:
        b = self.behavior
        if b is None:
            return
        action = str(self.after_combo.currentData())
        pose_id = None
        if action == "pose":
            if not self.poses:
                self.confirmer.error(
                    "No saved poses yet",
                    "Save a pose first (Poses → Save current), then choose it as the ending.",
                )
                self._load_after_widgets()
                return
            data = self.after_pose_combo.currentData()
            pose_id = str(data) if data else self.poses[0].id
            if not any(pz.id == pose_id for pz in self.poses):
                pose_id = self.poses[0].id
        if action == b.settings.after_action and pose_id == b.settings.after_pose_id:
            self._load_after_widgets()
            return
        try:
            nb = self.service.set_after(b, action, pose_id)
        except BehaviorFormatError as exc:
            self.confirmer.error("Ending not saved", str(exc))
            self._load_after_widgets()
            return
        label_text = dict(AFTER_ITEMS)[action] + (
            f" '{next(pz.name for pz in self.poses if pz.id == pose_id)}'" if pose_id else ""
        )
        self.notice = f"'{nb.name}' now ends with: {label_text} (saved)."
        self.refresh_library(select=nb.id)

    # ================================================================ saved poses (ADR-017)
    def _on_save_pose(self) -> None:
        if self.snapshot is None:
            return
        name = f"Pose {time.strftime('%H:%M:%S')}"
        try:
            pz = self.service.save_current_pose(name, self.snapshot)
        except PoseFormatError as exc:
            self.confirmer.error("Pose not saved", str(exc))
            return
        where = "the real arm" if pz.source.value == "real_arm" else "the 3D arm"
        clamped = (
            f" (kept inside the safe range: {', '.join(pz.clamped_joints)})" if pz.clamped_joints else ""
        )
        self.notice = f"Saved '{pz.name}' from {where}{clamped} — rename it with the pose ⋯ menu."
        self.refresh_poses(select=pz.id)

    def _on_go_pose(self) -> None:
        pz, snap = self._selected_pose(), self.snapshot
        if pz is None or snap is None or Action.MOVE_TO_POSE not in snap.available_actions:
            return
        self._invalidate_preview("move to pose")
        self._end_scrub()
        moving = snap.connection in {Connection.PLAYING_BEHAVIOR, Connection.MOVING_TO_POSE}
        if snap.mode == Mode.SIM_TO_REAL and not moving:  # already moving: like Go Rest, no extra card
            text = dialogs.MOVE_TO_POSE_TEXT.format(name=pz.name, profile=self.entry_profile.label)
            if not self.confirmer.confirm_move_to_pose(text):
                return
        try:
            self.service.move_to_pose(pz.id, self.entry_profile, confirmed=True)
        except PoseFormatError as exc:
            self.confirmer.error("Pose unavailable", str(exc))

    def _on_preview_pose(self) -> None:
        pz = self._selected_pose()
        if pz is None or self.preview is not None:
            return
        self.scrubbing = True
        self._display(pz.pose)
        self.live_button.show()
        self.notice = f"Showing pose '{pz.name}' in 3D (not the real arm)."
        self._refresh_status()

    def _start_pose_rename(self) -> None:
        pz = self._selected_pose()
        if pz is None:
            return
        self.pose_rename_edit.setText(pz.name)
        self.pose_combo.hide()
        self.pose_rename_edit.show()
        self.pose_rename_edit.setFocus()
        self.pose_rename_edit.selectAll()

    def _end_pose_rename(self) -> None:
        self.pose_rename_edit.hide()
        self.pose_combo.show()

    def _commit_pose_rename(self) -> None:
        pz = self._selected_pose()
        if pz is None:
            return
        try:
            renamed = self.service.rename_pose(pz.id, self.pose_rename_edit.text())
        except PoseFormatError as exc:
            self.confirmer.error("Pose not renamed", str(exc))
            return
        self._end_pose_rename()
        self.notice = f"Pose renamed to '{renamed.name}'."
        self.refresh_poses(select=renamed.id)

    def _on_delete_pose(self) -> None:
        pz = self._selected_pose()
        if pz is None:
            return
        users = [s.name for s in self.service.list_behaviors()[0] if self._behavior_ends_at(s.id, pz.id)]
        warn = f" It is the ending of: {', '.join(users)} (they will need another ending)." if users else ""
        if not self.confirmer.confirm_delete_behavior(f"pose '{pz.name}'.{warn}"):
            return
        self.service.delete_pose(pz.id)
        self.notice = f"Deleted pose '{pz.name}'."
        self.refresh_poses()

    def _behavior_ends_at(self, behavior_id: str, pose_id: str) -> bool:
        try:
            b = self.service.load(behavior_id)
        except (BehaviorFormatError, OSError):
            return False
        return b.settings.after_action == "pose" and b.settings.after_pose_id == pose_id

    # ================================================================ keyframes
    def _on_keyframes_toggled(self, on: bool) -> None:
        self.kf_box.setVisible(on)
        if on and self.behavior is not None and self.behavior.origin is Origin.SIM_KEYFRAMES:
            self.reveal_sliders()

    def _load_keyframes(self) -> None:
        b = self.behavior
        editable = b is not None and b.origin is Origin.SIM_KEYFRAMES
        self.kf_note.setVisible(not editable)
        for w in (self.kf_table, self.kf_add, self.kf_del, self.kf_save):
            w.setVisible(editable)
        self.kf_table.setRowCount(0)
        if not editable or b is None:
            return
        for s in b.samples:
            assert s.requested is not None
            self._append_keyframe_row(s.t, s.requested)

    def _append_keyframe_row(self, t: float, pose: Mapping[str, float]) -> None:
        r = self.kf_table.rowCount()
        self.kf_table.insertRow(r)
        self.kf_table.setItem(r, 0, QTableWidgetItem(f"{t:.3f}"))
        for c, n in enumerate(JOINT_NAMES, start=1):
            self.kf_table.setItem(r, c, QTableWidgetItem(f"{pose[n]:.2f}"))

    def keyframes_from_table(self) -> tuple[Sample, ...]:
        rows = []
        for r in range(self.kf_table.rowCount()):
            cells = [self.kf_table.item(r, c) for c in range(1 + len(JOINT_NAMES))]
            try:
                values = [float(c.text()) if c is not None else float("nan") for c in cells]
            except ValueError as exc:
                raise BehaviorFormatError([f"keyframe row {r + 1}: {exc}"]) from exc
            rows.append(Sample(values[0], requested=dict(zip(JOINT_NAMES, values[1:], strict=True))))
        return tuple(rows)

    def _on_add_keyframe(self) -> None:
        last = self.kf_table.rowCount() - 1
        t = 0.0
        if last >= 0:
            item = self.kf_table.item(last, 0)
            t = float(item.text()) + 1.0 if item is not None else 0.0
        self._append_keyframe_row(t, self.current_slider_pose())

    def _on_delete_keyframe(self) -> None:
        for r in sorted({i.row() for i in self.kf_table.selectedIndexes()}, reverse=True):
            self.kf_table.removeRow(r)

    def _on_save_keyframes(self) -> None:
        if self.behavior is None:
            return
        try:
            b = self.service.set_keyframes(self.behavior, self.keyframes_from_table())
        except BehaviorFormatError as exc:
            self.confirmer.error("Keyframes not saved", "\n".join(exc.problems))
            return
        self.notice = "Keyframes saved (new revision)."
        self.refresh_library(select=b.id)

    # ================================================================ mode workflow
    def _sync_mode_combo(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(snap.mode))
        self.mode_combo.blockSignals(False)
        acts = snap.available_actions
        allowed = {
            Mode.SIM_ONLY: True,
            Mode.REAL_TO_SIM: Action.ENTER_MODE_REAL_TO_SIM in acts or snap.mode == Mode.REAL_TO_SIM,
            Mode.SIM_TO_REAL: Action.ENTER_MODE_SIM_TO_REAL in acts or snap.mode == Mode.SIM_TO_REAL,
        }
        model = self.mode_combo.model()
        assert isinstance(model, QStandardItemModel)
        for i in range(self.mode_combo.count()):
            item = model.item(i)
            if item is not None:
                item.setEnabled(allowed[Mode(self.mode_combo.itemData(i))])
        busy = (
            snap.connection
            in {
                Connection.CONNECTING,
                Connection.PLAYING_BEHAVIOR,
                Connection.RETURNING_TO_REST,
                Connection.MOVING_TO_POSE,
            }
            or Action.STOP_RECORDING in acts
        )
        self.mode_combo.setEnabled(not busy)

    def select_mode(self, mode: Mode) -> None:
        """Programmatic equivalent of picking ``mode`` in the dropdown (tests, keyboard)."""
        idx = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(idx)
        self._on_mode_activated(idx)

    def _on_mode_activated(self, index: int) -> None:
        target = Mode(self.mode_combo.itemData(index))
        snap = self.snapshot
        if snap is None or target == snap.mode:
            self._sync_mode_combo()
            return
        port, cal = self.connection_settings()
        needs_connect = target != Mode.SIM_ONLY and self.coordinator.robot is None
        if needs_connect:
            if not port.startswith("/dev/"):
                self.confirmer.error(
                    "Connection not set up",
                    "Open Advanced settings → Connection and select the servo bus port "
                    "(/dev/serial/by-id/…) and your LeRobot calibration file.",
                )
                self._sync_mode_combo()
                return
            try:
                load_calibration(cal)
            except CalibrationError as exc:
                self.confirmer.error("Calibration file invalid", f"{exc}\n\nNothing was touched.")
                self._sync_mode_combo()
                return
        connect = dialogs.CONNECT_PART.format(port=port, cal=cal) if needs_connect else ""
        if target == Mode.REAL_TO_SIM:
            ok = self.confirmer.confirm_mode_change(
                "Real → Simulation", dialogs.REAL_TO_SIM_TEXT.format(connect=connect)
            )
        elif target == Mode.SIM_TO_REAL:
            torque_off = not needs_connect and (
                snap.connection == Connection.TORQUE_OFF or snap.torque != Torque.ON
            )
            ok = self.confirmer.confirm_mode_change(
                "Simulation → Real",
                dialogs.SIM_TO_REAL_TEXT.format(
                    connect=connect, torque=dialogs.SIM_TO_REAL_TORQUE_PART if torque_off else ""
                ),
            )
        else:
            ok = True  # Simulation Only stops motion targets; torque and connection are unchanged
        if not ok:
            self._sync_mode_combo()
            return
        self._invalidate_preview("mode changed")
        self._end_scrub()
        self.coordinator.submit(rq.EnterMode(target, port, cal, confirmed=True))

    # ================================================================ primary controls
    def _on_record(self) -> None:
        acts = self._acts()
        if Action.STOP_RECORDING in acts:
            self.service.stop_recording()
        elif Action.START_SIM_RECORDING in acts and self._mode() == Mode.SIM_ONLY:
            self._invalidate_preview("recording")
            self.service.start_recording(Origin.SIM_SLIDERS)
            self.reveal_sliders()
        elif Action.START_REAL_RECORDING in acts:
            self._invalidate_preview("recording")
            self.service.start_recording(Origin.REAL_HAND_GUIDED)

    def _on_play(self) -> None:
        if self._mode() == Mode.SIM_TO_REAL:
            self._play_real()
        else:
            self._start_preview()

    def _on_pause(self) -> None:
        acts = self._acts()
        if Action.PAUSE_BEHAVIOR in acts:
            self.service.pause()
        elif Action.RESUME_BEHAVIOR in acts:
            self.service.resume()
        elif self.preview is not None:
            if self.preview_paused_at is None:
                self.preview_paused_at = self.clock() - self._preview_started
                self._timer.stop()
            else:
                self._preview_started = self.clock() - self.preview_paused_at
                self.preview_paused_at = None
                self._timer.start()
            if self._last_display_pose is not None:  # tell the controller (overlays freeze with the preview)
                self.show_preview_pose(self._last_display_pose)
            self._refresh_status()
            self._update_enabled()

    def _save_rest_mode(self) -> bool:
        """Real → Simulation: the arm is limp and moved by hand, so the fourth control saves its pose
        as the rest pose instead of moving anywhere (owner request, Studio UI)."""
        return self._mode() == Mode.REAL_TO_SIM

    def _on_go_rest(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        if self._save_rest_mode():
            allowed = Action.RECORD_REST_POSE in snap.available_actions
            if allowed and self.confirmer.confirm_record_rest_pose():
                self.coordinator.submit(rq.RecordRestPose(confirmed=True))
                self.notice = "Rest pose saved from the arm's current position — Go Rest returns here."
            return
        if Action.STOP_TO_REST not in snap.available_actions:
            return
        if self.preview is not None:
            self._invalidate_preview("go rest")
        self._end_scrub()
        if snap.mode == Mode.SIM_TO_REAL and snap.connection in {
            Connection.HOLDING,
            Connection.CONNECTED_READ_ONLY,
        }:
            text = dialogs.GO_REST_TEXT.format(speed=policy.REST_RETURN_SPEED)
            if not self.confirmer.confirm_go_rest(text):
                return
        self.coordinator.submit(rq.StopToRest())

    # ================================================================ preview / sim playback
    def _invalidate_preview(self, why: str) -> None:
        had = self.approval is not None
        self.approval = None
        self._approval_key = None
        if self.preview is not None:
            self.preview.cancel()
            self.preview = None
            self.preview_paused_at = None
            self._timer.stop()
            self._end_preview_display()
        if had:
            self.preview_label.setText(f"Preview no longer valid ({why}).")

    def _prepare(self) -> PreviewSession | None:
        if self.behavior is None or self.snapshot is None:
            return None
        try:
            prepared = self.service.prepare_preview(
                self.behavior,
                self.settings(),
                self.snapshot,
                entry_profile=self.entry_profile,
                bridge_profile=self.bridge_profile,
            )
        except (BehaviorFormatError, CompatibilityError) as exc:
            self.confirmer.error("Cannot play this behavior", str(exc))
            return None
        self.preview_reason = prepared.provisional_reason
        self.clamp_label.setText(prepared.session.plan.audit.describe())
        return prepared.session

    def _start_preview(self) -> None:
        self._invalidate_preview("new preview")
        self._end_scrub()
        session = self._prepare()
        if session is None:
            return
        self.preview = session
        self.preview_paused_at = None
        self._preview_started = self.clock()
        self._timer.start()
        self.preview_step(0.0)
        self._update_enabled()

    def preview_step(self, elapsed: float) -> None:
        """Advance the running 3D playback to ``elapsed`` seconds (timer-driven; tests call it directly)."""
        session = self.preview
        if session is None:
            return
        pose, _label = session.step(elapsed)
        self._display(pose)
        self.timeline.set_playhead(self._preview_source_time(session, elapsed))
        self._update_time_labels(self.timeline.playhead)
        if session.completed:
            self._timer.stop()
            self.approval = session.approval()
            self._approval_key = self._current_key() if self.approval else None
            self.preview = None
            self.preview_paused_at = None
            self._end_preview_display()
            if self.approval is not None and self._mode() == Mode.SIM_TO_REAL:
                self.preview_label.setText(
                    "Preview complete — authorizes ONE real run of exactly this trajectory from the current "
                    "pose (re-preview after any change, or if the arm moves > 3°)."
                )
            elif self.approval is None and self._mode() == Mode.SIM_TO_REAL:
                self.preview_label.setText(f"Preview complete — PROVISIONAL: {self.preview_reason}")
            else:
                self.preview_label.setText("Played in 3D.")
        self._refresh_status()
        self._update_enabled()

    def _preview_source_time(self, session: PreviewSession, t: float) -> float:
        plan = session.plan
        start, end = self._trim
        if t >= session.behavior_s:
            return end
        return _source_time(
            t,
            entry=plan.entry.duration,
            content=plan.content.duration,
            bridge=plan.bridge.duration,
            start=start,
            end=end,
            speed=self._speed,
        )

    def _on_preview_timer(self) -> None:
        self.preview_step(self.clock() - self._preview_started)

    def _on_scrub(self, t_src: float) -> None:
        if self.preview is not None or self.behavior is None:
            return
        if self.snapshot is not None and self.snapshot.connection in {
            Connection.PLAYING_BEHAVIOR,
            Connection.RETURNING_TO_REST,
        }:
            return
        times, poses = self.behavior.series()
        self.scrubbing = True
        self._display(_interpolate(times, poses, t_src))
        self.live_button.show()
        self._update_time_labels(t_src)
        self._refresh_status()

    def _end_scrub(self) -> None:
        if self.scrubbing:
            self.scrubbing = False
            self._end_preview_display()
        self.live_button.hide()

    def _display(self, pose: Mapping[str, float]) -> None:
        self._last_display_pose = dict(pose)
        self.show_preview_pose(pose)
        limits = self.model_range_logical()
        notes = []
        for n in JOINT_NAMES:
            if n in self.full_turn or n == "gripper":
                continue
            lo, hi = limits[n]
            v = pose[n]
            if not lo <= v <= hi:
                notes.append(f"{n} {v:+.1f}° drawn at {min(max(v, lo), hi):+.1f}°")
        self.display_limit_label.setText(
            "3D view limited by the MuJoCo model range (display only — the trajectory is NOT clamped): "
            + ", ".join(notes)
            if notes
            else ""
        )

    def _end_preview_display(self) -> None:
        self._last_display_pose = None
        self.show_preview_pose(None)
        self.display_limit_label.setText("")
        self.timeline.set_playhead(None)

    # ================================================================ real playback
    def _play_real(self) -> None:
        snap, b = self.snapshot, self.behavior
        if snap is None or b is None:
            return
        approval = self.approval if self._approval_valid() else None  # D7: optional
        torque_off = snap.connection == Connection.TORQUE_OFF or snap.torque == Torque.OFF
        s = self.settings()
        session = self._prepare()
        entry_s = session.plan.entry.duration if session is not None else 0.0
        seamless = session is not None and session.plan.bridge.duration == 0.0
        reps = "until you stop it" if s.repeat_count is None else f"{s.repeat_count}×"
        in_motion = snap.connection in {
            Connection.PLAYING_BEHAVIOR,
            Connection.MOVING_TO_POSE,
            Connection.RETURNING_TO_REST,
        }
        lines = [
            "This REPLACES the motion that is running now: the arm turns smoothly into the new behavior "
            "from where it is heading (no return to rest in between)."
            if in_motion
            else "",
            "Preview authorized — completed on the connected arm."
            if approval is not None
            else "NO 3D preview was run for this run (optional, D7). The arm starts from where it is now — "
            "check the workspace.",
            "The motors are OFF: confirming ENERGIZES all six motors (targets set to the measured position "
            "first, no jump)."
            if torque_off
            else "Torque is ON (movement enabled): the motors will move.",
            f"Getting into position: {self.entry_profile.label} ({entry_s:.1f} s), then '{b.name}' at "
            f"{s.speed:g}× — repeats: {reps}.",
            f"Between repetitions: {self.bridge_profile.label}"
            + (" (the gesture ends where it starts: no bridge)." if seamless else "."),
            self._ending_sentence(s),
            "Recorded motion is replayed as recorded — not speed-limited; only the calibrated limits (−5°) "
            f"apply. {self.clamp_label.text()}",
            "Supervision required: stay with the arm and be ready to stop.",
            "Emergency: EMERGENCY HOLD in the app, or cut the servo power.",
            "Known open risks: the unexplained wrist motion (ADR-009) and the 5 V supply.",
        ]
        text = "\n".join(f"•  {line}" for line in lines if line)
        confirmed = self.confirmer.confirm_play_behavior(text)
        self.approval = None  # single use, whatever the answer
        self._approval_key = None
        self._end_preview_display()
        if not confirmed:
            self.preview_label.setText("Play cancelled.")
            return
        self.service.play(
            b,
            s,
            approval,
            confirmed=True,
            enable_torque=torque_off,
            entry_profile=self.entry_profile,
            bridge_profile=self.bridge_profile,
        )
        self.preview_label.setText("Preview used for this run." if approval is not None else "")

    def _ending_sentence(self, s: PlaybackSettings) -> str:
        if s.repeat_count is None:
            return "It loops until you stop it; Go Rest then returns to the rest pose and holds."
        if s.after_action == "hold":
            return (
                "When all repetitions are done it HOLDS its last position (torque on). "
                "Stopping early returns to rest."
            )
        if s.after_action == "pose":
            name = next((pz.name for pz in self.poses if pz.id == s.after_pose_id), "?")
            return (
                f"When all repetitions are done it moves smoothly to pose '{name}' and holds. "
                "Stopping early returns to rest."
            )
        return "Then it returns to the rest pose and holds with torque on."

    # ================================================================ snapshots
    # ================================================================ Follow Me / Talk Mode (ADR-019)
    def preview_direction(self) -> Any:
        """Follow Me reference of the behavior being played in 3D (Simulation Only composition, D6)."""
        if self.preview is None or self.behavior is None:
            return None
        return self.service.behavior_direction(self.behavior, self.settings())

    def preview_frozen(self) -> bool:
        return self.preview is None or self.preview_paused_at is not None

    def _overlays(self) -> tuple[dict[str, Any], dict[str, Any]]:
        snap = self.snapshot
        if snap is None:
            return {}, {}
        return dict(snap.extra.get("follow") or {}), dict(snap.extra.get("talk") or {})

    def _on_follow_clicked(self) -> None:
        follow, _ = self._overlays()
        snap = self.snapshot
        if follow.get("enabled"):
            self.coordinator.submit(rq.SetFollow(False))  # = Stop following (pan handed back smoothly)
            return
        if snap is None or Action.FOLLOW not in snap.available_actions:
            self.follow_button.setChecked(False)
            return
        if snap.mode == Mode.SIM_TO_REAL and not self.confirmer.confirm_overlay(
            "Follow Me", dialogs.FOLLOW_ON_TEXT
        ):
            self.follow_button.setChecked(False)
            return
        self.coordinator.submit(rq.SetFollow(True, float(self.follow_target.value())))

    def _on_follow_target(self, value: float) -> None:
        follow, _ = self._overlays()
        if follow.get("enabled"):
            self.coordinator.set_follow_target(float(value))

    def _on_talk_clicked(self) -> None:
        _, talk = self._overlays()
        snap = self.snapshot
        if talk.get("active"):
            self._talk_renew_timer.stop()
            self.coordinator.submit(rq.TalkStop())
            return
        if snap is None or Action.TALK not in snap.available_actions:
            self.talk_button.setChecked(False)
            return
        if snap.mode == Mode.SIM_TO_REAL and not self.confirmer.confirm_overlay(
            "Talk Mode", dialogs.TALK_ON_TEXT
        ):
            self.talk_button.setChecked(False)
            return
        self.coordinator.submit(rq.TalkStart(str(self.talk_intensity.currentData()), STUDIO_TALK_LEASE_S))
        self._talk_renew_timer.start()

    def _renew_talk(self, changed: bool = False) -> None:
        """The Studio holds a renewable lease like any client (a closed window cannot leave it running)."""
        _, talk = self._overlays()
        if not talk.get("active"):
            if not changed:
                self._talk_renew_timer.stop()
            return
        self.coordinator.submit(rq.TalkStart(str(self.talk_intensity.currentData()), STUDIO_TALK_LEASE_S))

    def _update_overlay_widgets(self) -> None:
        follow, talk = self._overlays()
        acts = self._acts()
        on = bool(follow.get("enabled"))
        self.follow_button.setChecked(on)
        self.follow_button.setText("Follow Me: ON" if on else "Follow Me")
        self.follow_button.setEnabled(on or Action.FOLLOW in acts)
        self.stop_follow_button.setVisible(on)
        state = follow.get("state", "OFF")
        if state == "OFF":
            text = ""
        elif state == "SUSPENDED":
            text = f"SUSPENDED — pan holds its last target: {follow.get('suspended_reason')}"
        elif state == "HANDING_BACK":
            text = "Returning the pan to the behavior / pose…"
        else:
            ref = follow.get("reference") or {}
            facing = follow.get("facing_estimate_deg")
            reach = follow.get("reach") or "…"
            parts = [
                f"Facing {facing:+.0f}°" if facing is not None else "Facing —",
                f"target {follow.get('target_deg', 0):+.0f}°",
                f"{ref.get('classification', '?')} ref {ref.get('psi_deg', 0):+.0f}°",
                reach
                + (f" (off by {abs(follow['residual_deg']):.0f}°)" if follow.get("residual_deg") else ""),
            ]
            if state in ("REORIENTING", "PAUSED", "AT_LIMIT"):
                parts.append(state.lower().replace("_", " "))
            text = " · ".join(parts)
        self.follow_status.setText(text)
        talking = bool(talk.get("active"))
        self.talk_button.setChecked(talking)
        self.talk_button.setText("Talk: ON" if talking else "Talk")
        self.talk_button.setEnabled(talking or Action.TALK in acts)
        tstate = talk.get("state", "OFF")
        self.talk_indicator.setText(
            {
                "ON": f"● talking ({talk.get('intensity')})",
                "PAUSED": "● paused",
                "STOPPING": "○ stopping…",
            }.get(tstate, "")
        )

    def apply_snapshot(self, snap: Snapshot) -> None:
        previous = self.snapshot
        self.snapshot = snap
        if self.preview is not None and snap.connection in {
            Connection.RETURNING_TO_REST,
            Connection.PLAYING_BEHAVIOR,
            Connection.MOVING_TO_POSE,
        }:
            self._invalidate_preview("the real arm is moving — the 3D view shows it again")
        if (
            previous is not None
            and self.approval is not None
            and (
                previous.extra.get("calibration_fingerprint") != snap.extra.get("calibration_fingerprint")
                or previous.extra.get("hw_bounds") != snap.extra.get("hw_bounds")
            )
        ):
            self._invalidate_preview("calibration changed")
        motion = {Connection.PLAYING_BEHAVIOR, Connection.MOVING_TO_POSE, Connection.RETURNING_TO_REST}
        if snap.connection in motion and (previous is None or previous.connection not in motion):
            self.notice = ""  # a new run / move starts: its own result replaces the last notice
        rec = snap.extra.get("recording")
        if rec is not None and rec.get("state") == "finished":
            self._collect_recording()
        self._sync_mode_combo()
        self._update_overlay_widgets()
        self._refresh_status()
        self._update_enabled()

    def _collect_recording(self) -> None:
        rec = self.service.take_recording()
        if rec is None:
            return
        if not rec.valid:
            self.notice = "Last take NOT playable — record again."
            self.confirmer.error(
                "Recording not playable",
                "The take was discarded (nothing saved):\n• "
                + "\n• ".join(rec.problems()[:12])
                + "\n\nYou can record again.",
            )
            return
        prefix = "Take" if rec.origin is Origin.REAL_HAND_GUIDED else "Slider take"
        name = f"{prefix} {time.strftime('%H:%M:%S')}"
        try:
            b = self.service.save_recording(rec, name, "")
        except BehaviorFormatError as exc:
            self.confirmer.error("Not saved", str(exc))
            return
        self.notice = f"Saved '{b.name}' ({rec.report.duration_s:.1f} s) — rename it with ⋯."
        self.refresh_library(select=b.id)

    # -- status
    def _refresh_status(self) -> None:
        snap = self.snapshot
        if snap is None:
            return
        beh = snap.extra.get("behavior") or {}
        rec = snap.extra.get("recording") or {}
        chip: int | None = None
        progress = 0.0
        pill, tone = "Ready", theme.GREEN
        text = self.notice or (
            f"Ready to play '{self.behavior.name}'." if self.behavior is not None else "No behavior selected."
        )
        if snap.fault is not None:
            pill, tone = "Fault", theme.RED
            text = f"Fault {snap.fault.code} — no commands are sent. Open diagnostics to recover."
        elif rec.get("state") == "recording":
            pill, tone = "Recording", theme.RED
            src = "hand-guided" if rec.get("origin") == Origin.REAL_HAND_GUIDED.value else "sliders"
            text = (
                f"Recording ({src}) · {rec.get('elapsed_s', 0.0):.1f} s · {rec.get('samples', 0)} samples"
                + (f" · INVALID: {rec['invalid_so_far']}" if rec.get("invalid_so_far") else "")
            )
        elif snap.connection == Connection.PLAYING_BEHAVIOR and beh.get("state") in {"playing", "paused"}:
            paused = beh["state"] == "paused"
            pill, tone = ("Paused", theme.AMBER) if paused else ("Playing", theme.GREEN)
            reps = "∞" if beh.get("repeat_count") is None else beh.get("repeat_count")
            seg = beh.get("segment")
            chip = 0 if seg in {"entry", "resume", None} else 1
            step = {
                "entry": "getting into position",
                "resume": "rejoining the trajectory",
                "content": f"loop {beh.get('repetition')}/{reps}",
                "bridge": f"between loops {beh.get('repetition')}/{reps}",
            }.get(str(seg), "")
            text = f"{'Paused' if paused else 'Playing'} {beh.get('name')} · {step}"
            if beh.get("finish_after_repetition"):
                after = (beh.get("settings") or {}).get("after", {}).get("action", "rest")
                then = {"rest": "return to rest", "hold": "hold", "pose": "move to its pose"}.get(
                    after, "rest"
                )
                text += f" · finishing after loop {beh['finish_after_repetition']}, then {then}"
            total = beh.get("nominal_total_s")
            t = float(beh.get("t_logical_s", 0.0))
            progress = (t / total) if total else (t % 10.0) / 10.0
            settings = beh.get("settings") or {}
            start = float(settings.get("trim_start_s", 0.0))
            end = settings.get("trim_end_s")
            end_f = float(end) if end is not None else (self.behavior.duration_s if self.behavior else start)
            t_src = _source_time(
                t,
                entry=float(beh.get("entry_s", 0.0)),
                content=float(beh.get("content_s", 0.0)),
                bridge=float(beh.get("bridge_s", 0.0)),
                start=start,
                end=end_f,
                speed=float(settings.get("speed", 1.0)),
            )
            if self.behavior is not None and self.behavior.id == beh.get("behavior_id"):
                self.timeline.set_playhead(t_src)
                self._update_time_labels(t_src)
            if float(beh.get("timeline_delay_ms", 0.0)) > 250:
                text += f" · serial delay {beh['timeline_delay_ms'] / 1000:.1f} s"
        elif snap.connection == Connection.MOVING_TO_POSE:
            pm = snap.extra.get("pose_move") or {}
            pill, tone = "Moving to pose", theme.BLUE
            chip = 2
            progress = float(pm.get("progress", 0.0))
            text = f"Moving to pose '{pm.get('name', '')}'" + (
                f" · {pm['purpose']}" if pm.get("purpose", "").startswith("ending") else ""
            )
        elif snap.connection == Connection.RETURNING_TO_REST:
            pill, tone = "Returning to rest", theme.BLUE
            chip = 2
            progress = float(snap.extra.get("rest_progress") or 0.0)
            text = beh.get("last_result") or "Returning to the rest pose…"
        elif snap.extra.get("sim_rest_progress") is not None:
            pill, tone = "Returning to rest", theme.BLUE
            chip = 2
            progress = float(snap.extra["sim_rest_progress"])
            text = "The 3D arm is returning to its rest pose."
        elif self.preview is not None:
            session = self.preview
            elapsed = (
                self.preview_paused_at
                if self.preview_paused_at is not None
                else (self.clock() - self._preview_started)
            )
            paused = self.preview_paused_at is not None
            pill, tone = ("Paused", theme.AMBER) if paused else ("Previewing", theme.BLUE)
            label_text = session.pose_at(min(elapsed, session.duration))[1]
            chip = 0 if label_text.startswith("entry") else 2 if label_text.startswith("return") else 1
            progress = elapsed / max(session.duration, 1e-9)
            text = f"{'Paused' if paused else 'Playing in 3D'} (not the real arm) · {label_text}"
        elif snap.connection == Connection.CONNECTING:
            pill, tone = "Connecting…", theme.BLUE
            text = "Connecting to the arm…"
        elif self.scrubbing:
            text = "Scrubbing — 3D view only. Press Play, or 'Back to live view'."
        elif beh.get("last_result"):
            text = beh["last_result"] if not self.notice else self.notice
        ending = (snap.extra.get("behavior") or {}).get("settings", {}).get("after", {}) or {}
        action = ending.get("action") if snap.connection == Connection.PLAYING_BEHAVIOR else None
        if snap.connection == Connection.MOVING_TO_POSE:
            action = "pose"
        elif snap.connection == Connection.RETURNING_TO_REST:
            action = "rest"
        if action is None and self.behavior is not None:
            action = self.behavior.settings.after_action
        self.chips.set_label(2, ENDING_CHIP.get(str(action), "Returning to rest"))
        self.chips.set_step(chip)
        self.progress.setValue(round(1000 * min(1.0, max(0.0, progress))))
        self.status_text.setText(text)
        sub = _connection_summary(snap)
        self.status_pill.setText(
            f"<span style='color:{tone};font-weight:700;font-size:15px'>● {pill}</span><br>"
            f"<span style='color:{theme.MUTED};font-size:11px'>{sub}</span>"
        )
        if snap.fault is not None:
            f = snap.fault
            self.fault_text.setText(
                f"FAULT [{f.code}] — {f.details}\nNo commands are sent. Recover or disconnect in Advanced. "
                f"Torque: {snap.torque.value}"
            )
            self.fault_banner.show()
        else:
            self.fault_banner.hide()
        warning = snap.extra.get("divergence_warning")
        self.warning_label.setVisible(bool(warning))
        if warning:
            self.warning_label.setText(
                f"⚠ Tracking divergence (diagnostic only): {warning}. "
                "Use EMERGENCY HOLD if the arm misbehaves."
            )

    # -- enabling + reasons
    def _update_enabled(self) -> None:
        snap = self.snapshot
        acts = self._acts()
        mode = self._mode()
        has = self.behavior is not None
        playing = snap is not None and snap.connection == Connection.PLAYING_BEHAVIOR
        returning = snap is not None and snap.connection == Connection.RETURNING_TO_REST
        recording = Action.STOP_RECORDING in acts
        # Record / Stop
        can_record = (Action.START_SIM_RECORDING in acts and mode == Mode.SIM_ONLY) or (
            Action.START_REAL_RECORDING in acts
        )
        self.record_button.setEnabled(recording or (can_record and self.preview is None))
        self.record_button.setText("  Stop" if recording else "  Record")
        self.record_button.setIcon(
            action_icon("stop" if recording else "record", "#ffffff" if recording else theme.TEXT)
        )
        self.record_button.setProperty("role", "record-active" if recording else "primary")
        self.record_button.style().unpolish(self.record_button)
        self.record_button.style().polish(self.record_button)
        # Play
        if mode == Mode.SIM_TO_REAL:
            can_play = Action.PLAY_BEHAVIOR in acts and has and self.preview is None
        else:
            can_play = has and not recording and not playing and not returning and self.preview is None
        self.play_button.setEnabled(can_play)
        # Pause / Resume
        sim_pausable = self.preview is not None
        paused = (Action.RESUME_BEHAVIOR in acts) or (sim_pausable and self.preview_paused_at is not None)
        self.pause_button.setEnabled(
            Action.PAUSE_BEHAVIOR in acts or Action.RESUME_BEHAVIOR in acts or sim_pausable
        )
        self.pause_button.setText("  Resume" if paused else "  Pause")
        self.pause_button.setIcon(action_icon("resume" if paused else "pause"))
        # Go Rest (Save Rest in Real → Simulation) / Emergency Hold
        progress = snap.extra.get("rest_progress") if snap is not None else None
        if self._save_rest_mode():
            self.rest_button.setEnabled(Action.RECORD_REST_POSE in acts and not recording)
            self.rest_button.setText("  Save Rest")
            self.rest_button.setToolTip(
                "Save the arm's CURRENT position as its rest pose (nothing moves). "
                "Go Rest in Simulation → Real returns here."
            )
        else:
            self.rest_button.setEnabled(Action.STOP_TO_REST in acts)
            resting = f"  Resting {progress * 100:.0f}%" if progress is not None else "  Go Rest"
            self.rest_button.setText(resting)
            self.rest_button.setToolTip("Return to the rest pose and hold (in Simulation Only: the 3D arm)")
        self.hold_button.setEnabled(Action.EMERGENCY_HOLD in acts)
        # library + settings
        idle = not playing and not recording
        # ADR-018: in Simulation → Real another behavior may be chosen and played while one runs; it
        # supersedes the running motion (no return to rest in between), exactly like an API request.
        editable = (not playing or mode == Mode.SIM_TO_REAL) and not recording
        for a in (self.act_rename, self.act_duplicate, self.act_save_settings, self.act_delete):
            a.setEnabled(has and idle)
        self.act_new_keyframes.setEnabled(idle)
        self.behavior_combo.setEnabled(editable and self.preview is None)
        for w in (
            self.speed_minus,
            self.speed_plus,
            self.repeats,
            self.forever,
            self.entry_preset,
            self.bridge_preset,
        ):
            w.setEnabled(has and editable)
        self.repeats.setEnabled(has and editable and not self.forever.isChecked())
        self.timeline.setEnabled(has and not playing and not returning)
        self.preview_button.setVisible(mode == Mode.SIM_TO_REAL)
        self.preview_button.setEnabled(has and not playing and not returning and self.preview is None)
        # poses / after behavior
        moving = snap is not None and snap.connection in {
            Connection.PLAYING_BEHAVIOR,
            Connection.RETURNING_TO_REST,
            Connection.MOVING_TO_POSE,
        }
        has_pose = self._selected_pose() is not None
        self.save_pose_button.setEnabled(snap is not None and not moving and not recording)
        self.go_pose_button.setEnabled(has_pose and Action.MOVE_TO_POSE in acts and self.preview is None)
        beh_state = (snap.extra.get("behavior") or {}) if snap is not None else {}
        self.finish_button.setVisible(playing)
        self.finish_button.setEnabled(Action.FINISH_BEHAVIOR in acts)
        self.finish_button.setText(
            "Finishing…" if beh_state.get("finish_after_repetition") else "Finish after this loop"
        )
        for a in (self.act_rename_pose, self.act_delete_pose):
            a.setEnabled(has_pose and not moving)
        self.act_preview_pose.setEnabled(has_pose and not moving and self.preview is None)
        self.after_combo.setEnabled(has and editable)
        self.after_pose_combo.setEnabled(has and editable)
        if self.ending_pose_missing() and mode == Mode.SIM_TO_REAL:
            can_play = False
            self.play_button.setEnabled(False)
        self.reason_label.setText(self._reason(can_record, can_play))

    def _reason(self, can_record: bool, can_play: bool) -> str:
        snap = self.snapshot
        if snap is None:
            return ""
        acts = snap.available_actions
        mode = snap.mode
        if snap.fault is not None:
            return "A fault is latched: nothing moves until you recover (Open diagnostics)."
        if Action.STOP_RECORDING in acts:
            return "Recording — press Stop when done." + (
                " Move the joint sliders (Advanced) to record."
                if mode == Mode.SIM_ONLY
                else " Move the arm by hand."
            )
        msgs: list[str] = []
        if self.ending_pose_missing():
            msgs.append("This behavior ends at a pose that was deleted — choose another 'After behavior'.")
        if mode == Mode.REAL_TO_SIM and self._selected_pose() is not None:
            msgs.append("Go to a pose needs Simulation → Real (or Simulation Only for the 3D arm).")
        if self.behavior is None:
            msgs.append("Press Record to create a behavior, or pick one from the list.")
        if mode == Mode.SIM_TO_REAL and not can_record:
            msgs.append("To record: Real → Simulation (by hand) or Simulation Only (sliders).")
        if mode == Mode.REAL_TO_SIM:
            if not can_record:
                msgs.append("Recording by hand needs torque OFF — choose Real → Simulation again.")
            msgs.append(
                "Place the arm by hand and press Save Rest to make it the rest pose. Play shows the behavior "
                "in 3D; switch to Simulation → Real to play it on the arm."
            )
        if mode == Mode.SIM_TO_REAL and self.behavior is not None and not can_play and self.preview is None:
            rest = (snap.extra.get("rest_status") or {}).get("state")
            if snap.connection in {Connection.PLAYING_BEHAVIOR, Connection.RETURNING_TO_REST}:
                pass
            elif rest != "loaded":
                msgs.append(
                    "Playing on the arm needs a rest pose for this calibration (Advanced → Rest pose)."
                )
            elif snap.torque == Torque.UNKNOWN:
                msgs.append("Torque state unknown — establish it in Advanced → Hardware.")
            else:
                msgs.append(f"Play is not available while {snap.connection.value.lower().replace('_', ' ')}.")
        if (
            mode == Mode.SIM_TO_REAL
            and Action.STOP_TO_REST not in acts
            and snap.extra.get("rest_pose") is None
        ):
            msgs.append("Go Rest needs a recorded rest pose (Advanced → Rest pose).")
        if mode == Mode.SIM_ONLY and snap.paused:
            msgs.append("The simulation is frozen (Advanced → Simulation).")
        return "  ·  ".join(msgs[:2])


# ------------------------------------------------------------------------------------ helpers
def _source_time(
    t: float, *, entry: float, content: float, bridge: float, start: float, end: float, speed: float
) -> float:
    """Map logical playback time to the behavior's SOURCE time (for the timeline playhead)."""
    if t < entry:
        return start
    cycle = content + bridge
    u = t - entry
    r = u % cycle if cycle > 0 else 0.0
    if r < content:
        return min(end, start + r * speed)
    return end


def _interpolate(times: list[float], poses: list[dict[str, float]], t: float) -> dict[str, float]:
    if not times:
        return {}
    if t <= times[0]:
        return dict(poses[0])
    if t >= times[-1]:
        return dict(poses[-1])
    i = bisect.bisect_right(times, t)
    t0, t1 = times[i - 1], times[i]
    a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
    return {n: poses[i - 1][n] + a * (poses[i][n] - poses[i - 1][n]) for n in poses[0]}


def _connection_summary(snap: Snapshot) -> str:
    c = snap.connection
    if c == Connection.DISCONNECTED:
        return "Simulation · arm not connected"
    detail = {
        Connection.CONNECTING: "Connecting",
        Connection.CONNECTED_READ_ONLY: "Connected · holding",
        Connection.ARMED: "Connected · movement enabled",
        Connection.RETURNING_TO_REST: "Connected · moving",
        Connection.PLAYING_BEHAVIOR: "Connected · moving",
        Connection.HOLDING: "Connected · holding",
        Connection.FAULT_LATCHED: "Fault latched",
        Connection.TORQUE_OFF: "Connected · torque off",
        Connection.MOVING_TO_POSE: "Connected · moving",
    }[c]
    return detail

"""In-window confirmation and message cards (Studio UI): no separate pop-up windows for normal use.

``OverlayConfirmer`` implements the same ``Confirmer`` protocol as the old message boxes, so every
safety confirmation keeps its exact text and its blocking yes/no semantics (a nested event loop runs
while the card is shown). File choosers (Advanced only) stay native file dialogs.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from so101_companion.ui import dialogs, theme
from so101_companion.ui.widgets import action_icon


class OverlayCard(QWidget):
    """A dimmed full-window layer with one centered card."""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setObjectName("overlayCard")
        self.host = host
        host.installEventFilter(self)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#overlayCard{background: rgba(4, 7, 11, 175);}")
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card = QFrame()
        self.card.setObjectName("overlayInner")
        self.card.setMaximumWidth(600)
        self.card.setMinimumWidth(420)
        self.card.setStyleSheet(
            f"QFrame#overlayInner{{background:{theme.CARD};border:1px solid {theme.BORDER};"
            "border-radius:14px;}"
        )
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        self.icon = QLabel()
        self.title = QLabel()
        self.title.setStyleSheet("font-size:18px;font-weight:700;")
        head.addWidget(self.icon)
        head.addWidget(self.title, 1)
        lay.addLayout(head)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.body.setStyleSheet(f"color:{theme.TEXT};font-size:13px;line-height:140%;")
        lay.addWidget(self.body)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = QPushButton("Cancel")
        self.ok = QPushButton("OK")
        for b in (self.cancel, self.ok):
            b.setMinimumHeight(38)
            b.setMinimumWidth(120)
            buttons.addWidget(b)
        lay.addLayout(buttons)
        outer.addWidget(self.card)
        self.cancel.clicked.connect(lambda: self._finish(False))
        self.ok.clicked.connect(lambda: self._finish(True))
        self._loop: QEventLoop | None = None
        self._result = False
        self.hide()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.host and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.host.rect())
        return False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(False)
        else:
            super().keyPressEvent(event)

    def _finish(self, result: bool) -> None:
        self._result = result
        if self._loop is not None:
            self._loop.quit()

    def ask(self, title: str, text: str, *, confirm: str, cancel: str | None, tone: str) -> bool:
        colors = {
            "danger": (theme.RED_DARK, theme.RED, theme.AMBER),
            "play": (theme.GREEN_DARK, theme.GREEN, theme.GREEN),
            "neutral": (theme.BLUE_DARK, theme.BLUE, theme.BLUE),
            "error": (theme.RED_DARK, theme.RED, theme.RED),
        }[tone]
        self.title.setText(title)
        self.body.setText(text)
        self.icon.setPixmap(
            action_icon("play" if tone == "play" else "warning", colors[2], 24).pixmap(24, 24)
        )
        self.ok.setText(confirm)
        self.ok.setStyleSheet(
            f"QPushButton{{background:{colors[0]};border:1px solid {colors[1]};font-weight:600;}}"
        )
        self.cancel.setVisible(cancel is not None)
        self.cancel.setText(cancel or "")
        self.setGeometry(self.host.rect())
        self.show()
        self.raise_()
        (self.cancel if cancel is not None else self.ok).setFocus()  # the safe choice has the focus
        self._result = False
        self._loop = QEventLoop()
        self._loop.exec()
        self._loop = None
        self.hide()
        return self._result


class OverlayConfirmer:
    """``Confirmer`` with in-window cards (same texts and semantics as ``QtConfirmer``)."""

    def __init__(self, host: QWidget) -> None:
        self.host = host
        self.card = OverlayCard(host)

    def _ask(self, title: str, text: str, confirm: str = "Confirm", tone: str = "danger") -> bool:
        return self.card.ask(title, text, confirm=confirm, cancel="Cancel", tone=tone)

    def confirm_connect(self, port: str, calibration: str) -> bool:
        return self._ask(
            "Connect to the SO-101", dialogs.CONNECT_WARNING.format(port=port, cal=calibration), "Connect"
        )

    def confirm_enable_movement(self) -> bool:
        return self._ask("Enable movement", dialogs.ENABLE_WARNING, "Enable")

    def confirm_torque_off(self) -> bool:
        return self._ask("Torque OFF", dialogs.TORQUE_OFF_WARNING, "Torque OFF")

    def confirm_record_rest_pose(self) -> bool:
        return self._ask("Record rest pose", dialogs.RECORD_REST_TEXT, "Record", tone="neutral")

    def confirm_enable_torque(self) -> bool:
        return self._ask("Enable torque", dialogs.ENABLE_TORQUE_WARNING, "Enable torque")

    def confirm_recover(self, code: str, details: str) -> bool:
        return self._ask(
            "Recover from fault", dialogs.RECOVER_WARNING.format(code=code, details=details), "Recover"
        )

    def confirm_disconnect(self, disable_torque_on_disconnect: bool) -> bool:
        text = (
            dialogs.DISCONNECT_WARNING_TORQUE_OFF
            if disable_torque_on_disconnect
            else dialogs.DISCONNECT_WARNING_KEEP
        )
        return self._ask("Disconnect", text, "Disconnect")

    def confirm_quit_while_connected(self, disable_torque_on_disconnect: bool) -> bool:
        text = (
            dialogs.DISCONNECT_WARNING_TORQUE_OFF
            if disable_torque_on_disconnect
            else dialogs.DISCONNECT_WARNING_KEEP
        )
        return self._ask(
            "Quit — the arm is connected", "Quitting disconnects the arm first.\n\n" + text, "Quit"
        )

    def confirm_adopt_rest_pose(self, text: str) -> bool:
        return self._ask("Adopt rest pose for this calibration", text, "Adopt", tone="neutral")

    def confirm_play_behavior(self, text: str) -> bool:
        return self._ask("Play on the real arm?", text, "Play on Real Arm", tone="play")

    def confirm_delete_behavior(self, name: str) -> bool:
        return self._ask(
            "Delete behavior", f"Delete '{name}'? Its file is removed; this cannot be undone.", "Delete"
        )

    def confirm_mode_change(self, title: str, text: str) -> bool:
        return self._ask(title, text, "Switch mode")

    def confirm_go_rest(self, text: str) -> bool:
        return self._ask("Go to rest", text, "Go Rest", tone="neutral")

    def confirm_move_to_pose(self, text: str) -> bool:
        return self._ask("Move to saved pose", text, "Move", tone="neutral")

    def confirm_overlay(self, title: str, text: str) -> bool:
        return self._ask(title, text, "Start", tone="neutral")

    def ask_behavior_details(self, default_name: str) -> tuple[str, str] | None:
        return (default_name, "")  # the Studio names takes automatically; rename inline

    def choose_calibration_file(self, start_dir: str) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self.host, "Select EXISTING LeRobot calibration file", start_dir, "Calibration (*.json)"
        )
        return path or None

    def choose_export_path(self) -> str | None:
        from pathlib import Path

        path, _ = QFileDialog.getSaveFileName(
            self.host, "Export event log (redacted JSON Lines)", str(Path.home() / "so101_events.jsonl"),
            "JSON Lines (*.jsonl)",
        )  # fmt: skip
        return path or None

    def choose_mapping_path(self, default: str) -> str | None:
        path, _ = QFileDialog.getSaveFileName(
            self.host,
            "Save 3D display mapping into config (TOML [simulation] section)",
            default,
            "TOML (*.toml)",
        )
        return path or None

    def info(self, title: str, text: str) -> None:
        self.card.ask(title, text, confirm="OK", cancel=None, tone="neutral")

    def error(self, title: str, text: str) -> None:
        self.card.ask(title, text, confirm="OK", cancel=None, tone="error")

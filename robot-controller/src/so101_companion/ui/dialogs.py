"""Operator confirmations. The UI asks a ``Confirmer``; tests inject a scripted one.

Texts state the real consequences (torque may be toggled by LeRobot configure(), Torque Off may let
the arm fall, Hold/Torque Off are not physical emergency stops).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QMessageBox, QWidget

CONNECT_WARNING = (
    "Connecting runs LeRobot SOFollower.connect(calibrate=False):\n"
    " • opens {port}\n"
    " • configure() temporarily DISABLES TORQUE on all six motors, writes operating mode / PID and the "
    "gripper's protective limits, then re-enables torque.\n"
    " • NO automatic calibration is ever run; calibration file: {cal}\n\n"
    "Support the arm by hand / make sure it is in a safe resting position, clamped to the desk.\n"
    "Connecting does NOT enable movement — that is a separate step.\n\nConnect now?"
)
ENABLE_WARNING = (
    "Enable movement: slider targets will be SENT to the real arm.\n"
    "Fixed M1 policy: targets are clamped to the calibrated range (−5° margin, gripper 0–100) and sent "
    "DIRECTLY — no step limiter, no LeRobot max_relative_target. Large slider jumps move the joint at "
    "full servo speed.\nThe sliders start at the measured pose. Keep clear of the arm.\n\nEnable?"
)
RECORD_REST_TEXT = (
    "Record the arm's CURRENT measured pose as the rest pose for this calibration.\n"
    "Joints inside the 5° safety margin are saved at the allowed limit (shown afterwards).\n"
    "STOP will later return here at 30°/s and hold with torque on. Nothing moves now.\n\nRecord?"
)
TORQUE_OFF_WARNING = (
    "TORQUE OFF: all six motors stop holding. The arm WILL fall under gravity unless supported.\n"
    "Support the arm with your hand now. This is not a physical emergency stop.\n\nTurn torque off?"
)
ENABLE_TORQUE_WARNING = (
    "Enable torque: the goal is first set to the PRESENT position, then torque is enabled so the "
    "motors hold where the arm is now. (Physical behaviour not yet verified on hardware.)\n\nEnable torque?"
)
RECOVER_WARNING = (
    "Fault [{code}]: {details}\n\nRecovery performs ONE fresh position read. It does not reconnect, does "
    "not resume motion and does not change torque. Torque state stays UNKNOWN until you establish it "
    "(Torque Off then Enable torque).\n\nAttempt recovery?"
)
DISCONNECT_WARNING_TORQUE_OFF = (
    "Disconnect: LeRobot is configured with disable_torque_on_disconnect=True, so ALL MOTORS WILL GO LIMP "
    "and the arm may fall. Support the arm first.\n\nDisconnect?"
)
DISCONNECT_WARNING_KEEP = (
    "Disconnect: disable_torque_on_disconnect=False — motors keep their last torque state, but nothing "
    "will monitor them after the port closes.\n\nDisconnect?"
)


ADOPT_REST_TEXT = (
    "This rest pose was saved before calibration fingerprints existed ({recorded_at}).\n"
    "{pose}\n\nOnly adopt it if it is still the right physical rest pose for the CURRENT calibration "
    "(after a recalibration the same numbers can mean a different physical pose). The file is backed up "
    "first, then stamped with the current calibration fingerprint. Nothing moves now.\n\n"
    "Otherwise cancel and use Record rest pose….\n\nAdopt?"
)


CONNECT_PART = (
    "First the app connects to the arm on {port}. LeRobot configure() briefly disables torque, writes the "
    "operating settings and re-enables it; no calibration is run (file: {cal}).\n\n"
)
REAL_TO_SIM_TEXT = (
    "{connect}"
    "Torque will be turned OFF on all six motors so you can move the arm by hand.\n"
    " • The arm WILL fall under gravity unless you support it. Hold the arm now.\n"
    " • The 3D arm then mirrors the real one. Nothing is commanded while in this mode.\n\n"
    "Switch to Real → Simulation?"
)
SIM_TO_REAL_TEXT = (
    "{connect}"
    "{torque}"
    "Movement is enabled: Play, Go Rest and the joint sliders (Advanced) move the real arm. Targets are "
    "clamped to the calibrated range (−5° margin) and sent directly (fixed policy, no step limiter).\n"
    " • Stay with the arm; keep the servo-power cut-off within reach. EMERGENCY HOLD is always visible.\n"
    " • Known open risks: the unexplained wrist motion (ADR-009) and the 5 V supply.\n\n"
    "Switch to Simulation → Real?"
)
SIM_TO_REAL_TORQUE_PART = (
    "The motors are OFF. The current positions are read first, the motor targets are set to exactly "
    "those positions, then all six motors are ENERGIZED (no jump to old targets).\n\n"
)
FOLLOW_ON_TEXT = (
    "Follow Me takes over the SHOULDER PAN of the real arm and turns it toward the target angle "
    "(clockwise = the robot's right, from your configured front). The other joints keep following the "
    "behavior or pose. It turns smoothly from the current position — make sure the base can rotate freely. "
    "Emergency Hold, Torque OFF and faults stop it at once."
)
TALK_ON_TEXT = (
    "Talk Mode animates the GRIPPER of the real arm (small mouth-like openings, at most about 1.5 per "
    "second, within the verified gripper limits). Keep fingers and objects away from the jaws. Emergency "
    "Hold, Torque OFF and faults stop it at once."
)

GO_REST_TEXT = (
    "The arm is still. It will now move from where it is to its recorded rest pose at {speed:g}°/s, "
    "then hold with torque on. Keep the path clear.\n\nGo to rest?"
)

MOVE_TO_POSE_TEXT = (
    "The arm will move smoothly from where it is to the saved pose '{name}' ({profile} speed), then hold "
    "with torque on. The pose was checked against the current calibration. Keep the path clear; "
    "EMERGENCY HOLD and Go Rest stay available.\n\nMove to '{name}'?"
)


class Confirmer(Protocol):
    def confirm_connect(self, port: str, calibration: str) -> bool: ...
    def confirm_enable_movement(self) -> bool: ...
    def confirm_torque_off(self) -> bool: ...
    def confirm_record_rest_pose(self) -> bool: ...
    def confirm_enable_torque(self) -> bool: ...
    def confirm_recover(self, code: str, details: str) -> bool: ...
    def confirm_disconnect(self, disable_torque_on_disconnect: bool) -> bool: ...
    def confirm_quit_while_connected(self, disable_torque_on_disconnect: bool) -> bool: ...
    def confirm_adopt_rest_pose(self, text: str) -> bool: ...
    def confirm_play_behavior(self, text: str) -> bool: ...
    def confirm_delete_behavior(self, name: str) -> bool: ...
    def confirm_mode_change(self, title: str, text: str) -> bool: ...
    def confirm_go_rest(self, text: str) -> bool: ...
    def confirm_move_to_pose(self, text: str) -> bool: ...
    def confirm_overlay(self, title: str, text: str) -> bool: ...
    def ask_behavior_details(self, default_name: str) -> tuple[str, str] | None: ...
    def choose_calibration_file(self, start_dir: str) -> str | None: ...
    def choose_export_path(self) -> str | None: ...
    def choose_mapping_path(self, default: str) -> str | None: ...
    def info(self, title: str, text: str) -> None: ...
    def error(self, title: str, text: str) -> None: ...


class QtConfirmer:
    def __init__(self, parent: QWidget) -> None:
        self.parent = parent

    def _ask(self, title: str, text: str, danger: bool = True) -> bool:
        icon = QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question
        box = QMessageBox(
            icon, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self.parent
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def confirm_connect(self, port: str, calibration: str) -> bool:
        return self._ask("Connect SO-101 hardware", CONNECT_WARNING.format(port=port, cal=calibration))

    def confirm_enable_movement(self) -> bool:
        return self._ask("Enable movement", ENABLE_WARNING)

    def confirm_torque_off(self) -> bool:
        return self._ask("Torque OFF", TORQUE_OFF_WARNING)

    def confirm_record_rest_pose(self) -> bool:
        return self._ask("Record rest pose", RECORD_REST_TEXT, danger=False)

    def confirm_enable_torque(self) -> bool:
        return self._ask("Enable torque", ENABLE_TORQUE_WARNING)

    def confirm_recover(self, code: str, details: str) -> bool:
        return self._ask("Manual fault recovery", RECOVER_WARNING.format(code=code, details=details))

    def confirm_disconnect(self, disable_torque_on_disconnect: bool) -> bool:
        text = DISCONNECT_WARNING_TORQUE_OFF if disable_torque_on_disconnect else DISCONNECT_WARNING_KEEP
        return self._ask("Disconnect hardware", text)

    def confirm_quit_while_connected(self, disable_torque_on_disconnect: bool) -> bool:
        text = DISCONNECT_WARNING_TORQUE_OFF if disable_torque_on_disconnect else DISCONNECT_WARNING_KEEP
        return self._ask(
            "Quit — hardware is connected", "Quitting disconnects the hardware first.\n\n" + text
        )

    def confirm_adopt_rest_pose(self, text: str) -> bool:
        return self._ask("Adopt rest pose for this calibration", text)

    def confirm_play_behavior(self, text: str) -> bool:
        return self._ask("Play behavior on the REAL arm", text)

    def confirm_delete_behavior(self, name: str) -> bool:
        return self._ask("Delete behavior", f"Delete the behavior '{name}' file? This cannot be undone.")

    def confirm_mode_change(self, title: str, text: str) -> bool:
        return self._ask(title, text)

    def confirm_go_rest(self, text: str) -> bool:
        return self._ask("Go to rest", text)

    def confirm_move_to_pose(self, text: str) -> bool:
        return self._ask("Move to saved pose", text)

    def confirm_overlay(self, title: str, text: str) -> bool:
        return self._ask(title, text)

    def ask_behavior_details(self, default_name: str) -> tuple[str, str] | None:
        name, ok = QInputDialog.getText(
            self.parent, "Save behavior", "Name:", QLineEdit.EchoMode.Normal, default_name
        )
        if not ok or not name.strip():
            return None
        desc, ok = QInputDialog.getText(self.parent, "Save behavior", "Description (optional):")
        return (name.strip(), desc if ok else "")

    def choose_calibration_file(self, start_dir: str) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self.parent, "Select EXISTING LeRobot calibration file", start_dir, "Calibration (*.json)"
        )
        return path or None

    def choose_export_path(self) -> str | None:
        path, _ = QFileDialog.getSaveFileName(
            self.parent,
            "Export event log (redacted JSON Lines)",
            str(Path.home() / "so101_events.jsonl"),
            "JSON Lines (*.jsonl)",
        )
        return path or None

    def choose_mapping_path(self, default: str) -> str | None:
        path, _ = QFileDialog.getSaveFileName(
            self.parent,
            "Save 3D display mapping into config (TOML [simulation] section)",
            default,
            "TOML (*.toml)",
        )
        return path or None

    def info(self, title: str, text: str) -> None:
        QMessageBox.information(self.parent, title, text)

    def error(self, title: str, text: str) -> None:
        QMessageBox.critical(self.parent, title, text)

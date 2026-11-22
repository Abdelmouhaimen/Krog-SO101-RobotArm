"""Robot device contract (interfaces.md ``RobotPort``) and its error taxonomy.

Every exception raised by an adapter is one of the ``HardwareError`` subclasses below; the
controller treats ANY of them during hardware operation as a latched fault (FR-13/SR-04).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from so101_companion.devices.calibration import CalibrationData
from so101_companion.domain.joints import JointFrame


class HardwareError(RuntimeError):
    code = "HARDWARE_ERROR"
    # False when the error happened before the serial port was opened (nothing on the bus changed).
    port_touched = True


class ConnectError(HardwareError):
    code = "CONNECT_FAILED"


class ReadError(HardwareError):
    code = "READ_FAILED"


class WriteError(HardwareError):
    code = "WRITE_FAILED"


class TelemetryError(HardwareError):
    """Invalid data returned by the driver (missing keys, NaN...)."""

    code = "INVALID_TELEMETRY"


class CalibrationMismatchError(HardwareError):
    code = "CALIBRATION_MISMATCH"


class TorqueCommandError(HardwareError):
    code = "TORQUE_COMMAND_FAILED"


class UnsupportedDriverError(HardwareError):
    code = "UNSUPPORTED_DRIVER"
    port_touched = False


@runtime_checkable
class RobotPort(Protocol):
    """One physical (or fake) SO-101 follower. All calls are blocking and must be made from ONE thread."""

    @property
    def calibration(self) -> CalibrationData | None: ...

    @property
    def disable_torque_on_disconnect(self) -> bool: ...

    def initialize(self, calibration_path: Path, port: str) -> None:
        """Open the bus and configure motors (LeRobot ``connect(calibrate=False)``). May toggle torque.
        Only ever called after explicit operator confirmation."""

    def verify_calibration(self) -> bool:
        """True if the motors' stored calibration registers match the calibration file (read-only)."""

    def observe(self) -> JointFrame:
        """Measured positions (logical units). Raises ReadError/TelemetryError; never returns targets."""

    def send(self, targets: JointFrame) -> JointFrame:
        """Send a full six-joint position target; return what the driver ACTUALLY sent."""

    def torque_off(self) -> None: ...

    def enable_torque_holding_present(self) -> JointFrame:
        """Set goal := present position, then enable torque. Returns the measured pose used."""

    def disconnect(self) -> None: ...

    def describe(self) -> dict[str, str]: ...

"""Read-only validation of an EXISTING LeRobot SO follower calibration file, and derivation of the
physical angular bounds in LeRobot's logical degree convention. Never creates or writes calibration.

File format (LeRobot 0.4.4 ``Robot._save_calibration`` → ``draccus.dump(dict[str, MotorCalibration])``)::

    {"shoulder_pan": {"id": 1, "drive_mode": 0, "homing_offset": -1234,
                      "range_min": 812, "range_max": 3301}, ...}

Degree convention, verified in LeRobot 0.4.4 ``SerialMotorsBus._normalize`` (MotorNormMode.DEGREES)::

    mid = (range_min + range_max) / 2
    degrees = (raw - mid) * 360 / (MODEL_RESOLUTION["sts3215"] - 1)      # 4096 - 1 = 4095

so the calibrated physical span of an arm joint in the SAME degrees that ``send_action`` accepts is
``[(range_min - mid) * 360/4095, (range_max - mid) * 360/4095]``. This equals the SO-101-VR-Control
``_tighten_limits_from_calibration`` formula (``(rmin - mid) * 360.0 / STS3215_RESOLUTION`` with
``STS3215_RESOLUTION = 4095``), which is therefore *verified*, not blindly copied. Unlike that source,
a missing/invalid file is a hard failure (no URDF fallback) — CONSTITUTION "fail closed".
DEGREES normalization ignores ``drive_mode``; the gripper uses RANGE_0_100 (0..100, drive_mode
inverts it inside LeRobot), so only 0..100 is needed for the gripper here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from so101_companion.domain.joints import ARM_JOINTS, JOINT_NAMES
from so101_companion.safety import policy
from so101_companion.safety.bounds import LABEL_CALIBRATED, JointBounds, build_bounds, restrict

STS3215_RESOLUTION = 4096  # lerobot.motors.feetech.tables.MODEL_RESOLUTION["sts3215"]
STS3215_MAX_RAW = STS3215_RESOLUTION - 1
HOMING_OFFSET_MAX = 2047  # Homing_Offset is sign-magnitude with sign bit 11 (feetech tables)
EXPECTED_IDS = {name: i + 1 for i, name in enumerate(JOINT_NAMES)}  # LeRobot SOFollower motor ids
MIN_SPAN_RAW = 20  # defensive: a near-zero recorded range indicates a failed calibration run


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class MotorCal:
    id: int
    drive_mode: int
    homing_offset: int
    range_min: int
    range_max: int


@dataclass(frozen=True)
class CalibrationData:
    path: Path
    robot_id: str  # LeRobot robot id == file stem
    motors: dict[str, MotorCal]

    @property
    def calibration_dir(self) -> Path:
        return self.path.parent


def _int_field(motor: str, entry: dict[str, Any], key: str) -> int:
    if key not in entry:
        raise CalibrationError(f"{motor}: missing field {key!r}")
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalibrationError(f"{motor}.{key}: expected integer, got {value!r}")
    return value


def parse_calibration(data: Any, path: Path) -> CalibrationData:
    if not isinstance(data, dict):
        raise CalibrationError("calibration root must be a JSON object")
    names = set(data)
    if names != set(JOINT_NAMES):
        raise CalibrationError(
            f"calibration motors {sorted(names)} != expected {sorted(JOINT_NAMES)} (wrong robot type?)"
        )
    motors: dict[str, MotorCal] = {}
    for name in JOINT_NAMES:
        entry = data[name]
        if not isinstance(entry, dict):
            raise CalibrationError(f"{name}: entry must be an object")
        unknown = set(entry) - {"id", "drive_mode", "homing_offset", "range_min", "range_max"}
        if unknown:
            raise CalibrationError(f"{name}: unexpected fields {sorted(unknown)}")
        cal = MotorCal(**{k: _int_field(name, entry, k) for k in MotorCal.__dataclass_fields__})
        if cal.id != EXPECTED_IDS[name]:
            raise CalibrationError(f"{name}: id {cal.id} != expected SOFollower id {EXPECTED_IDS[name]}")
        if cal.drive_mode not in (0, 1):
            raise CalibrationError(f"{name}: drive_mode must be 0 or 1, got {cal.drive_mode}")
        if abs(cal.homing_offset) > HOMING_OFFSET_MAX:
            raise CalibrationError(f"{name}: homing_offset {cal.homing_offset} outside ±{HOMING_OFFSET_MAX}")
        if not 0 <= cal.range_min < cal.range_max <= STS3215_MAX_RAW:
            raise CalibrationError(
                f"{name}: invalid range [{cal.range_min}, {cal.range_max}] (need 0 <= min < max <= 4095)"
            )
        if cal.range_max - cal.range_min < MIN_SPAN_RAW:
            raise CalibrationError(
                f"{name}: recorded range span {cal.range_max - cal.range_min} is implausibly small"
            )
        motors[name] = cal
    return CalibrationData(path=path, robot_id=path.stem, motors=motors)


def load_calibration(path: Path | str) -> CalibrationData:
    p = Path(path).expanduser()
    if not str(path).strip():
        raise CalibrationError("no calibration file selected")
    if p.suffix != ".json":
        raise CalibrationError(f"{p}: LeRobot calibration files are <robot_id>.json")
    if not p.is_file():
        raise CalibrationError(f"calibration file not found: {p} (M1 never runs automatic calibration)")
    try:
        data = json.loads(p.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"{p}: unreadable/corrupt JSON: {exc}") from exc
    return parse_calibration(data, p.resolve())


def calibrated_degree_range(cal: MotorCal) -> tuple[float, float]:
    """Physical calibrated span in LeRobot DEGREES (same convention as send_action/get_observation)."""
    mid = (cal.range_min + cal.range_max) / 2.0
    lo = (cal.range_min - mid) * 360.0 / STS3215_MAX_RAW
    hi = (cal.range_max - mid) * 360.0 / STS3215_MAX_RAW
    assert math.isclose(lo, -hi)
    return lo, hi


def calibrated_bounds(calibration: CalibrationData, margin_deg: float) -> JointBounds:
    ranges = {n: calibrated_degree_range(calibration.motors[n]) for n in ARM_JOINTS}
    bounds = build_bounds(ranges, margin_deg, f"calibration {calibration.path.name}", LABEL_CALIBRATED)
    return restrict(bounds, policy.JOINT_RANGE_LIMITS, "owner-set usable range")


def calibration_fingerprint(calibration: CalibrationData) -> str:
    """Identity of the logical joint frame defined by a calibration (D5, ADR-014).

    SHA-256 over the six motors' calibration entries (id, drive_mode, homing_offset, range_min,
    range_max) in canonical order. Any recalibration — e.g. the ADR-012 wrist re-centring, which kept
    the same robot id — changes it, so poses recorded under another calibration are never reused
    silently. The file path and robot id are deliberately NOT part of it.
    """
    import hashlib

    canonical = {
        name: [m.id, m.drive_mode, m.homing_offset, m.range_min, m.range_max]
        for name, m in ((n, calibration.motors[n]) for n in JOINT_NAMES)
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode())
    return "sha256:" + digest.hexdigest()


def default_calibration_hint() -> Path:
    """Where SO-101-VR-Control / LeRobot 0.4.4 keep follower calibrations (display hint only; never
    auto-selected): $HF_LEROBOT_CALIBRATION or ~/.cache/huggingface/lerobot/calibration, then
    robots/so_follower/<id>.json."""
    import os

    base = os.environ.get("HF_LEROBOT_CALIBRATION")
    if base:
        return Path(base).expanduser() / "robots" / "so_follower"
    home = os.environ.get("HF_LEROBOT_HOME")
    root = Path(home).expanduser() if home else Path("~/.cache/huggingface/lerobot").expanduser()
    return root / "calibration" / "robots" / "so_follower"

"""Application configuration: TOML file -> validated, immutable dataclasses.

Defaults equal the approved M1 decisions (milestone-1/contracts/config.example.toml). Unknown keys
and the explicitly rejected auto-connect/auto-enable switches are errors, not silently ignored.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from so101_companion.domain.joints import ARM_JOINTS


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppSection:
    command_hz: float = 20.0
    render_target_fps: float = 30.0
    observe_every_n_ticks: int = 1  # actual-position reads per issued tick in hardware modes
    stale_after_s: float = 0.5
    event_ring_size: int = 20000


@dataclass(frozen=True)
class HardwareSection:
    port: str = ""  # MUST be explicitly chosen by the operator; never auto-discovered
    calibration_path: str = ""  # existing LeRobot calibration JSON; never auto-created
    allowed_lerobot_versions: tuple[str, ...] = ("0.4.4",)


GripperEnd = Literal["model_min", "model_max"]


@dataclass(frozen=True)
class SimulationSection:
    model_path: str = ""  # empty -> pinned vendor copy
    # PROVISIONAL (ADR-006): LeRobot gripper 0 (closed) -> official model's closed end (jnt_range min).
    logical_gripper_0_maps_to: GripperEnd = "model_min"
    # PROVISIONAL (ADR-006): model angle [deg] = sign * logical_deg + offset_deg. Defaults are the
    # identity because both LeRobot DEGREES and the SO101 "new calib" model put zero at mid-range.
    joint_sign: Mapping[str, float] = field(default_factory=lambda: dict.fromkeys(ARM_JOINTS, 1.0))
    joint_offset_deg: Mapping[str, float] = field(default_factory=lambda: dict.fromkeys(ARM_JOINTS, 0.0))
    # LeRobot 0.4.4 SOFollower.calibrate(): full_turn_motor = "wrist_roll" (range 0..4095).
    full_turn_joints: tuple[str, ...] = ("wrist_roll",)


@dataclass(frozen=True)
class ApiSection:
    """Milestone 2.5 HTTP API (ADR-018). Off unless enabled here or with ``--api``."""

    enabled: bool = False
    host: str = "127.0.0.1"  # loopback only unless allow_remote = true
    port: int = 8765
    allow_remote: bool = False
    # Authenticated API requests may move the REAL arm (when the controller is ready). Off by default:
    # in Simulation Only the API still moves the 3D arm; with a connected arm motion requests are refused.
    allow_physical_control: bool = False
    token_file: str = ""  # "" -> ~/.config/so101-companion/api-tokens.json (outside the repository)
    ack_timeout_s: float = 0.25  # a POST waits at most this long for the controller's verdict
    stream_max_fps: float = 5.0  # MJPEG cap (hard upper limit 5)
    frame_width: int = 640
    frame_height: int = 480
    jpeg_quality: int = 80


LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class AppConfig:
    app: AppSection = field(default_factory=AppSection)
    hardware: HardwareSection = field(default_factory=HardwareSection)
    simulation: SimulationSection = field(default_factory=SimulationSection)
    api: ApiSection = field(default_factory=ApiSection)
    source: str = "<defaults>"


REJECTED_KEYS = {
    ("app", "hardware_autoconnect"),
    ("app", "hardware_autoenable"),
    ("app", "mode_on_start"),
}


def _section(cls: type[Any], name: str, raw: Mapping[str, Any]) -> Any:
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if (name, key) in REJECTED_KEYS:
            if value in (False, "SIM_ONLY"):
                continue  # explicitly restating the safe default is harmless
            raise ConfigError(
                f"[{name}].{key}={value!r} is not supported: the app always starts in SIM_ONLY and never "
                "connects or enables movement without explicit operator action"
            )
        if key not in known:
            raise ConfigError(f"Unknown config key [{name}].{key}")
        kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"[{name}]: {exc}") from exc


def _positive(name: str, value: Any, *, upper: float | None = None, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{name} must be {'>= 0' if allow_zero else '> 0'}, got {value}")
    if upper is not None and value > upper:
        raise ConfigError(f"{name} must be <= {upper}, got {value}")
    return float(value)


def validate(cfg: AppConfig) -> AppConfig:
    a, h, sim = cfg.app, cfg.hardware, cfg.simulation
    validate_api(cfg.api)
    _positive("app.command_hz", a.command_hz, upper=100.0)
    _positive("app.render_target_fps", a.render_target_fps, upper=240.0)
    if not isinstance(a.observe_every_n_ticks, int) or a.observe_every_n_ticks < 1:
        raise ConfigError("app.observe_every_n_ticks must be an integer >= 1")
    _positive("app.stale_after_s", a.stale_after_s, upper=60.0)
    if not isinstance(a.event_ring_size, int) or a.event_ring_size < 100:
        raise ConfigError("app.event_ring_size must be an integer >= 100")
    if not isinstance(h.port, str) or not isinstance(h.calibration_path, str):
        raise ConfigError("hardware.port and hardware.calibration_path must be strings")
    if not h.allowed_lerobot_versions or not all(isinstance(v, str) for v in h.allowed_lerobot_versions):
        raise ConfigError("hardware.allowed_lerobot_versions must be a non-empty list of strings")
    if not set(sim.full_turn_joints) <= set(ARM_JOINTS):
        raise ConfigError("simulation.full_turn_joints must be arm joint names")
    if sim.logical_gripper_0_maps_to not in ("model_min", "model_max"):
        raise ConfigError("simulation.logical_gripper_0_maps_to must be 'model_min' or 'model_max'")
    for table_name, table, check in (
        ("joint_sign", sim.joint_sign, lambda v: v in (1.0, -1.0)),
        ("joint_offset_deg", sim.joint_offset_deg, lambda v: math.isfinite(v) and abs(v) <= 180.0),
    ):
        if set(table) != set(ARM_JOINTS):
            raise ConfigError(f"simulation.{table_name} must define exactly the five arm joints")
        for joint, value in table.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not check(float(value)):
                raise ConfigError(f"simulation.{table_name}.{joint} has invalid value {value!r}")
    return cfg


def validate_api(api: ApiSection) -> None:
    for name in ("enabled", "allow_remote", "allow_physical_control"):
        if not isinstance(getattr(api, name), bool):
            raise ConfigError(f"api.{name} must be true or false")
    if not isinstance(api.host, str) or not api.host:
        raise ConfigError("api.host must be a non-empty string")
    if api.host not in LOOPBACK_HOSTS and not api.allow_remote:
        raise ConfigError(
            f"api.host = {api.host!r} is not a loopback address: remote access is disabled unless "
            "api.allow_remote = true"
        )
    if isinstance(api.port, bool) or not isinstance(api.port, int) or not 1 <= api.port <= 65535:
        raise ConfigError("api.port must be an integer 1..65535")
    if not isinstance(api.token_file, str):
        raise ConfigError("api.token_file must be a string")
    _positive("api.ack_timeout_s", api.ack_timeout_s, upper=5.0, allow_zero=True)
    _positive("api.stream_max_fps", api.stream_max_fps, upper=5.0)
    for name, lo, hi in (("frame_width", 64, 1920), ("frame_height", 64, 1080), ("jpeg_quality", 10, 95)):
        v = getattr(api, name)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ConfigError(f"api.{name} must be an integer {lo}..{hi}")


def load_config(path: Path | None) -> AppConfig:
    if path is None:
        return validate(AppConfig())
    try:
        raw = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    if "safety" in raw:
        raise ConfigError(
            "[safety] is not configurable: the M1 control/safety policy is fixed "
            "(so101_companion/safety/policy.py, decisions/ADR-010)"
        )
    if "disable_torque_on_disconnect" in raw.get("hardware", {}):
        raise ConfigError("hardware.disable_torque_on_disconnect is fixed by the M1 policy (ADR-010)")
    unknown = set(raw) - {"app", "hardware", "simulation", "api"}
    if unknown:
        raise ConfigError(f"Unknown config sections: {sorted(unknown)}")
    hw_raw = dict(raw.get("hardware", {}))
    if "allowed_lerobot_versions" in hw_raw:
        hw_raw["allowed_lerobot_versions"] = tuple(hw_raw["allowed_lerobot_versions"])
    sim_raw = dict(raw.get("simulation", {}))
    if "full_turn_joints" in sim_raw:
        sim_raw["full_turn_joints"] = tuple(sim_raw["full_turn_joints"])
    for key in ("joint_sign", "joint_offset_deg"):
        if key in sim_raw:
            sim_raw[key] = MappingProxyType({k: float(v) for k, v in sim_raw[key].items()})
    cfg = AppConfig(
        app=_section(AppSection, "app", raw.get("app", {})),
        hardware=_section(HardwareSection, "hardware", hw_raw),
        simulation=_section(SimulationSection, "simulation", sim_raw),
        api=_section(ApiSection, "api", raw.get("api", {})),
        source=str(path),
    )
    return validate(cfg)


def simulation_section_toml(sim: SimulationSection) -> str:
    def table(values: Mapping[str, float]) -> str:
        return "{ " + ", ".join(f"{k} = {float(values[k]):.1f}" for k in ARM_JOINTS) + " }"

    return (
        "[simulation]\n"
        f'model_path = "{sim.model_path}"\n'
        f'logical_gripper_0_maps_to = "{sim.logical_gripper_0_maps_to}"\n'
        f"joint_sign = {table(sim.joint_sign)}\n"
        f"joint_offset_deg = {table(sim.joint_offset_deg)}\n"
        "full_turn_joints = [" + ", ".join(f'"{j}"' for j in sim.full_turn_joints) + "]\n"
    )


def write_simulation_section(path: Path, sim: SimulationSection) -> None:
    """Replace (or append) the [simulation] section of a TOML config, then re-validate the file.
    The original text is restored if the result does not load."""
    import re

    path = Path(path)
    original = path.read_text() if path.exists() else ""
    section = simulation_section_toml(sim)
    pattern = re.compile(r"^\[simulation\][^\n]*\n(?:(?!^\[).*\n?)*", re.M)
    if pattern.search(original):
        updated = pattern.sub(lambda _m: section + "\n", original, count=1)
    else:
        updated = original.rstrip("\n") + ("\n\n" if original.strip() else "") + section
    path.write_text(updated)
    try:
        load_config(path)
    except ConfigError:
        path.write_text(original)
        raise

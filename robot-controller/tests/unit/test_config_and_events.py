"""FR-08 configurable/validated limits; FR-17 structured, redacted logging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from so101_companion.config import AppConfig, ConfigError, load_config
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.telemetry.events import EventLog, redact

ROOT = Path(__file__).resolve().parents[2]


def test_defaults_and_no_safety_configuration_surface() -> None:
    cfg = load_config(None)
    assert not hasattr(cfg, "safety")  # fixed policy: safety/policy.py (ADR-010)
    assert cfg.app.command_hz == 20
    assert cfg.hardware.port == "" and cfg.hardware.calibration_path == ""
    assert cfg.hardware.allowed_lerobot_versions == ("0.4.4",)
    assert not hasattr(cfg.hardware, "disable_torque_on_disconnect")


def test_shipped_default_toml_equals_builtin_defaults() -> None:
    shipped = load_config(ROOT / "config" / "default.toml")
    builtin = AppConfig()
    assert shipped.app == builtin.app
    assert shipped.hardware == builtin.hardware
    assert dict(shipped.simulation.joint_sign) == dict(builtin.simulation.joint_sign)
    assert dict(shipped.simulation.joint_offset_deg) == dict(builtin.simulation.joint_offset_deg)


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(text)
    return p


@pytest.mark.parametrize(
    "text",
    [
        "[safety]\nmax_relative_target_arm_deg = 15.0\n",
        "[safety]\nangular_margin_deg = 5.0\n",
        "[safety]\n",
        "[hardware]\ndisable_torque_on_disconnect = false\n",
    ],
)
def test_safety_policy_is_not_configurable(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match=r"fixed|not configurable"):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize(
    "text",
    [
        "[app]\ncommand_hz = 0\n",
        "[app]\nhardware_autoconnect = true\n",
        "[app]\nhardware_autoenable = true\n",
        '[app]\nmode_on_start = "SIM_TO_REAL"\n',
        "[app]\nunknown_key = 1\n",
        "[robot]\nx = 1\n",
        '[simulation]\nlogical_gripper_0_maps_to = "open"\n',
        "[simulation]\njoint_sign = { shoulder_pan = 2.0 }\n",
        "not toml ===",
    ],
)
def test_invalid_configs_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_restating_safe_defaults_is_allowed(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, '[app]\nhardware_autoconnect = false\nmode_on_start = "SIM_ONLY"\n'))
    assert cfg.app.command_hz == 20


def test_event_log_ring_kinds_and_export_redaction(tmp_path: Path) -> None:
    log = EventLog(ring_size=100, clock=lambda: 1.0)
    log.emit("startup", mode="SIM_ONLY")
    home = str(Path.home())
    log.emit(
        "connect_confirmation",
        port="/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A46083062-if00",
        calibration_path=f"{home}/.cache/huggingface/lerobot/calibration/robots/so_follower/left_follower.json",
    )
    for i in range(3):
        log.emit("control_tick", seq=i)
    assert [r["kind"] for r in log.records(exclude_ticks=True)] == ["startup", "connect_confirmation"]
    out = tmp_path / "log.jsonl"
    assert log.export_jsonl(out, include_ticks=False) == 2
    text = out.read_text()
    assert "5A46083062" not in text and home not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[1]["port"] == "/dev/serial/by-id/<redacted>"
    assert rows[1]["calibration_path"].startswith("~/.cache")
    assert all("t_monotonic_seconds" in r and "event_seq" in r for r in rows)
    assert log.export_jsonl(out, include_ticks=True) == 5


def test_ring_is_bounded() -> None:
    log = EventLog(ring_size=100)
    for i in range(250):
        log.emit("x", i=i)
    records = log.records()
    assert len(records) == 100 and records[0]["i"] == 150


def test_redact_nested() -> None:
    assert redact({"a": ["/home/u/x", "/dev/serial/by-path/pci-0:1"]}, home="/home/u") == {
        "a": ["~/x", "/dev/serial/by-path/<redacted>"]
    }


def test_stream_to_appends_redacted_lines_including_ticks(tmp_path: Path) -> None:
    log = EventLog(ring_size=100, clock=lambda: 1.0)
    log.emit("startup")
    out = tmp_path / "live.jsonl"
    log.stream_to(out)
    log.emit("control_tick", seq=1)
    log.emit("connect_confirmation", port="/dev/serial/by-id/usb-1a86_X_123-if00")
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["startup", "control_tick", "connect_confirmation"]
    assert rows[2]["port"] == "/dev/serial/by-id/<redacted>"


def test_write_simulation_section_replaces_and_revalidates(tmp_path: Path) -> None:
    from dataclasses import replace

    from so101_companion.config import write_simulation_section

    cfg_path = tmp_path / "hw.toml"
    cfg_path.write_text(
        '[hardware]\nport = "/dev/x"\n\n[simulation]\nmodel_path = ""\n\n[app]\ncommand_hz = 20\n'
    )
    sim = replace(
        load_config(None).simulation,
        joint_offset_deg={**dict.fromkeys(JOINT_NAMES[:5], 0.0), "wrist_roll": -101.8},
        logical_gripper_0_maps_to="model_min",
    )
    write_simulation_section(cfg_path, sim)
    cfg = load_config(cfg_path)
    assert cfg.simulation.joint_offset_deg["wrist_roll"] == -101.8
    assert cfg.hardware.port == "/dev/x" and cfg.app.command_hz == 20
    assert cfg_path.read_text().count("[simulation]") == 1
    other = tmp_path / "new.toml"
    write_simulation_section(other, sim)  # creates the file
    assert load_config(other).simulation.full_turn_joints == ("wrist_roll",)

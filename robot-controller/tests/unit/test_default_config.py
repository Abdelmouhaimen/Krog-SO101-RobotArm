"""``so101-companion`` without ``--config`` uses the machine's gitignored ``config/local.toml``."""

from __future__ import annotations

from pathlib import Path

import pytest

from so101_companion import app


def test_tests_never_pick_up_the_local_config() -> None:
    assert app.default_config_path() is None  # conftest sets SO101_COMPANION_CONFIG=""


def test_local_toml_in_the_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(app.LOCAL_CONFIG_ENV)
    local = tmp_path / "config" / "local.toml"
    local.parent.mkdir()
    local.write_text("[api]\nenabled = true\n")
    monkeypatch.chdir(tmp_path)
    assert app.default_config_path() == Path.cwd() / "config" / "local.toml"


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other.toml"
    monkeypatch.setenv(app.LOCAL_CONFIG_ENV, str(other))
    assert app.default_config_path() == other


def test_local_config_enables_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "local.toml"
    local.write_text("[api]\nenabled = true\nallow_physical_control = true\n")
    monkeypatch.setenv(app.LOCAL_CONFIG_ENV, str(local))
    args = app.build_parser().parse_args([])
    cfg = app.api_overrides(args, app.load_config(app.default_config_path()))
    assert cfg.api.enabled and cfg.api.allow_physical_control and cfg.api.host == "127.0.0.1"

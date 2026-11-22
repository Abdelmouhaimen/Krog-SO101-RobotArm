"""Configuration, CLI and repository hygiene: A10 config, A20 secrets, A25 obsolete code, A26 startup."""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from so101_voice.__main__ import main
from so101_voice.config import AppConfig, ConfigError, load_config, load_dotenv, load_secrets

from .fake_providers import DG_KEY, OA_KEY, FakeFlux, FakeFluxTTS, FakeOpenAI, say, until
from .rig import Rig

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "so101_voice"


# ------------------------------------------------------------------ configuration
def test_default_config_is_the_custom_pipeline():
    cfg = load_config()
    assert (cfg.stt.provider, cfg.stt.model, cfg.stt.url) == (
        "deepgram",
        "flux-general-en",
        "wss://api.deepgram.com/v2/listen",
    )
    assert (cfg.stt.sample_rate, cfg.stt.send_chunk_ms) == (16000, 80)
    assert (cfg.llm.provider, cfg.llm.model, cfg.llm.reasoning_effort, cfg.llm.stream) == (
        "openai",
        "gpt-6-luna",
        "none",
        True,
    )
    assert (cfg.tts.provider, cfg.tts.model, cfg.tts.sample_rate) == ("deepgram", "flux-cliff-en", 24000)
    assert cfg.input.mode == "continuous" and cfg.stt.keyterms == []  # no paid add-on by default
    assert (
        cfg.cost.enabled and cfg.cost.max_session_estimated_usd == 0.25 and cfg.cost.warn_at_fraction == 0.8
    )
    p = cfg.cost.prices
    assert (
        p.verified_on == "2026-11-11"
        and p.stt_source.startswith("https://")
        and p.llm_source.startswith("https://")
    )


def test_obsolete_voice_agent_config_is_rejected_with_guidance(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/default.toml").write_text('[deepgram]\nthink_model = "gpt-4.1-mini"\n')
    with pytest.raises(ConfigError, match=r"obsolete \[deepgram\].*\[stt\], \[llm\] and \[tts\]"):
        load_config(root=tmp_path)
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"llm": {"think_model": "x"}})  # unknown keys are errors


def test_config_validation_rules():
    with pytest.raises(ValidationError, match="temperature"):
        AppConfig.model_validate({"llm": {"reasoning_effort": "low", "temperature": 0.5}})
    with pytest.raises(ValidationError, match="prices"):
        AppConfig.model_validate({"llm": {"model": "gpt-6.1-terra"}})  # price table must match the model
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"stt": {"sample_rate": 22050}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"llm": {"stream": False}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"robot": {"lease_s": 1.0, "renew_interval_s": 0.8}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"robot": {"lease_s": 1.0, "renew_interval_s": 0.5, "stop_grace_s": 0.5}})
    terra = AppConfig.model_validate(
        {
            "llm": {"model": "gpt-6.1-terra"},
            "cost": {"prices": {"llm_model": "gpt-6.1-terra", "llm_input_usd_per_mtok": 1.0}},
        }
    )
    assert terra.llm.model == "gpt-6.1-terra"


def test_secrets_come_from_env_or_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DEEPGRAM_API_KEY=dg\nexport OPENAI_API_KEY='sk'\n# comment\n")
    s = load_secrets(env)
    assert (s.deepgram_api_key, s.openai_api_key, s.so101_api_token) == ("dg", "sk", None)
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    load_dotenv(env)  # existing environment variables win
    import os

    assert os.environ["OPENAI_API_KEY"] == "from-env"


# ------------------------------------------------------------------ CLI / startup (A26)
def test_cli_requires_both_provider_keys(capsys, monkeypatch):
    import so101_voice.__main__ as cli
    from so101_voice.config import Secrets

    none = Secrets(deepgram_api_key=None, openai_api_key=None, so101_api_token=None)
    monkeypatch.setattr(cli, "load_secrets", lambda: none)  # never read the developer's real .env here
    assert main(["--plain", "--no-robot"]) == 2
    assert "DEEPGRAM_API_KEY is not set" in capsys.readouterr().err
    monkeypatch.setattr(cli, "load_secrets", lambda: none.model_copy(update={"deepgram_api_key": "x"}))
    assert main(["--plain", "--no-robot"]) == 2
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().err


def test_cli_print_request_shows_the_responses_body(capsys):
    assert main(["--print-request"]) == 0
    body = capsys.readouterr().out
    assert '"model": "gpt-6-luna"' in body and '"store": false' in body and '"effort": "none"' in body
    assert "agent" not in body.lower() or "Settings" not in body


def test_cli_rejects_obsolete_config_file(tmp_path, capsys):
    f = tmp_path / "old.toml"
    f.write_text('[deepgram]\nvoice = "flux-cliff-en"\n')
    assert main(["--config", str(f), "--print-request"]) == 2
    assert "obsolete [deepgram]" in capsys.readouterr().err


async def test_app_starts_with_only_the_custom_pipeline():
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        rig = Rig(flux, aura, FakeOpenAI([say("Hi.")]))
        comp = rig.companion
        from so101_voice.llm.responses import ResponsesClient

        assert isinstance(comp.llm, ResponsesClient)
        assert await rig.start()
        from so101_voice.stt.flux import FluxSTT
        from so101_voice.tts.flux import FluxTTS

        assert isinstance(rig.conv.stt, FluxSTT) and isinstance(rig.conv.tts, FluxTTS)
        await flux.wait_connections(1)
        await until(lambda: len(aura.conns) == 1, what="TTS pre-connected")
        await rig.close()


# ------------------------------------------------------------------ A20: no credentials in logs or commits
async def test_api_keys_never_appear_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        rig = Rig(flux, aura, FakeOpenAI([say("Hello there. "), say("Again.")]))
        assert await rig.start()
        await flux.wait_connections(1)
        await flux.utter("hello")
        await until(lambda: rig.conv.generations_started == 1 and rig.conv._gen is None, 5)
        await rig.close()
    text = caplog.text + "\n".join(str(e) for e in rig.events)
    assert DG_KEY not in text and OA_KEY not in text


def test_no_credentials_in_tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    files = [REPO / f for f in out.split() if (REPO / f).is_file()]
    assert not any(f.name == ".env" for f in files)
    secret = re.compile(r"(sk-(proj-)?[A-Za-z0-9_-]{32,}|\b[0-9a-f]{40}\b|so101_[A-Za-z0-9_-]{24,})")
    for f in files:
        if f.suffix in {".py", ".toml", ".md", ".txt", ".json", ".example", ".cfg", ".ini", ".yml", ".yaml"}:
            assert not secret.search(f.read_text(errors="ignore")), f"possible credential in {f}"


# ------------------------------------------------------------------ A25: obsolete managed Voice Agent removed
def test_obsolete_voice_agent_code_and_dependencies_are_gone():
    assert not (SRC / "deepgram").exists() or not any((SRC / "deepgram").glob("*.py"))
    assert not (SRC / "session.py").exists()
    banned = (
        "agent.deepgram.com",
        "agent/converse",
        "SettingsApplied",
        "AgentAudioDone",
        "FunctionCallRequest",
        "think_model",
    )
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        for word in banned:
            assert word not in text, f"{path.name} still references {word!r}"
    for path in [REPO / "config/default.toml", REPO / "pyproject.toml"]:
        text = path.read_text()
        assert "agent.deepgram.com" not in text and "[deepgram]" not in text and "deepgram-sdk" not in text
    deps = re.search(r"dependencies = \[(.*?)\]", (REPO / "pyproject.toml").read_text(), re.S)
    assert deps is not None
    names = sorted(re.findall(r'"([a-zA-Z0-9_-]+)', deps.group(1)))
    assert names == ["httpx", "miniaudio", "pydantic", "textual", "websockets"]


def test_every_module_is_used():
    """No dead modules: every package module is imported somewhere else (or is an entry point)."""
    mods = {p.relative_to(SRC.parent).with_suffix("").as_posix().replace("/", ".") for p in SRC.rglob("*.py")}
    imported: set[str] = set()
    for p in list(SRC.rglob("*.py")) + list((REPO / "tests").glob("*.py")):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
    # robot.actions is the documented PLANNED physical-action contract (future milestone), not dead code.
    planned = {"so101_voice.__main__", "so101_voice.robot.actions"}
    unused = {m for m in mods if not m.endswith("__init__") and m not in imported and m not in planned}
    assert unused == set()

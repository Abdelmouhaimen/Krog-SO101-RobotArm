"""Audio modes: PipeWire AEC manager (with fake processes), failure policies, half duplex.

The real acoustic behaviour (echo reduction on this machine's speakers and microphone) is measured by
``scripts/aec_check.py`` and ``scripts/aec_pipeline_check.py``. These tests cover the logic offline.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from so101_voice.audio.aec import (
    SINK_NODE,
    SOURCE_DESCRIPTION,
    SOURCE_NODE,
    AECSettings,
    AECUnavailable,
    AudioNode,
    PipeWireAEC,
    render_config,
    resolve_node,
)
from so101_voice.audio.playback import PlaybackState
from so101_voice.conversation import Notice, Phase

from .fake_providers import FakeFlux, FakeFluxTTS, FakeOpenAI, say, until
from .rig import Rig


# ------------------------------------------------------------------ configuration rendering
def test_monitor_topology_references_the_speaker_monitor():
    conf = render_config(AECSettings(topology="monitor", speaker_node="alsa_out", mic_node="alsa_in"))
    assert "library.name = aec/libspa-aec-webrtc" in conf and "monitor.mode = true" in conf
    assert "stream.capture.sink = true" in conf and 'target.object = "alsa_out"' in conf
    assert 'node.name = "so101_ec_capture" node.passive = true target.object = "alsa_in"' in conf
    assert "webrtc.delay_agnostic = true" in conf and "webrtc.extended_filter = true" in conf
    assert "webrtc.gain_control = false" in conf  # measured default (AGC lifted residual echo)
    assert "default.audio" not in conf and "metadata" not in conf  # never touches system defaults


def test_sink_topology_creates_a_virtual_sink():
    conf = render_config(AECSettings(topology="sink", noise_suppression=False))
    assert "monitor.mode = false" in conf and "stream.capture.sink" not in conf
    assert f'node.name = "{SINK_NODE}"' in conf and "webrtc.noise_suppression = false" in conf


def test_resolve_node_by_name_or_description():
    nodes = [
        AudioNode(1, "Audio/Source", "alsa_input.mic", "sof-hda-dsp Digital Microphone"),
        AudioNode(2, "Audio/Sink", "alsa_output.spk", "sof-hda-dsp Speaker + Headphones"),
    ]
    assert resolve_node(None, "Audio/Source", nodes) is None
    assert resolve_node("default", "Audio/Sink", nodes) is None
    assert resolve_node("alsa_input.mic", "Audio/Source", nodes) == "alsa_input.mic"
    assert (
        resolve_node("digital mic", "Audio/Source", nodes) == "alsa_input.mic"
    )  # case-insensitive substring
    assert resolve_node("Digital Microphone", "Audio/Source", nodes) == "alsa_input.mic"
    with pytest.raises(AECUnavailable, match="Available"):
        resolve_node("USB headset", "Audio/Source", nodes)


# ------------------------------------------------------------------ process lifecycle (fakes)
class FakeProc:
    def __init__(self, exit_code: int | None = None, stderr: str = "") -> None:
        self.returncode = exit_code
        self.pid = 4242
        self.terminated = False

        class _Err:
            def read(self_inner) -> str:
                return stderr

        self.stderr = _Err()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.returncode = -9


def make_aec(
    tmp_path, *, proc: FakeProc, nodes_after: int = 0, topology="monitor"
) -> tuple[PipeWireAEC, list]:
    calls: list = []
    state = {"polls": 0}

    def list_nodes() -> list[AudioNode]:
        state["polls"] += 1
        if proc.returncode is None and state["polls"] > nodes_after + 1:  # +1: the stale check
            return [
                AudioNode(9, "Audio/Source", SOURCE_NODE, SOURCE_DESCRIPTION),
                AudioNode(10, "Audio/Sink", SINK_NODE, "x"),
            ]
        return []

    def popen(cmd, **kw):
        calls.append(cmd)
        return proc

    return PipeWireAEC(
        AECSettings(topology=topology), popen=popen, list_nodes=list_nodes, runtime_dir=tmp_path
    ), calls


def test_aec_start_waits_for_nodes_and_stop_cleans_up(tmp_path):
    proc = FakeProc()
    aec, calls = make_aec(tmp_path, proc=proc, nodes_after=2)
    nodes = aec.start(timeout_s=2)
    assert calls[0][:2] == ["pipewire", "-c"] and aec.config_path.exists()
    assert nodes.source_description == SOURCE_DESCRIPTION and nodes.sink_node is None  # monitor topology
    aec.stop()
    assert proc.terminated and not aec.config_path.exists() and not aec.running


def test_aec_process_exit_and_timeout_are_reported(tmp_path):
    aec, _ = make_aec(tmp_path, proc=FakeProc(exit_code=1, stderr="module-echo-cancel: failed to load"))
    with pytest.raises(AECUnavailable, match="failed to load"):
        aec.start(timeout_s=1)
    proc = FakeProc()
    aec2, _ = make_aec(tmp_path, proc=proc, nodes_after=10_000)
    with pytest.raises(AECUnavailable, match="did not appear"):
        aec2.start(timeout_s=0.3)
    assert proc.terminated  # no orphan process


def test_aec_refuses_when_another_instance_runs(tmp_path):
    def nodes() -> list[AudioNode]:
        return [AudioNode(9, "Audio/Source", SOURCE_NODE, SOURCE_DESCRIPTION)]

    aec = PipeWireAEC(AECSettings(), popen=lambda *a, **k: FakeProc(), list_nodes=nodes, runtime_dir=tmp_path)
    with pytest.raises(AECUnavailable, match="already"):
        aec.start(timeout_s=1)


def test_missing_pipewire_binary_is_reported(tmp_path):
    def popen(*a, **k):
        raise FileNotFoundError("pipewire")

    aec = PipeWireAEC(AECSettings(), popen=popen, list_nodes=list, runtime_dir=tmp_path)
    with pytest.raises(AECUnavailable, match="cannot start pipewire"):
        aec.start(timeout_s=1)


def test_probe_reports_missing_pieces(monkeypatch):
    import so101_voice.audio.aec as mod

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod, "MODULE_DIRS", ())
    monkeypatch.setattr(mod, "SPA_DIRS", ())
    problems = mod.probe()
    assert any("pipewire" in p for p in problems) and any("echo-cancel" in p for p in problems)
    assert any("WebRTC" in p for p in problems)


# ------------------------------------------------------------------ failure policies (no silent downgrade)
def failing_aec(tmp_path) -> PipeWireAEC:
    def popen(*a, **k):
        raise FileNotFoundError("pipewire")

    return PipeWireAEC(AECSettings(), popen=popen, list_nodes=list, runtime_dir=tmp_path)


def rig_with_aec(flux, aura, tmp_path, policy: str, **over) -> Rig:
    rig = Rig(flux, aura, FakeOpenAI([say("Hello.")]), audio={"prebuffer_ms": 20, **over})
    rig.cfg.audio.aec.on_failure = policy  # type: ignore[assignment]
    rig.companion.aec = failing_aec(tmp_path)
    return rig


async def test_aec_failure_with_error_policy_refuses_to_start(tmp_path):
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        rig = rig_with_aec(flux, aura, tmp_path, "error")
        assert not await rig.start()
        assert rig.conv.phase is Phase.ERROR and "echo cancellation unavailable" in (
            rig.conv.last_error or ""
        )
        assert "on_failure" in rig.conv.last_error and flux.conns == []  # nothing paid was started
        await rig.close()


@pytest.mark.parametrize("policy", ["raw", "half_duplex"])
async def test_aec_failure_with_explicit_fallback_warns_loudly(tmp_path, policy):
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        rig = rig_with_aec(flux, aura, tmp_path, policy)
        assert await rig.start()
        warn = [e.text for e in rig.events if isinstance(e, Notice) and e.level == "warning"]
        assert any("echo cancellation unavailable" in w and policy.upper() in w for w in warn)
        assert rig.conv.audio_mode == policy and rig.conv.half_duplex == (policy == "half_duplex")
        await rig.close()


def test_aec_sets_the_echo_cancelled_devices(tmp_path):
    from so101_voice.app import build_companion
    from so101_voice.config import AppConfig, Secrets

    cfg = AppConfig.model_validate({"log_file": None, "cost": {"ledger_file": None}})
    comp = build_companion(
        cfg,
        Secrets(deepgram_api_key="k", openai_api_key="k", so101_api_token=None),
        on_event=print,
        use_robot=False,
    )
    assert comp.mic_io is not None and comp.aec is not None
    assert comp.mic_io.input_device is None  # replaced by the echo-cancelled mic once AEC runs
    proc = FakeProc()
    comp.aec, _ = make_aec(tmp_path, proc=proc, topology="monitor")
    asyncio.run(comp.prepare_audio())
    assert comp.mic_io.input_device == SOURCE_DESCRIPTION and comp.mic_io.output_device is None
    assert comp.conversation.audio_mode == "aec"
    comp.aec.stop()


# ------------------------------------------------------------------ half duplex (fallback mode)
async def test_half_duplex_ignores_turns_while_the_assistant_is_audible():
    async with FakeFlux() as flux, FakeFluxTTS(frame_delay_s=0.005) as aura:
        oai = FakeOpenAI(
            [say("A fairly long answer that plays for a little while, on and on and on. " * 2), say("Sure.")]
        )
        rig = Rig(
            flux, aura, oai, audio={"mode": "half_duplex", "prebuffer_ms": 20, "half_duplex_tail_ms": 200}
        )
        assert await rig.start()
        await flux.wait_connections(1)
        await until(lambda: rig.conv.phase is Phase.LISTENING)
        assert any(isinstance(e, Notice) and "HALF-DUPLEX" in e.text for e in rig.events)
        await flux.utter("Tell me something")
        await until(lambda: rig.playback.state is PlaybackState.PLAYING, 3.0)
        await flux.turn("StartOfTurn", "stop")  # the microphone may hear the assistant: ignored
        await flux.turn("EndOfTurn", "stop please")
        await asyncio.sleep(0.2)
        assert rig.conv.interruptions == 0 and len(oai.requests) == 1 and rig.conv.half_duplex_ignored == 1
        await until(lambda: rig.conv._gen is None and rig.conv.phase is Phase.LISTENING, 8.0)
        await asyncio.sleep(0.3)  # past the tail
        await flux.utter("Thanks")
        await until(lambda: len(oai.requests) == 2, 2.0, "turn accepted after playback")
        await rig.close()


def test_real_pipewire_aec_smoke():
    """Starts the real echo canceller if this machine has it (silent: no sound is played)."""
    from so101_voice.audio.aec import probe

    if probe():
        pytest.skip("PipeWire AEC not available here")
    if SOURCE_NODE in subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout:
        pytest.skip("so101-voice is running: its echo canceller exists (a second one is refused by design)")
    aec = PipeWireAEC(AECSettings())
    try:
        nodes = aec.start(timeout_s=5)
        assert nodes.source_node == SOURCE_NODE
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
        assert SOURCE_NODE in out
    finally:
        aec.stop()
    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
    assert SOURCE_NODE not in out  # gone with the app: nothing left behind


def test_aec_is_recreated_when_the_audio_service_restarted(tmp_path, monkeypatch):
    """Seen live: PipeWire was restarted while the app ran; the echo-cancelled mic vanished and the next
    Start failed. Before every Start the app now checks for its nodes and recreates the canceller."""
    import so101_voice.app as app_mod
    from so101_voice.app import build_companion
    from so101_voice.config import AppConfig, Secrets

    cfg = AppConfig.model_validate({"log_file": None, "cost": {"ledger_file": None}})
    comp = build_companion(
        cfg,
        Secrets(deepgram_api_key="k", openai_api_key="k", so101_api_token=None),
        on_event=lambda e: None,
        use_robot=False,
    )
    procs = []

    nodes = [AudioNode(9, "Audio/Source", SOURCE_NODE, SOURCE_DESCRIPTION)]
    state = {"up": False}

    def popen(cmd, **kw):
        procs.append(FakeProc())
        state["up"] = True  # the echo-cancel nodes appear once the process runs
        return procs[-1]

    comp.aec = PipeWireAEC(
        AECSettings(), popen=popen, list_nodes=lambda: nodes if state["up"] else [], runtime_dir=tmp_path
    )

    async def scenario():
        await comp.prepare_audio()
        assert len(procs) == 1 and comp.conversation.audio_mode == "aec"
        monkeypatch.setattr(app_mod, "list_audio_nodes", lambda: nodes if state["up"] else [])
        await comp.prepare_audio()  # nodes present: nothing to do
        assert len(procs) == 1
        state["up"] = False  # the audio service restarted: our nodes are gone, the process still "runs"
        await comp.prepare_audio()
        assert len(procs) == 2 and procs[0].terminated and state["up"]

    asyncio.run(scenario())
    comp.aec.stop()

"""PipeWire WebRTC acoustic echo cancellation, owned by this app (no system configuration is changed).

The app starts its own ``pipewire -c <generated config>`` client process that loads
``libpipewire-module-echo-cancel`` with the WebRTC backend (``aec/libspa-aec-webrtc``), the same
pattern the desktop uses for ``filter-chain.conf``. Its virtual nodes exist only while the process
runs. The user's default devices are never modified.

Two topologies (``[audio.aec] topology``):

* ``monitor`` (default): the reference is the **monitor of the speaker sink**, so everything played on
  the speakers is cancelled: the assistant's TTS *and* game audio and system sounds. The app plays to
  the normal speaker; the microphone is read from the echo-cancelled source. Fits "playing Valorant
  with the companion on room speakers".
* ``sink``: the module creates a virtual "SO-101 Echo-Cancel Speaker" sink. Only audio sent there
  (the TTS) is the reference, so game audio is NOT cancelled unless the game is also routed there.

The WebRTC canceller estimates the speaker→microphone delay itself (``delay_agnostic`` and
``extended_filter``), so playback/capture timestamps need no manual alignment. Sample-rate
conversion happens in PipeWire: the canceller runs at ``rate`` (48 kHz); the app still captures
16 kHz for Flux and plays 24 kHz TTS.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

MODULE_DIRS = (
    Path("/usr/lib/x86_64-linux-gnu/pipewire-0.3"),
    Path("/usr/lib64/pipewire-0.3"),
    Path("/usr/lib/pipewire-0.3"),
)
SPA_DIRS = (Path("/usr/lib/x86_64-linux-gnu/spa-0.2"), Path("/usr/lib64/spa-0.2"), Path("/usr/lib/spa-0.2"))

SOURCE_NODE = "so101_ec_source"
SINK_NODE = "so101_ec_sink"
SOURCE_DESCRIPTION = "SO-101 Echo-Cancelled Mic"
SINK_DESCRIPTION = "SO-101 Echo-Cancel Speaker"

Topology = Literal["monitor", "sink"]


class AECUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AECSettings:
    topology: Topology = "monitor"
    mic_node: str | None = None  # PipeWire node.name of the microphone (None = default source)
    speaker_node: str | None = None  # node.name of the speaker sink (None = default sink)
    rate: int = 48000
    noise_suppression: bool = True
    high_pass_filter: bool = True
    gain_control: bool = False  # AGC can pump the level the ASR sees; off by default
    extra_args: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class AECNodes:
    source_node: str
    source_description: str
    sink_node: str | None  # where the app plays to (None: the normal speaker, monitor topology)
    sink_description: str | None
    topology: Topology


@dataclass(frozen=True)
class AudioNode:
    id: int
    media_class: str
    name: str
    description: str


# ------------------------------------------------------------------ discovery
def probe() -> list[str]:
    """Problems that prevent AEC on this machine (empty list = everything needed is present)."""
    problems = []
    if shutil.which("pipewire") is None:
        problems.append("the `pipewire` binary is not installed")
    if shutil.which("pw-dump") is None:
        problems.append("`pw-dump` is not installed (package pipewire-bin)")
    if not any((d / "libpipewire-module-echo-cancel.so").exists() for d in MODULE_DIRS):
        problems.append("libpipewire-module-echo-cancel is not installed")
    if not any((d / "aec" / "libspa-aec-webrtc.so").exists() for d in SPA_DIRS):
        problems.append("the WebRTC AEC backend (spa aec/libspa-aec-webrtc) is not installed")
    if not problems and not list_audio_nodes():
        problems.append("no PipeWire audio graph is reachable (is the PipeWire daemon running?)")
    return problems


def list_audio_nodes(run: Callable[..., Any] = subprocess.run) -> list[AudioNode]:
    try:
        out = run(["pw-dump"], capture_output=True, text=True, timeout=5)
        data = json.loads(out.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    nodes = []
    for obj in data:
        props = (obj.get("info") or {}).get("props") or {}
        mc = str(props.get("media.class", ""))
        if obj.get("type", "").endswith("Node") and mc.startswith(("Audio/", "Stream/")):
            nodes.append(
                AudioNode(
                    int(obj["id"]),
                    mc,
                    str(props.get("node.name", "")),
                    str(props.get("node.description", "")),
                )
            )
    return nodes


def resolve_node(wanted: str | None, media_class: str, nodes: list[AudioNode]) -> str | None:
    """Map a configured device (node.name, or description substring) to a node.name; None = default."""
    if wanted in (None, "", "default"):
        return None
    cands = [n for n in nodes if n.media_class == media_class]
    for n in cands:
        if n.name == wanted:
            return n.name
    for n in cands:
        if str(wanted).lower() in n.description.lower():
            return n.name
    names = ", ".join(f"{n.description} ({n.name})" for n in cands)
    raise AECUnavailable(f"no {media_class} matches {wanted!r}. Available: {names}")


# ------------------------------------------------------------------ configuration
def _bool(v: bool) -> str:
    return "true" if v else "false"


def render_config(s: AECSettings) -> str:
    aec_args = {
        "webrtc.extended_filter": True,
        "webrtc.delay_agnostic": True,
        "webrtc.high_pass_filter": s.high_pass_filter,
        "webrtc.noise_suppression": s.noise_suppression,
        "webrtc.gain_control": s.gain_control,
        "webrtc.voice_detection": False,
    } | s.extra_args
    args = " ".join(f"{k} = {_bool(v)}" for k, v in aec_args.items())
    capture_target = f' target.object = "{s.mic_node}"' if s.mic_node else ""
    speaker_target = f' target.object = "{s.speaker_node}"' if s.speaker_node else ""
    monitor = s.topology == "monitor"
    # monitor topology: the "sink" side is a stream capturing the speaker's monitor ports
    sink_props = f'node.name = "{SINK_NODE}" node.description = "{SINK_DESCRIPTION}"' + (
        f" stream.capture.sink = true{speaker_target}" if monitor else ""
    )
    return f"""# generated by so101-voice: echo cancellation while the app runs (not a system config)
context.properties = {{ log.level = 2 }}
context.spa-libs = {{
  audio.convert.* = audioconvert/libspa-audioconvert
  support.* = support/libspa-support
}}
context.modules = [
  {{ name = libpipewire-module-rt args = {{ }} flags = [ ifexists nofail ] }}
  {{ name = libpipewire-module-protocol-native }}
  {{ name = libpipewire-module-client-node }}
  {{ name = libpipewire-module-adapter }}
  {{ name = libpipewire-module-echo-cancel
    args = {{
      library.name = aec/libspa-aec-webrtc
      monitor.mode = {_bool(monitor)}
      audio.rate = {s.rate}
      audio.channels = 1
      audio.position = [ MONO ]
      aec.args = {{ {args} }}
      capture.props = {{ node.name = "so101_ec_capture" node.passive = true{capture_target} }}
      source.props = {{ node.name = "{SOURCE_NODE}" node.description = "{SOURCE_DESCRIPTION}" }}
      sink.props = {{ {sink_props} }}
      playback.props = {{ node.name = "so101_ec_playback"{speaker_target} }}
    }}
  }}
]
"""


# ------------------------------------------------------------------ lifecycle
class PipeWireAEC:
    def __init__(
        self,
        settings: AECSettings,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        list_nodes: Callable[[], list[AudioNode]] = list_audio_nodes,
        runtime_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self._popen = popen
        self._list_nodes = list_nodes
        base = runtime_dir or Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        self.config_path = base / f"so101-voice-aec-{os.getpid()}.conf"
        self._proc: Any = None
        self.nodes: AECNodes | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout_s: float = 5.0) -> AECNodes:
        """Start the echo canceller and wait until its nodes exist. Raises ``AECUnavailable``."""
        if self.running and self.nodes is not None:
            return self.nodes
        stale = [n for n in self._list_nodes() if n.name == SOURCE_NODE]
        if stale:
            raise AECUnavailable(
                f"another echo canceller already provides {SOURCE_NODE} (another so101-voice?)"
            )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(render_config(self.settings))
        try:
            self._proc = self._popen(
                ["pipewire", "-c", str(self.config_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as e:
            raise AECUnavailable(f"cannot start pipewire: {e}") from e
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                code = self._proc.returncode
                err = (self._proc.stderr.read() if self._proc.stderr else "")[-500:]
                self.stop()
                raise AECUnavailable(f"the echo-cancel process exited (code {code}): {err.strip()}")
            names = {n.name for n in self._list_nodes()}
            want_sink = self.settings.topology == "sink"
            if SOURCE_NODE in names and (not want_sink or SINK_NODE in names):
                self.nodes = AECNodes(
                    SOURCE_NODE,
                    SOURCE_DESCRIPTION,
                    SINK_NODE if want_sink else None,
                    SINK_DESCRIPTION if want_sink else None,
                    self.settings.topology,
                )
                log.info(
                    "PipeWire echo cancellation running (%s topology, pid %s)",
                    self.settings.topology,
                    self._proc.pid,
                )
                return self.nodes
            time.sleep(0.1)
        self.stop()
        raise AECUnavailable(f"echo-cancel nodes did not appear within {timeout_s:.0f}s")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        self.nodes = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
        with contextlib.suppress(OSError):
            self.config_path.unlink()

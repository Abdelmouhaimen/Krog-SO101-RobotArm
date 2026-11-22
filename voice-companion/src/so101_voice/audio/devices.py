"""Microphone / speaker I/O behind a tiny protocol, so the session can run on real devices or fakes.

The real implementation uses ``miniaudio`` (bundled engine; talks to PipeWire/PulseAudio or ALSA, no
system PortAudio needed). Both devices run on miniaudio's own real-time threads:

* capture: every callback hands an int16 mono chunk at ``input_rate`` to ``on_audio`` (thread!)
* playback: every callback pulls exactly the requested frames from a ``PlaybackBuffer``

miniaudio resamples between the requested rate and the hardware rate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any, Protocol

from so101_voice.audio.playback import PlaybackBuffer

log = logging.getLogger(__name__)

AudioCallback = Callable[[bytes], None]
ErrorCallback = Callable[[str], None]


class AudioDeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    kind: str  # "input" | "output"
    index: int
    name: str


class AudioIO(Protocol):
    """Opens and closes both directions. Implementations must be safe to ``close()`` twice."""

    def start_input(self, on_audio: AudioCallback) -> None: ...
    def start_output(self, buffer: PlaybackBuffer) -> None: ...
    def close(self) -> None: ...
    @property
    def output_latency_s(self) -> float: ...


def _select(devices: list[dict[str, Any]], wanted: str | int | None, kind: str) -> dict[str, Any] | None:
    """Pick a device by index or case-insensitive name substring; None means the system default."""
    if wanted in (None, "", "default"):
        return None
    if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.isdigit()):
        i = int(wanted)
        if not 0 <= i < len(devices):
            raise AudioDeviceError(f"{kind} device index {i} does not exist ({len(devices)} available)")
        return devices[i]
    matches = [d for d in devices if str(wanted).lower() in d["name"].lower()]
    if not matches:
        names = ", ".join(d["name"] for d in devices)
        raise AudioDeviceError(f"no {kind} device matches {wanted!r}. Available: {names}")
    return matches[0]


def list_devices() -> list[DeviceInfo]:
    import miniaudio

    d = miniaudio.Devices()
    out = [DeviceInfo("input", i, c["name"]) for i, c in enumerate(d.get_captures())]
    out += [DeviceInfo("output", i, p["name"]) for i, p in enumerate(d.get_playbacks())]
    return out


class MiniaudioIO:
    def __init__(
        self,
        *,
        input_rate: int,
        output_rate: int,
        input_device: str | int | None = None,
        output_device: str | int | None = None,
        input_period_ms: int = 40,
        output_period_ms: int = 40,
        app_name: str = "so101-voice",
    ) -> None:
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.input_device = input_device
        self.output_device = output_device
        self.input_period_ms = input_period_ms
        self.output_period_ms = output_period_ms
        self.app_name = app_name
        self._capture: Any = None
        self._playback: Any = None

    @property
    def output_latency_s(self) -> float:
        # miniaudio's default is 3 periods of buffering on most backends.
        return 3 * self.output_period_ms / 1000.0

    def start_input(self, on_audio: AudioCallback) -> None:
        import miniaudio

        dev = _select(miniaudio.Devices().get_captures(), self.input_device, "input")

        def gen() -> Generator[None, Any, None]:
            while True:
                data = yield
                on_audio(bytes(data))

        try:
            self._capture = miniaudio.CaptureDevice(
                input_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=self.input_rate,
                buffersize_msec=self.input_period_ms,
                device_id=None if dev is None else dev["id"],
                app_name=self.app_name,
            )
            g = gen()
            next(g)
            self._capture.start(g)
        except miniaudio.MiniaudioError as e:
            raise AudioDeviceError(f"cannot open microphone: {e}") from e
        log.info("microphone open: %s @ %d Hz", "default" if dev is None else dev["name"], self.input_rate)

    def start_output(self, buffer: PlaybackBuffer) -> None:
        import miniaudio

        dev = _select(miniaudio.Devices().get_playbacks(), self.output_device, "output")

        def gen() -> Generator[bytes, int, None]:
            nframes = yield b""
            while True:
                nframes = yield buffer.read(nframes)

        try:
            self._playback = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=self.output_rate,
                buffersize_msec=self.output_period_ms,
                device_id=None if dev is None else dev["id"],
                app_name=self.app_name,
            )
            g = gen()
            next(g)
            self._playback.start(g)
        except miniaudio.MiniaudioError as e:
            raise AudioDeviceError(f"cannot open speaker: {e}") from e
        log.info("speaker open: %s @ %d Hz", "default" if dev is None else dev["name"], self.output_rate)

    def close(self) -> None:
        for name in ("_capture", "_playback"):
            dev = getattr(self, name)
            setattr(self, name, None)
            if dev is not None:
                try:
                    dev.close()
                except Exception as e:  # pragma: no cover - best effort
                    log.warning("closing %s failed: %s", name, e)

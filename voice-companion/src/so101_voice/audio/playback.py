"""Playback buffer: the single source of truth for "is the assistant audible right now?".

Agent audio arrives from Deepgram faster than real time. It is appended here by the asyncio side and
consumed by the audio device's real-time callback thread (``read``). The buffer therefore knows what the
speaker is *actually* doing, unlike TTS ``SpeechMetadata`` (the server finished sending audio).

State is reported through a listener called from the audio thread (keep it cheap and thread-safe, e.g.
``loop.call_soon_threadsafe``):

* ``PLAYING`` — the device callback started consuming real agent samples.
* ``IDLE``    — reason ``drained``: the response is complete (``mark_end_of_stream``), the buffer ran
                empty *and* the device's own output latency elapsed, i.e. the last sample left the speaker;
                reason ``underrun``: no end-of-stream yet, but nothing arrived for ``underrun_hold_s``
                (covers missing ``SpeechMetadata``); reason ``flushed:<why>``: barge-in / disconnect / stop.

**Timeline markers** (``mark(tag)``): a tag placed at the current write position is reported through
``add_marker_listener`` when the device consumes the first real sample at that position, with an
estimate of when that sample becomes audible (``+ device_latency_s``). The pipeline marks the start of
every TTS segment (to know what was actually heard before a barge-in) and choreography anchors gestures
to them. ``flush`` drops pending markers: a flushed segment's marker never fires.

Mid-response gaps are NOT reported as IDLE. Measured on the live API (2026-11): Deepgram sends TTS
sentence by sentence and the buffer can run dry for 20-40 ms between sentences. Reporting those as
IDLE made the UI phase flap SPEAKING→LISTENING mid-answer, so while the response is open the buffer
pads silence and stays PLAYING for up to ``underrun_hold_s``.
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

SAMPLE_WIDTH = 2  # int16 mono


class PlaybackState(enum.Enum):
    IDLE = "idle"
    PLAYING = "playing"


@dataclass(frozen=True)
class PlaybackEvent:
    state: PlaybackState
    reason: str  # "started" | "drained" | "underrun" | "flushed:<why>"
    t_mono: float


@dataclass(frozen=True)
class PlaybackMarker:
    tag: object
    t_mono: float  # when the device consumed the sample
    audible_at: float  # estimated time it leaves the speaker


Listener = Callable[[PlaybackEvent], None]
MarkerListener = Callable[[PlaybackMarker], None]


class PlaybackBuffer:
    def __init__(
        self,
        sample_rate: int,
        *,
        prebuffer_s: float = 0.06,
        device_latency_s: float = 0.04,
        underrun_hold_s: float = 0.5,
        max_buffer_s: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sample_rate = sample_rate
        self.prebuffer_bytes = int(prebuffer_s * sample_rate) * SAMPLE_WIDTH
        self.prebuffer_s = prebuffer_s
        self.device_latency_s = device_latency_s
        self.underrun_hold_s = underrun_hold_s
        self.max_bytes = int(max_buffer_s * sample_rate) * SAMPLE_WIDTH
        self._clock = clock
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._state = PlaybackState.IDLE
        self._end_of_stream = False  # server said the response audio is complete (final TTS SpeechMetadata)
        self._first_pending_t: float | None = None  # when un-started audio first arrived
        self._last_real_t: float | None = None  # when the last real sample was handed to the device
        self._listeners: list[Listener] = []
        self._marker_listeners: list[MarkerListener] = []
        self._markers: deque[tuple[int, object]] = deque()  # (absolute byte position, tag)
        self._written_pos = 0  # absolute stream position of the end of the buffer
        self._read_pos = 0  # absolute stream position of the next sample to play
        self.dropped_bytes = 0
        self.played_bytes = 0
        self.flushed_bytes = 0  # audio that was received but never played (barge-in, stop)
        self.dropped_markers = 0

    # ------------------------------------------------------------------ listeners
    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def add_marker_listener(self, fn: MarkerListener) -> None:
        self._marker_listeners.append(fn)

    def _emit(self, state: PlaybackState, reason: str) -> None:
        ev = PlaybackEvent(state, reason, self._clock())
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # a listener must never break the audio thread
                pass

    # ------------------------------------------------------------------ producer side (asyncio)
    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            room = self.max_bytes - len(self._buf)
            if room <= 0:
                self.dropped_bytes += len(pcm)
                return
            if len(pcm) > room:
                self.dropped_bytes += len(pcm) - room
                pcm = pcm[: room - room % SAMPLE_WIDTH]
            self._buf.extend(pcm)
            self._written_pos += len(pcm)
            self._end_of_stream = False
            if self._state is PlaybackState.IDLE and self._first_pending_t is None:
                self._first_pending_t = self._clock()

    def mark(self, tag: object) -> None:
        """Report ``tag`` when the next written sample starts playing (see module docstring)."""
        with self._lock:
            self._markers.append((self._written_pos, tag))

    def mark_end_of_stream(self) -> None:
        """No more audio is coming for this response: start playing whatever is buffered at once."""
        with self._lock:
            self._end_of_stream = True

    def flush(self, why: str) -> bool:
        """Drop all pending audio now (barge-in, disconnect, stop). Returns True if it was playing."""
        with self._lock:
            was_playing = self._state is PlaybackState.PLAYING
            self.flushed_bytes += len(self._buf)
            self._read_pos = self._written_pos  # positions stay monotonic across flushes
            self.dropped_markers += len(self._markers)
            self._markers.clear()
            self._buf.clear()
            self._first_pending_t = None
            self._last_real_t = None
            self._end_of_stream = False
            self._state = PlaybackState.IDLE
        if was_playing:
            self._emit(PlaybackState.IDLE, f"flushed:{why}")
        return was_playing

    # ------------------------------------------------------------------ queries
    @property
    def state(self) -> PlaybackState:
        return self._state

    def buffered_s(self) -> float:
        with self._lock:
            return len(self._buf) / SAMPLE_WIDTH / self.sample_rate

    # ------------------------------------------------------------------ consumer side (audio thread)
    def read(self, nframes: int) -> bytes:
        """Return exactly ``nframes`` int16 frames (silence-padded). Called by the device callback."""
        want = nframes * SAMPLE_WIDTH
        now = self._clock()
        event: tuple[PlaybackState, str] | None = None
        reached: list[PlaybackMarker] = []
        with self._lock:
            if self._state is PlaybackState.IDLE and self._buf:
                ready = (
                    len(self._buf) >= self.prebuffer_bytes
                    or self._end_of_stream
                    or (self._first_pending_t is not None and now - self._first_pending_t >= self.prebuffer_s)
                )
                if ready:
                    self._state = PlaybackState.PLAYING
                    self._first_pending_t = None
                    event = (PlaybackState.PLAYING, "started")
            if self._state is PlaybackState.PLAYING:
                n = min(want, len(self._buf))
                chunk = bytes(self._buf[:n])
                del self._buf[:n]
                if n:
                    start = self._read_pos
                    self._read_pos += n
                    while self._markers and self._markers[0][0] < self._read_pos:
                        pos, tag = self._markers.popleft()
                        offset = max(0, pos - start) / SAMPLE_WIDTH / self.sample_rate
                        reached.append(PlaybackMarker(tag, now, now + offset + self.device_latency_s))
                    self.played_bytes += n
                    # The last real sample of this chunk leaves the speaker after this chunk's duration.
                    self._last_real_t = now + n / SAMPLE_WIDTH / self.sample_rate
                elif self._last_real_t is not None:
                    silent_since = (
                        self._last_real_t + self.device_latency_s
                    )  # last sample has left the speaker
                    if self._end_of_stream and now >= silent_since:
                        event = (PlaybackState.IDLE, "drained")
                    elif now >= silent_since + self.underrun_hold_s:
                        event = (PlaybackState.IDLE, "underrun")
                    if event is not None:
                        self._state = PlaybackState.IDLE
                        self._last_real_t = None
            else:
                chunk = b""
        if event is not None and event[0] is PlaybackState.PLAYING:
            self._emit(*event)
        for mk in reached:
            for ml in list(self._marker_listeners):
                try:
                    ml(mk)
                except Exception:  # a listener must never break the audio thread
                    pass
        if event is not None and event[0] is PlaybackState.IDLE:
            self._emit(*event)
        if len(chunk) < want:
            chunk += bytes(want - len(chunk))
        return chunk

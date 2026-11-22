"""Cost-aware microphone gate: decides which captured audio is sent to (and billed by) the STT service.

Modes:
* ``continuous`` — every sample is sent. The reference mode: Flux sees all audio, including silences,
  for its end-of-turn model.
* ``push_to_talk`` — audio is sent only while the user holds talk on (a toggle in the terminal UI),
  plus ``preroll_ms`` from before the press and ``postroll_ms`` after the release.
* ``vad`` — **experimental**: a local energy detector opens the gate on speech (``attack_ms`` above
  ``threshold_dbfs``) and closes it ``postroll_ms`` after the level drops. ``preroll_ms`` of audio from
  before the onset is sent first, so word starts are not clipped.

Whether gating confuses Flux's end-of-turn detection is NOT verified. The post-roll keeps some trailing
silence after speech so the model can see the turn end. That is why ``continuous`` stays the default.

The gate is pure (no I/O): ``process(pcm)`` returns the bytes to send now. The caller keeps a send
timeline so the STT client can close an idle stream and reopen it when the gate opens again.
"""

from __future__ import annotations

import array
import enum
import math
from collections import deque


class InputMode(enum.Enum):
    CONTINUOUS = "continuous"
    PUSH_TO_TALK = "push_to_talk"
    VAD = "vad"


def level_dbfs(pcm: bytes) -> float:
    """RMS level of int16 mono PCM in dBFS (-120 for digital silence)."""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not a:
        return -120.0
    rms = math.sqrt(sum(x * x for x in a) / len(a)) / 32768.0
    return 20 * math.log10(rms) if rms > 1e-6 else -120.0


class MicGate:
    def __init__(
        self,
        mode: InputMode,
        *,
        sample_rate: int = 16000,
        preroll_ms: int = 300,
        postroll_ms: int = 800,
        threshold_dbfs: float = -42.0,
        attack_ms: int = 60,
    ) -> None:
        self.mode = mode
        self.bytes_per_ms = sample_rate * 2 / 1000
        self.preroll_bytes = int(preroll_ms * self.bytes_per_ms)
        self.postroll_bytes = int(postroll_ms * self.bytes_per_ms)
        self.attack_bytes = int(attack_ms * self.bytes_per_ms)
        self.threshold_dbfs = threshold_dbfs
        self._ptt = False
        self._open = mode is InputMode.CONTINUOUS
        self._pre: deque[bytes] = deque()
        self._pre_len = 0
        self._post_left = 0  # bytes still to send after the close condition
        self._loud_run = 0  # consecutive loud bytes (VAD attack)
        self.sent_bytes = 0
        self.suppressed_bytes = 0

    @property
    def is_open(self) -> bool:
        return self._open

    def set_push_to_talk(self, active: bool) -> None:
        self._ptt = active

    def reset(self) -> None:
        """Forget buffered pre-roll (e.g. after a reconnect)."""
        self._pre.clear()
        self._pre_len = 0
        if self.mode is not InputMode.CONTINUOUS:
            self._open = False
            self._post_left = 0
            self._loud_run = 0

    def process(self, pcm: bytes) -> bytes:
        if self.mode is InputMode.CONTINUOUS:
            self.sent_bytes += len(pcm)
            return pcm
        want = self._ptt if self.mode is InputMode.PUSH_TO_TALK else self._vad(pcm)
        if want:
            self._post_left = self.postroll_bytes
            if not self._open:
                self._open = True
                out = b"".join(self._pre) + pcm
                self._pre.clear()
                self._pre_len = 0
                self.sent_bytes += len(out)
                return out
            self.sent_bytes += len(pcm)
            return pcm
        if self._open and self._post_left > 0:
            self._post_left -= len(pcm)
            self.sent_bytes += len(pcm)
            if self._post_left <= 0:
                self._open = False
            return pcm
        self._open = False
        self._remember(pcm)
        self.suppressed_bytes += len(pcm)
        return b""

    # ------------------------------------------------------------------ internals
    def _vad(self, pcm: bytes) -> bool:
        if level_dbfs(pcm) >= self.threshold_dbfs:
            self._loud_run += len(pcm)
        else:
            self._loud_run = 0
        return self._loud_run >= self.attack_bytes or (self._open and self._loud_run > 0)

    def _remember(self, pcm: bytes) -> None:
        self._pre.append(pcm)
        self._pre_len += len(pcm)
        while self._pre and self._pre_len - len(self._pre[0]) >= self.preroll_bytes:
            self._pre_len -= len(self._pre.popleft())

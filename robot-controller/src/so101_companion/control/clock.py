from __future__ import annotations

import threading


class FakeClock:
    """Deterministic monotonic clock for tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, dt: float) -> float:
        if dt < 0:
            raise ValueError("monotonic clock cannot go backwards")
        with self._lock:
            self._t += dt
            return self._t

    def sleep(self, dt: float) -> None:
        self.advance(dt)

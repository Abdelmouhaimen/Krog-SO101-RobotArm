"""The ``--screenshot`` aid must never starve the controller thread.

Real-arm session 2026-11-08: ``window.grab().save(png)`` ran every second on the GUI thread, and
``QPixmap.save`` keeps the GIL for the whole PNG encode (~130 ms). Twice the controller thread lost the
GIL inside the Feetech SDK's receive loop, came back after its 50 ms wall-clock timeout, and reported a
false "There is no status packet!" -> READ_FAILED, although the servos had answered.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from so101_companion.app import WindowShooter

SDK_RX_TIMEOUT_S = 0.050  # lerobot's patched Feetech packet timeout (~50 ms + bytes)


class Busy(QWidget):
    """A large window whose pixels are noise, so its PNG encodes slowly (like the Studio at 1838x1113 on
    a 1.25x display): saving it the old way blocks other threads for well over the SDK timeout."""

    def __init__(self) -> None:
        super().__init__()
        rgb = np.random.default_rng(0).integers(0, 256, (1400, 2300, 3), dtype=np.uint8)
        self._noise = QImage(rgb.tobytes(), 2300, 1400, 2300 * 3, QImage.Format.Format_RGB888).copy()

    def paintEvent(self, _event: object) -> None:
        p = QPainter(self)
        p.drawImage(0, 0, self._noise)
        p.end()


def worst_gap_while(fn: object, done: object) -> float:
    gaps: list[float] = []
    stop = threading.Event()

    def ticker() -> None:  # stands in for the controller thread
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.001)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    t = threading.Thread(target=ticker)
    t.start()
    time.sleep(0.05)
    fn()  # type: ignore[operator]
    deadline = time.monotonic() + 10.0
    while not done() and time.monotonic() < deadline:  # type: ignore[operator]
        time.sleep(0.01)
    time.sleep(0.05)
    stop.set()
    t.join()
    return max(gaps)


def test_screenshot_does_not_starve_other_threads(qapp: QApplication, tmp_path: Path) -> None:
    w = Busy()
    w.resize(2300, 1400)
    w.show()
    qapp.processEvents()
    out = tmp_path / "window.png"
    shooter = WindowShooter(w, out)
    gap = worst_gap_while(shooter.shoot, done=out.exists)  # measured through the background write
    assert out.exists() and out.stat().st_size > 10_000
    assert gap < SDK_RX_TIMEOUT_S, f"screenshot blocked other threads for {gap * 1000:.0f} ms"
    # a second shot while one is still being written is skipped, never queued
    with shooter._busy:
        assert shooter.shoot() is False
    w.close()

"""Controller thread: the ONLY thread that calls the coordinator (and therefore the servo bus).

Scheduling (FR-07, UX-04): monotonic deadlines at ``command_hz``. Operator requests are handled
as soon as they arrive. If a cycle overruns (e.g. slow serial), the missed deadlines are SKIPPED
and counted — never replayed — so there is exactly one send per issued tick and no catch-up burst.
Timed trajectories additionally use a stall-aware logical clock (control/timeline.py, ADR-013/014),
so a stall delays them instead of making the next target jump ahead.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from so101_companion.control.coordinator import ControllerCoordinator
from so101_companion.domain.state import Snapshot


class ControllerWorker(threading.Thread):
    def __init__(
        self,
        coordinator: ControllerCoordinator,
        publish: Callable[[Snapshot], None],
        hz: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="so101-controller", daemon=True)
        self.c = coordinator
        self.publish = publish
        self.period = 1.0 / hz
        self.clock = clock
        self._stop_event = threading.Event()
        self.c.on_change = self._publish_now
        self.error: BaseException | None = None

    def _publish_now(self) -> None:
        snap = self.c.snapshot()
        self.c.published.set(snap)  # for readers on other threads (HTTP API, video producer)
        self.publish(snap)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self.c.wake.set()
        if self.is_alive():
            self.join(timeout)

    def run(self) -> None:
        try:
            self._loop()
        except BaseException as exc:  # pragma: no cover - surfaced in UI/logs
            self.error = exc
            self.c.events.emit("controller_crashed", error=f"{type(exc).__name__}: {exc}")
            raise

    def _loop(self) -> None:
        next_deadline = self.clock()
        while not self._stop_event.is_set():
            self.c.wake.clear()
            if self.c.process_requests():
                continue
            now = self.clock()
            if now >= next_deadline:
                self.c.tick(now)
                self._publish_now()
                next_deadline += self.period
                after = self.clock()
                if after > next_deadline:
                    skipped = int((after - next_deadline) // self.period) + 1
                    self.c.overruns += skipped
                    next_deadline += skipped * self.period
                continue
            self.c.wake.wait(timeout=max(0.0, next_deadline - now))

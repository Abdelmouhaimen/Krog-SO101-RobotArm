"""Structured event/telemetry log (contracts/events-and-telemetry.md, FR-17).

Thread-safe ring buffer written by the controller thread, read by the UI. Export is JSON Lines with
redaction: serial-adapter serial numbers (``/dev/serial/by-id/...``) and the user's home directory
are removed, so exported logs carry robot telemetry only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

BY_ID_RE = re.compile(r"(/dev/serial/by-(?:id|path)/)[^\s\"']+")
TICK_KIND = "control_tick"


def _redact_str(text: str, home: str) -> str:
    text = BY_ID_RE.sub(r"\1<redacted>", text)
    if home and home != "/":
        text = text.replace(home, "~")
    return text


def redact(value: Any, home: str | None = None) -> Any:
    home = home if home is not None else os.path.expanduser("~")
    if isinstance(value, str):
        return _redact_str(value, home)
    if isinstance(value, dict):
        return {k: redact(v, home) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, home) for v in value]
    return value


class EventLog:
    def __init__(self, ring_size: int = 20000, clock: Callable[[], float] = time.monotonic) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._lock = threading.Lock()
        self._clock = clock
        self._seq = 0
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            record = {
                "kind": kind,
                "event_seq": self._seq,
                "t_monotonic_seconds": round(self._clock(), 6),
                "wall_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                **fields,
            }
            self._ring.append(record)
            listeners = list(self._listeners)
        for listener in listeners:
            listener(record)
        return record

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def records(
        self, kinds: Iterable[str] | None = None, exclude_ticks: bool = False
    ) -> list[dict[str, Any]]:
        wanted = set(kinds) if kinds is not None else None
        with self._lock:
            items = list(self._ring)
        return [
            r
            for r in items
            if (wanted is None or r["kind"] in wanted) and not (exclude_ticks and r["kind"] == TICK_KIND)
        ]

    def last(self, kind: str) -> dict[str, Any] | None:
        for record in reversed(self.records()):
            if record["kind"] == kind:
                found: dict[str, Any] = record
                return found
        return None

    def stream_to(self, path: Path) -> None:
        """Append every future record (incl. control ticks) to ``path`` as redacted JSON Lines, flushed
        per line, for live analysis of a running session (``--event-log``)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a", buffering=1)
        lock = threading.Lock()

        def write(record: dict[str, Any]) -> None:
            line = json.dumps(redact(record), default=str)
            with lock:
                fh.write(line + "\n")

        for record in self.records():
            write(record)
        self.subscribe(write)

    def export_jsonl(self, path: Path, *, include_ticks: bool = True) -> int:
        rows = self.records(exclude_ticks=not include_ticks)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for row in rows:
                fh.write(json.dumps(redact(row), default=str) + "\n")
        return len(rows)

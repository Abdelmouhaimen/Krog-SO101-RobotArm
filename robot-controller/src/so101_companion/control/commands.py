"""Command ledger (ADR-018): the lifecycle of every tracked motion command, from the Studio or the API.

The controller thread is the ONLY writer of command state (``mark``/``activate``/``finish_active``);
HTTP threads create records and read copies. One lock guards everything; records leave the ledger only
as JSON copies, so a reader never sees a half-updated record.

States::

    queued -> running <-> paused -> ending -> completed
           \\-> rejected            (controller refused it; nothing moved)
    running/paused/ending -> superseded (a newer motion command replaced it; ``superseded_by``)
                          -> cancelled  (Emergency Hold, Torque OFF, Disarm, slider, mode change)
                          -> failed     (fault latched, rest not reached, read failed)
"""

from __future__ import annotations

import enum
import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class CommandState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    ENDING = "ending"  # the behavior completed; its "After behavior" move is in progress
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL = frozenset(
    {
        CommandState.COMPLETED,
        CommandState.SUPERSEDED,
        CommandState.CANCELLED,
        CommandState.REJECTED,
        CommandState.FAILED,
    }
)
ACTIVE = frozenset({CommandState.RUNNING, CommandState.PAUSED, CommandState.ENDING})


class IdempotencyConflict(ValueError):
    """The idempotency key was already used for a DIFFERENT request."""

    def __init__(self, key: str, command_id: str) -> None:
        super().__init__(f"Idempotency-Key {key!r} was already used for a different request ({command_id})")
        self.key = key
        self.command_id = command_id


@dataclass
class CommandRecord:
    id: str
    kind: str
    source: str
    params: dict[str, Any]
    created_wall: str
    created_mono: float
    state: CommandState = CommandState.QUEUED
    detail: str = ""
    handled_mono: float | None = None
    started_mono: float | None = None
    first_send_mono: float | None = None
    finished_mono: float | None = None
    superseded_by: str | None = None
    replaced: str | None = None
    cancelled_by: str | None = None
    after_behavior_cancelled: bool = False
    idempotency_key: str | None = None
    history: list[tuple[float, str, str]] = field(default_factory=list)
    handled: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_json(self) -> dict[str, Any]:
        def ms(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else round((b - a) * 1000.0, 2)

        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "state": self.state.value,
            "terminal": self.state in TERMINAL,
            "detail": self.detail,
            "params": self.params,
            "created_at": self.created_wall,
            "superseded_by": self.superseded_by,
            "replaced": self.replaced,
            "cancelled_by": self.cancelled_by,
            "after_behavior_cancelled": self.after_behavior_cancelled,
            "idempotency_key": self.idempotency_key,
            "latency_ms": {
                "dispatch": ms(self.created_mono, self.handled_mono),  # received -> controller handled
                "first_target": ms(self.created_mono, self.first_send_mono),  # received -> first send
            },
            "duration_s": None
            if self.started_mono is None or self.finished_mono is None
            else round(self.finished_mono - self.started_mono, 3),
            "history": [
                {"t_s": round(t - self.created_mono, 4), "state": s, "detail": d} for t, s, d in self.history
            ],
        }


def request_fingerprint(kind: str, params: Mapping[str, Any]) -> str:
    raw = json.dumps({"kind": kind, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class CommandLedger:
    def __init__(self, clock: Callable[[], float] = time.monotonic, history: int = 500) -> None:
        self.clock = clock
        self._history = history
        self._lock = threading.Lock()
        self._records: OrderedDict[str, CommandRecord] = OrderedDict()
        self._idem: OrderedDict[str, tuple[str, str]] = OrderedDict()  # key -> (fingerprint, command id)
        self.active_id: str | None = None  # controller thread only: the command owning the motion
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    # ------------------------------------------------------------------ any thread
    def create(
        self,
        kind: str,
        source: str,
        params: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """New record (state ``queued``). With an idempotency key already used for the SAME request the
        original record is returned and ``replay`` is True (nothing must be dispatched again)."""
        params = dict(params or {})
        fp = request_fingerprint(kind, params)
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idem:
                old_fp, old_id = self._idem[idempotency_key]
                if old_fp != fp:
                    raise IdempotencyConflict(idempotency_key, old_id)
                rec = self._records.get(old_id)
                if rec is not None:
                    return rec.to_json(), True
            now = self.clock()
            rec = CommandRecord(
                id="cmd_" + uuid.uuid4().hex[:16],
                kind=kind,
                source=source,
                params=params,
                created_wall=datetime.now(UTC).isoformat(timespec="milliseconds"),
                created_mono=now,
                idempotency_key=idempotency_key,
            )
            rec.history.append((now, rec.state.value, "received"))
            self._records[rec.id] = rec
            if idempotency_key is not None:
                self._idem[idempotency_key] = (fp, rec.id)
                while len(self._idem) > self._history * 4:
                    self._idem.popitem(last=False)
            while len(self._records) > self._history:
                oldest = next(iter(self._records))
                if oldest == self.active_id:
                    self._records.move_to_end(oldest)
                    break
                self._records.popitem(last=False)
            return rec.to_json(), False

    def get(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._records.get(command_id)
            return None if rec is None else rec.to_json()

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            recs = list(self._records.values())[-limit:]
            return [r.to_json() for r in reversed(recs)]

    def wait_handled(self, command_id: str, timeout: float) -> dict[str, Any] | None:
        """Block (an HTTP worker thread, never the controller) until the controller handled the command
        or ``timeout`` passed; returns the current record."""
        with self._lock:
            rec = self._records.get(command_id)
        if rec is None:
            return None
        rec.handled.wait(timeout)
        return self.get(command_id)

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            rec = self._records.get(self.active_id) if self.active_id else None
            return None if rec is None else rec.to_json()

    # ------------------------------------------------------------------ controller thread
    def state_of(self, command_id: str | None) -> CommandState | None:
        with self._lock:
            rec = self._records.get(command_id) if command_id else None
            return None if rec is None else rec.state

    def mark(self, command_id: str | None, state: CommandState, detail: str = "", **fields: Any) -> None:
        if command_id is None:
            return
        with self._lock:
            rec = self._records.get(command_id)
            if rec is None or rec.state in TERMINAL:
                return
            now = self.clock()
            rec.state = state
            if detail:
                rec.detail = detail
            for k, v in fields.items():
                setattr(rec, k, v)
            if rec.handled_mono is None and state != CommandState.QUEUED:
                rec.handled_mono = now
            if state in ACTIVE and rec.started_mono is None:
                rec.started_mono = now
            if state in TERMINAL:
                rec.finished_mono = now
                if self.active_id == command_id:
                    self.active_id = None
            rec.history.append((now, state.value, detail))
            if state != CommandState.QUEUED:
                rec.handled.set()
            snapshot = rec.to_json()
        for listener in list(self.listeners):
            listener(snapshot)

    def note_first_send(self) -> None:
        with self._lock:
            rec = self._records.get(self.active_id) if self.active_id else None
            if rec is not None and rec.first_send_mono is None:
                rec.first_send_mono = self.clock()

    def activate(self, command_id: str | None, detail: str = "") -> str | None:
        """``command_id`` now owns the motion. The previous owner (if still active) is SUPERSEDED.
        Returns the superseded command id."""
        previous = self.active_id
        superseded = None
        if previous is not None and previous != command_id and self.state_of(previous) in ACTIVE:
            ending = self.state_of(previous) == CommandState.ENDING
            self.mark(
                previous,
                CommandState.SUPERSEDED,
                f"superseded by {command_id}"
                + (" (its After Behavior action was cancelled)" if ending else ""),
                superseded_by=command_id,
                after_behavior_cancelled=ending,
            )
            superseded = previous
        self.active_id = command_id
        if command_id is not None:
            self.mark(command_id, CommandState.RUNNING, detail, replaced=superseded)
        return superseded

    def finish_active(self, state: CommandState, detail: str, **fields: Any) -> None:
        cid = self.active_id
        if cid is not None:
            self.mark(cid, state, detail, **fields)
        self.active_id = None

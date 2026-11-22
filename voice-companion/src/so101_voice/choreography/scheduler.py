"""Mock choreography scheduler: binds gestures to playback markers. NOT connected to the robot.

Flow for one generation (one assistant response):

1. ``prepare(gen, plan)`` returns the speech segments, each with a marker tag. The pipeline calls
   ``PlaybackBuffer.mark(tag)`` right before writing that segment's audio. Gestures are registered
   against those tags, plus an ``END`` tag for a gesture after the last segment.
2. ``on_marker(tag)`` (a playback marker reached the speaker) and ``on_drained(gen)`` (the generation
   finished playing) trigger the anchored gestures through the ``GestureExecutor``.
3. ``cancel_generation(gen)`` (barge-in, stop, disconnect) drops its pending gestures and cancels a
   running one. Late markers for a cancelled generation are ignored, so a cancelled gesture never runs.

Supersession: starting a gesture cancels the one still running (same rule as robot API commands).
The only executor shipped is ``MockGestureExecutor``. A real one, calling ``RobotActions`` with the
idempotency key below, is future work and requires physical tools to be enabled explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from so101_voice.choreography.contracts import (
    BehaviorAction,
    ChoreographyPlan,
    GestureAction,
    SpeakAction,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SegmentTag:
    gen: int
    index: int  # position among the plan's speak segments; END = after the last one


END = -1


@dataclass(frozen=True)
class PlannedSegment:
    tag: SegmentTag
    text: str


class GestureExecutor(Protocol):
    async def run(self, action: GestureAction, *, idempotency_key: str, cancelled: asyncio.Event) -> None: ...


@dataclass
class MockGestureExecutor:
    """Records what would have been sent to the robot. ``duration_s`` simulates gesture length."""

    duration_s: float = 0.0
    clock: Callable[[], float] = time.monotonic
    started: list[tuple[float, str, str]] = field(default_factory=list)  # (t, target, key)
    finished: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    async def run(self, action: GestureAction, *, idempotency_key: str, cancelled: asyncio.Event) -> None:
        target = action.behavior_id if isinstance(action, BehaviorAction) else action.pose_id
        self.started.append((self.clock(), target, idempotency_key))
        try:
            if self.duration_s:
                await asyncio.sleep(self.duration_s)
            if cancelled.is_set():
                raise asyncio.CancelledError
            self.finished.append(idempotency_key)
        except asyncio.CancelledError:
            self.cancelled.append(idempotency_key)
            raise


@dataclass
class _Pending:
    action: GestureAction
    key: str


class ChoreographyScheduler:
    def __init__(self, executor: GestureExecutor) -> None:
        self.executor = executor
        self._pending: dict[SegmentTag, list[_Pending]] = {}
        self._cancelled_gens: set[int] = set()
        self._running: tuple[asyncio.Task[None], asyncio.Event, int] | None = None
        self.fired: list[str] = []

    def prepare(self, gen: int, plan: ChoreographyPlan) -> list[PlannedSegment]:
        segments: list[PlannedSegment] = []
        waiting: list[tuple[GestureAction, int]] = []  # gestures waiting for the next segment's start
        n_speak = 0
        for i, act in enumerate(plan.actions):
            if isinstance(act, SpeakAction):
                tag = SegmentTag(gen, n_speak)
                segments.append(PlannedSegment(tag, act.text))
                for g, gi in waiting:
                    self._add(tag, g, gen, gi)
                waiting = []
                n_speak += 1
            elif act.anchor == "immediate":
                self._add(SegmentTag(gen, 0), act, gen, i)
            else:  # after_previous and with_next both resolve to the next segment start (or END)
                waiting.append((act, i))
        for g, gi in waiting:
            self._add(SegmentTag(gen, END), g, gen, gi)
        return segments

    def on_marker(self, tag: object) -> None:
        if isinstance(tag, SegmentTag):
            self._fire(tag)

    def on_drained(self, gen: int) -> None:
        self._fire(SegmentTag(gen, END))

    def cancel_generation(self, gen: int) -> None:
        self._cancelled_gens.add(gen)
        for tag in [t for t in self._pending if t.gen == gen]:
            del self._pending[tag]
        if self._running is not None and self._running[2] == gen:
            task, ev, _ = self._running
            ev.set()
            task.cancel()

    async def aclose(self) -> None:
        self._pending.clear()
        if self._running is not None:
            task, ev, _ = self._running
            ev.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------ internals
    def _add(self, tag: SegmentTag, action: GestureAction, gen: int, index: int) -> None:
        self._pending.setdefault(tag, []).append(_Pending(action, f"gesture-{gen}-{index}"))

    def _fire(self, tag: SegmentTag) -> None:
        if tag.gen in self._cancelled_gens:
            return
        for p in self._pending.pop(tag, []):
            self._start(p, tag.gen)

    def _start(self, p: _Pending, gen: int) -> None:
        if self._running is not None and not self._running[0].done():
            task, ev, _ = self._running
            ev.set()
            task.cancel()  # supersede
        ev = asyncio.Event()
        task = asyncio.create_task(self.executor.run(p.action, idempotency_key=p.key, cancelled=ev))
        task.add_done_callback(_consume)
        self._running = (task, ev, gen)
        self.fired.append(p.key)
        log.info("gesture %s fired (mock)", p.key)


def _consume(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()

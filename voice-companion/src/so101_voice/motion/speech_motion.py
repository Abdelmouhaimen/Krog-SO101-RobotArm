"""SpeechMotionCoordinator: the SO-101 gripper "talks" while the ASSISTANT is audible, nothing else.

Inputs (all on the asyncio loop):
* ``on_playback(event)`` — from ``PlaybackBuffer`` (real speaker state, not network events).
* ``force_off(reason)`` — barge-in, provider disconnect, session stop, app exit: OFF immediately.

Output: ``POST /api/v1/talk/start`` (with a short lease) and ``POST /api/v1/talk/stop``.

Design:
* ONE reconciler task owns every robot request, so start/renew/stop are strictly ordered and never race.
* Desired state = audible, plus a short ``stop_grace_s`` after a natural drain, so a brief network
  underrun between audio packets does not flap the gripper. Flush / force_off skip the grace.
* Talk is started once per audible period and renewed every ``renew_interval_s`` with ``lease_s``
  (lease > 2 × renewal interval). If this process dies, the robot stops by itself within ``lease_s``.
* No duplicates: a start is sent only when the robot is believed OFF or a renewal is due; a stop only
  when it is believed ON (or might be).
* Robot unreachable / refusing: the voice session continues; ``status.link`` says why, retries are
  rate-limited to ``retry_interval_s`` and only while the assistant is audible.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from so101_voice.audio.playback import PlaybackEvent, PlaybackState
from so101_voice.robot.client import (
    RobotAPIClient,
    RobotAPIError,
    RobotAuthError,
    RobotRejected,
    RobotUnavailable,
    TalkIntensity,
)

log = logging.getLogger(__name__)


class RobotLink(enum.Enum):
    DISABLED = "disabled"  # no robot configured (--no-robot)
    UNKNOWN = "unknown"  # nothing sent yet
    OK = "ok"
    UNAVAILABLE = "unavailable"  # service down / timeout
    REJECTED = "rejected"  # 409: emergency hold, torque off, fault ...
    AUTH = "auth_failed"
    ERROR = "error"


@dataclass
class TalkStatus:
    link: RobotLink
    talk_on: bool
    detail: str = ""


@dataclass
class TalkStats:
    starts: int = 0
    renewals: int = 0
    stops: int = 0
    failures: int = 0
    history: list[str] = field(default_factory=list)  # "start", "renew", "stop" — for tests and logs


class SpeechMotionCoordinator:
    def __init__(
        self,
        robot: RobotAPIClient | None,
        *,
        intensity: TalkIntensity = "normal",
        lease_s: float = 2.0,
        renew_interval_s: float = 0.8,
        stop_grace_s: float = 0.25,
        retry_interval_s: float = 2.0,
        on_status: Callable[[TalkStatus], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if lease_s < 2 * renew_interval_s:
            raise ValueError("lease_s must be at least twice renew_interval_s")
        self.robot = robot
        self.intensity: TalkIntensity = intensity
        self.lease_s = lease_s
        self.renew_interval_s = renew_interval_s
        self.stop_grace_s = stop_grace_s
        self.retry_interval_s = retry_interval_s
        self._on_status = on_status
        self._clock = clock
        self.stats = TalkStats()

        self._audible = False
        self._grace_until: float | None = None
        self._robot_on = False  # robot believed to be animating
        self._lease_expires_at = 0.0
        self._next_renew_at = 0.0
        self._retry_at = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.status = TalkStatus(RobotLink.DISABLED if robot is None else RobotLink.UNKNOWN, False)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.robot is not None and self._task is None:
            self._task = asyncio.create_task(self._run(), name="speech-motion")

    async def aclose(self, timeout_s: float = 1.0) -> None:
        """Best-effort Talk OFF, then stop the reconciler. Safe to call more than once."""
        if self._closed:
            return
        self._audible, self._grace_until = False, None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self.robot is not None and self._robot_on:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._send_stop(), timeout_s)
        self._closed = True

    # ------------------------------------------------------------------ inputs
    def on_playback(self, ev: PlaybackEvent) -> None:
        if ev.state is PlaybackState.PLAYING:
            self._audible, self._grace_until = True, None
        elif ev.reason.startswith("flushed"):
            self._audible, self._grace_until = False, None
        else:  # natural drain: keep talking a moment in case more audio is only late
            self._audible = False
            self._grace_until = self._clock() + self.stop_grace_s
        self._wake.set()

    def force_off(self, reason: str) -> None:
        log.debug("talk force_off: %s", reason)
        self._audible, self._grace_until = False, None
        self._wake.set()

    @property
    def desired_on(self) -> bool:
        return self._audible or (self._grace_until is not None and self._clock() < self._grace_until)

    # ------------------------------------------------------------------ reconciler
    async def _run(self) -> None:
        while True:
            now = self._clock()
            want = self.desired_on
            if self._robot_on and now >= self._lease_expires_at:
                self._robot_on = False  # the robot let the lease lapse by itself
                self._publish()
            try:
                # Start/renew only while audio is actually audible; during the stop grace the current
                # lease (>= lease_s - renew_interval_s left) already covers it, so nothing is sent.
                if want and self._audible and now >= self._retry_at:
                    if not self._robot_on:
                        await self._send_start(renewal=False)
                    elif now >= self._next_renew_at:
                        await self._send_start(renewal=True)
                elif not want and self._robot_on and now >= self._retry_at:
                    await self._send_stop()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the reconciler die (or spin)
                log.exception("talk reconciler error: %s", e)
                self._retry_at = self._clock() + self.retry_interval_s
                self._set(RobotLink.ERROR, str(e))
            await self._sleep_until_next()

    async def _sleep_until_next(self) -> None:
        now = self._clock()
        deadlines = []
        if self.desired_on:
            if self._audible:
                deadlines.append(self._next_renew_at if self._robot_on else self._retry_at)
            if self._grace_until is not None:
                deadlines.append(self._grace_until)
        elif self._robot_on:
            deadlines.append(max(self._retry_at, now))
            deadlines.append(self._lease_expires_at)
        timeout = None if not deadlines else max(0.0, min(deadlines) - now)
        self._wake.clear()
        if timeout == 0.0:
            await asyncio.sleep(0)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout)

    async def _send_start(self, *, renewal: bool) -> None:
        assert self.robot is not None
        t0 = self._clock()
        try:
            await self.robot.talk_start(lease_s=self.lease_s, intensity=self.intensity)
        except RobotAPIError as e:
            self._failed(e)
            return
        self._robot_on = True
        self._lease_expires_at = t0 + self.lease_s
        self._next_renew_at = t0 + self.renew_interval_s
        if renewal:
            self.stats.renewals += 1
            self.stats.history.append("renew")
        else:
            self.stats.starts += 1
            self.stats.history.append("start")
        self._set(RobotLink.OK, "")

    async def _send_stop(self) -> None:
        assert self.robot is not None
        try:
            await self.robot.talk_stop()
        except RobotAPIError as e:
            self._failed(e)  # the lease bounds how long it can keep going
            return
        self._robot_on = False
        self.stats.stops += 1
        self.stats.history.append("stop")
        self._set(RobotLink.OK, "")

    def _failed(self, e: RobotAPIError) -> None:
        self.stats.failures += 1
        self._retry_at = self._clock() + self.retry_interval_s
        if isinstance(e, RobotUnavailable):
            link, msg = RobotLink.UNAVAILABLE, "robot animation disconnected (SO-101 API unreachable)"
        elif isinstance(e, RobotRejected):
            link, msg = RobotLink.REJECTED, f"robot refused Talk Mode: {e}"
        elif isinstance(e, RobotAuthError):
            link, msg = RobotLink.AUTH, "robot API token rejected"
        else:
            link, msg = RobotLink.ERROR, str(e)
        if self.status.link is not link:
            log.warning("%s", msg)
        self._set(link, msg)

    def _set(self, link: RobotLink, detail: str) -> None:
        self.status = TalkStatus(link, self._robot_on, detail)
        self._publish()

    def _publish(self) -> None:
        self.status = TalkStatus(self.status.link, self._robot_on, self.status.detail)
        if self._on_status is not None:
            self._on_status(self.status)

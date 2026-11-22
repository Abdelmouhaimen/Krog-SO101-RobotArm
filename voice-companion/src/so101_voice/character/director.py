"""BehaviorDirector: the companion's single owner of SO-101 arm behavior (robot repo unchanged).

The LLM decides what to express, the director coordinates, and the robot API executes.

**Priorities** (highest first):
1. **Safety** (not a level): faults, Emergency Hold, robot readiness, and the real-arm gate always win.
   Nothing is sent while they block, and nothing restarts automatically after them.
2. ``COMMAND``: an explicit pose or Go Rest.
3. ``REACTION``: a deliberate one-shot the LLM chose (``wavy``, ``comic_rage`` ...).
4. ``AGENT``: an idle the LLM chose (a mood) or a reaction's planned continuation. It holds through
   the reply and yields to automatic idles when the user starts the next turn.
5. ``AUTO``: speech-phase idles (listening, talking, calm after speaking). They replace only other
   ``AUTO`` idles, never a running reaction, command or (fresh) agent choice.

**Reaction → planned idle.** Playing a one-shot records its ``next_idle`` together with the command
id and a new *epoch*. The monitor polls ``GET /commands/{id}``, so completion is the robot's own
``completed`` state, never a timer (entry transitions make real runs longer than the recording). When
the reaction completes, the planned idle is sent if, and only if:
- the epoch is still current (no newer action was sent);
- the robot is ready (no fault or Emergency Hold, not resting, no operator takeover).

A superseded, cancelled or failed reaction drops its continuation. No rest is ever inserted: the robot
API supersedes motion directly.

**Suspension.** After an intentional Go Rest or pose, or when the operator takes over in the Studio,
automatic idles stop until the next deliberate behavior or a new session.

**Duplicates and retries.** Every request has an ``Idempotency-Key``: ``call-<id>`` for LLM tools, a
fresh id for automatic idles. A timeout is retried with the same key and reconciled through the ledger
(``RobotMotion``), never re-sent blindly.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from so101_voice.character.library import BehaviorInfo, BehaviorLibrary, UnknownBehavior
from so101_voice.config import CharacterConfig, RobotToolsConfig
from so101_voice.robot.actions import RobotActions, Transition
from so101_voice.robot.client import RobotAPIClient, RobotAPIError, RobotRejected

log = logging.getLogger(__name__)

TERMINAL = {"completed", "superseded", "cancelled", "failed", "rejected"}
Kind = Literal["idle", "reaction", "pose", "rest"]


class Priority(enum.IntEnum):
    AUTO = 1
    AGENT = 2
    REACTION = 3
    COMMAND = 4


class ActionRefused(RuntimeError):
    """The action was not sent (robot not ready, fault, Emergency Hold, real-arm gate ...)."""


@dataclass
class Active:
    command_id: str
    label: str  # behavior name, "pose:<name>" or "rest"
    kind: Kind
    priority: Priority
    epoch: int
    started: float
    next_idle: str | None = None
    state: str = "running"
    source: str = "agent"  # agent | auto | continuation | session

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


class BehaviorDirector:
    def __init__(
        self,
        robot: RobotAPIClient,
        motion: RobotActions,
        library: BehaviorLibrary,
        character: CharacterConfig,
        tools: RobotToolsConfig,
        *,
        on_notice: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.robot = robot
        self.motion = motion
        self.library = library
        self.cfg = character
        self.tools_cfg = tools
        self._notice = on_notice or (lambda level, text: None)
        self._clock = clock
        self._rng = rng or random.Random()
        self._lock = asyncio.Lock()
        self.active: Active | None = None
        self.epoch = 0
        self.phase = "idle"
        self.before_exclusive_motion: Callable[[], Awaitable[None]] | None = None
        self.suspended: str | None = None
        self.mood: str | None = None
        self._mood_until = 0.0
        self._last_auto_switch = -1e9
        self._status: dict[str, Any] | None = None
        self._status_t = -1e9
        self._link_ok = True
        self._our_ids: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._auto_task: asyncio.Task[None] | None = None
        self.dispatched: list[tuple[str, str, str]] = []  # (source, label, command id): demo / tests
        self.continuations_sent = 0
        self.continuations_dropped = 0

    # ================================================================== lifecycle
    async def start(self) -> None:
        try:
            await self.library.refresh()
        except Exception as e:
            self._notice("warning", f"robot behavior library unavailable: {e}")
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._monitor(), name="director-monitor"),
            loop.create_task(self._refresher(), name="director-library"),
        ]

    async def aclose(self) -> None:
        for t in [*self._tasks, *([self._auto_task] if self._auto_task else [])]:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        await self._rest_if_owned("app exit")

    # ================================================================== conversation hooks
    def on_phase(self, phase: str) -> None:
        """Conversation phase changed (lower-case ``Phase`` value)."""
        prev, self.phase = self.phase, phase
        if prev in ("idle", "error") and phase in ("connecting", "listening", "speaking"):
            self._spawn(self.session_started())
            return
        if phase == "idle" and prev not in ("idle", "error"):
            self._spawn(self.session_stopped())
            return
        if phase == "user_speaking" and self.active is not None and self.active.priority is Priority.AGENT:
            self.active.priority = Priority.AUTO  # the user's new turn: the agent's mood idle may yield
        delay = {"user_speaking": 0.6, "speaking": 0.4, "listening": 1.2}.get(phase)
        if delay is not None and self.cfg.automatic_idles:
            self._schedule_auto(delay)

    async def session_started(self) -> None:
        if self.suspended in ("resting", "holding pose", "operator"):
            self.suspended = None  # a new session is a fresh start
        name = self.cfg.session_start_reaction
        if name and self.library.has(name):
            with contextlib.suppress(ActionRefused, RobotAPIError, UnknownBehavior):
                await self._play(
                    name, next_idle=self.cfg.default_idle, key=f"auto-{uuid.uuid4().hex}", source="session"
                )
                return
        await self._auto_select(force=True)

    async def session_stopped(self) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
        await self._rest_if_owned("session ended")

    # ================================================================== intentional actions (tools)
    async def play(
        self,
        name: str,
        *,
        key: str,
        next_idle: str | None = None,
        speed: float | None = None,
        repeat: int | Literal["until_stopped"] | None = None,
    ) -> dict[str, Any]:
        return await self._play(
            name, next_idle=next_idle, speed=speed, repeat=repeat, key=key, source="agent"
        )

    async def go_pose(self, name: str, *, key: str, transition: Transition = "normal") -> dict[str, Any]:
        if self.before_exclusive_motion is not None:
            await self.before_exclusive_motion()
        pose = self.library.resolve_pose(name)
        if pose.usable is False:
            raise ActionRefused(f"pose {pose.name!r} is not usable with the connected arm's calibration")
        async with self._lock:
            await self._gate("go_pose")
            rec = await self.motion.go_pose(pose.id, idempotency_key=key, transition=transition)
            self._record(rec, f"pose:{pose.name}", "pose", Priority.COMMAND, None, "agent")
            self.suspended = "holding pose"
        return self._result(rec, f"moving to pose {pose.name}; it holds there (automatic idles paused)")

    async def go_rest(self, *, key: str, source: str = "agent") -> dict[str, Any]:
        if self.before_exclusive_motion is not None:
            await self.before_exclusive_motion()
        async with self._lock:
            await self._gate("go_rest")
            rec = await self.motion.go_rest(idempotency_key=key)
            self._record(rec, "rest", "rest", Priority.COMMAND, None, source)
            self.suspended = "resting"
        return self._result(rec, "returning to rest; automatic idles paused until the next gesture")

    async def robot_status(self) -> dict[str, Any]:
        st = await self._fetch_status()
        a = self.active
        return {
            "ready": (st.get("ready") or {}).get("play_behavior"),
            "fault": st.get("fault"),
            "emergency_hold_latched": st.get("emergency_hold_latched"),
            "robot": (st.get("robot") or {}).get("driver"),
            "real_arm_motion_allowed": self.tools_cfg.allow_physical,
            "current": None
            if a is None
            else {"behavior": a.label, "kind": a.kind, "state": a.state, "then": a.next_idle},
            "robot_behavior": st.get("behavior"),
            "automatic_idles": "paused: " + self.suspended if self.suspended else "on",
            "mood": self.mood,
        }

    def describe_library(self) -> dict[str, Any]:
        from so101_voice.character.persona import GESTURE_MEANINGS

        def item(b: BehaviorInfo) -> dict[str, Any]:
            return {
                "name": b.name,
                "meaning": GESTURE_MEANINGS.get(b.name) or b.description or b.kind,
                "seconds": round(b.duration_s, 1),
            }

        return {
            "idles_loop_until_replaced": [item(b) for b in self.library.idles()],
            "reactions_play_once": [item(b) for b in self.library.reactions()],
            "missing": [n for n in self.library.expected if not self.library.has(n)],
        }

    # ================================================================== internals: dispatch
    async def _play(
        self,
        name: str,
        *,
        key: str,
        source: str,
        next_idle: str | None = None,
        speed: float | None = None,
        repeat: int | Literal["until_stopped"] | None = None,
        priority: Priority | None = None,
    ) -> dict[str, Any]:
        b = self.library.resolve(name)  # raises UnknownBehavior: nothing is sent
        will_loop = repeat == "until_stopped" or (repeat is None and b.loops)
        nxt: BehaviorInfo | None = None
        if next_idle:
            nxt = self.library.resolve(next_idle)
            if not nxt.loops:
                raise UnknownBehavior(f"next_idle {next_idle!r} is a one-shot reaction, not a looping idle")
            if will_loop:
                raise UnknownBehavior("next_idle only applies to a one-shot reaction")
        kind: Kind = "idle" if will_loop else "reaction"
        if priority is None:
            priority = (
                (Priority.AGENT if kind == "idle" else Priority.REACTION)
                if source != "auto"
                else Priority.AUTO
            )
        async with self._lock:
            a = self.active
            if (
                kind == "idle"
                and a is not None
                and not a.terminal
                and a.label == b.name
                and speed is None
                and repeat is None
            ):
                a.priority = max(a.priority, priority)  # already looping: do not restart it
                if source == "agent":
                    self._set_mood(b.name)
                return {
                    "command_id": a.command_id,
                    "state": a.state,
                    "behavior": b.name,
                    "note": "already playing",
                }
            t0 = self._clock()
            await self._gate("play_behavior")
            t1 = self._clock()
            rec = await self.motion.play_behavior(b.id, idempotency_key=key, speed=speed, repeat=repeat)
            logging.getLogger("so101_voice.trace").info(
                "robot %s: status check %.0f ms, play accepted %.0f ms (state %s)",
                b.name,
                (t1 - t0) * 1000,
                (self._clock() - t1) * 1000,
                rec.get("state"),
            )
            self._record(rec, b.name, kind, priority, nxt.name if nxt else None, source)
            if source in ("agent", "continuation", "session") and kind == "idle":
                self._set_mood(b.name)
            if source == "agent":
                self.suspended = None
        then = f"; then {nxt.name} loops" if nxt else ""
        return self._result(
            rec, f"playing {b.name} ({'loops until replaced' if kind == 'idle' else 'once'}{then})"
        )

    def _record(
        self, rec: dict[str, Any], label: str, kind: Kind, priority: Priority, nxt: str | None, source: str
    ) -> None:
        """Called with the lock held, right after the robot accepted (or replayed) the command."""
        state = str(rec.get("state") or "running")
        cid = str(rec.get("id") or "")
        if state == "rejected":
            raise ActionRefused(f"the robot refused it: {rec.get('detail', 'rejected')}")
        self.epoch += 1  # any older completion is now stale
        self._our_ids.add(cid)
        self.active = Active(cid, label, kind, priority, self.epoch, self._clock(), nxt, state, source)
        self.dispatched.append((source, label, cid))
        log.info("robot %s %s (%s, cmd %s%s)", kind, label, source, cid, f", then {nxt}" if nxt else "")

    def _result(self, rec: dict[str, Any], summary: str) -> dict[str, Any]:
        return {
            "command_id": rec.get("id"),
            "state": rec.get("state"),
            "summary": summary,
            "replayed": bool(rec.get("idempotent_replay")),
        }

    async def _gate(self, action: str) -> None:
        """Safety first: refuse (raise) unless the robot is ready for ``action`` right now."""
        try:
            st = await self._fetch_status(max_age_s=0.0)
        except RobotAPIError as e:
            raise ActionRefused(f"robot API unavailable: {e}") from e
        if st.get("fault"):
            raise ActionRefused(f"robot fault latched: {st['fault']}")
        if st.get("emergency_hold_latched"):
            raise ActionRefused("Emergency Hold is latched; the operator must release it in the Studio")
        driver = str((st.get("robot") or {}).get("driver") or "")
        if driver and "fake" not in driver.lower() and not self.tools_cfg.allow_physical:
            raise ActionRefused(
                "a real arm is connected and real-arm behaviors are switched off for this session "
                "([robot_tools] allow_physical = false or --no-physical-robot)"
            )
        ready = (st.get("ready") or {}).get(action) or {}
        if not ready.get("ok"):
            raise ActionRefused(f"robot not ready for {action}: {ready.get('reason') or 'unknown'}")

    async def _fetch_status(self, max_age_s: float = 1.0) -> dict[str, Any]:
        if self._status is None or self._clock() - self._status_t > max_age_s:
            self._status = await self.robot.status()
            self._status_t = self._clock()
        return self._status

    # ================================================================== internals: automatic idles
    def _schedule_auto(self, delay: float) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
        phase = self.phase

        async def run() -> None:
            await asyncio.sleep(delay)
            wait = self.cfg.min_switch_interval_s - (self._clock() - self._last_auto_switch)
            if wait > 0:
                await asyncio.sleep(wait)
            if self.phase == phase:
                await self._auto_select()

        self._auto_task = asyncio.get_running_loop().create_task(run(), name="director-auto")

    def _auto_choice(self) -> str | None:
        phase = self.phase
        if (
            phase in ("user_speaking", "speaking", "listening", "connecting", "interrupted")
            and self.mood
            and self._clock() < self._mood_until
            and self.library.has(self.mood)
        ):
            return self.mood
        if phase == "user_speaking":
            return self.cfg.listening_idle
        if phase == "speaking":
            return self.cfg.talking_idle
        if phase in ("listening", "connecting", "interrupted"):
            a = self.active
            calm = [n for n in self.cfg.calm_idles if self.library.has(n)]
            if a is not None and not a.terminal and a.label in calm:
                return a.label  # already calm: stay in the same animation
            return self._rng.choice(calm) if calm else self.cfg.default_idle
        return None

    async def _auto_select(self, *, force: bool = False) -> None:
        if not self.tools_cfg.enabled or self.suspended:
            return
        name = self._auto_choice()
        if not name or not self.library.has(name):
            return
        a = self.active
        if a is not None and not a.terminal:
            if a.label == name or (a.priority > Priority.AUTO and not force):
                return  # never override a reaction, command or fresh agent choice
            if force and a.priority > Priority.AUTO:
                return
        try:
            await self._play(name, key=f"auto-{uuid.uuid4().hex}", source="auto")
            self._last_auto_switch = self._clock()
        except (ActionRefused, RobotAPIError, UnknownBehavior) as e:
            log.info("automatic idle %s not sent: %s", name, e)

    def _set_mood(self, name: str) -> None:
        self.mood = name
        self._mood_until = self._clock() + self.cfg.mood_ttl_s

    # ================================================================== internals: monitoring
    async def _monitor(self) -> None:
        next_check = 0.0
        while True:
            await asyncio.sleep(self.tools_cfg.command_poll_s)
            a = self.active
            now = self._clock()
            due = now >= next_check
            if a is None or a.terminal:
                if due:
                    next_check = now + self.tools_cfg.status_poll_s
                    with contextlib.suppress(RobotAPIError):
                        await self._fetch_status(max_age_s=0.0)
                continue
            if a.kind != "reaction" and not due:
                continue  # loops and holds only need a slow check (supersession by the operator)
            if due:
                next_check = now + self.tools_cfg.status_poll_s
            try:
                rec = await self.robot.command(a.command_id)
                if not self._link_ok:
                    self._link_ok = True
                    self._notice("info", "robot animation reconnected")
            except RobotAPIError as e:
                if self._link_ok:
                    self._link_ok = False
                    self._notice("warning", f"robot animation disconnected ({e}); voice continues")
                continue
            await self._observe(a, rec)

    async def _observe(self, a: Active, rec: dict[str, Any]) -> None:
        state = str(rec.get("state") or a.state)
        if state == a.state:
            return
        a.state = state
        if state not in TERMINAL:
            return
        if state == "completed":
            if a.kind == "reaction":
                await self._continue_after(a)
            return
        # superseded / cancelled / failed: the planned continuation is dropped
        if a.next_idle:
            self.continuations_dropped += 1
            log.info("continuation %s dropped: %s was %s", a.next_idle, a.label, state)
        by = rec.get("superseded_by") or rec.get("cancelled_by")
        if by and by not in self._our_ids:
            with contextlib.suppress(RobotAPIError):
                other = await self.robot.command(str(by))
                if other.get("source") == "studio":
                    self.suspended = "operator"
                    self._notice("info", "the operator took over in SO-101 Studio: automatic idles paused")
        if state in ("cancelled", "failed"):
            self.suspended = self.suspended or f"robot {state}: {rec.get('detail', '')}".strip()

    async def _continue_after(self, a: Active) -> None:
        async with self._lock:
            if a.epoch != self.epoch:
                self.continuations_dropped += 1
                return  # a newer action exists: an old completion never restarts an idle
        name = a.next_idle
        source = "continuation"
        if not name:
            name = self._auto_choice() or self.cfg.default_idle
            source = "auto"
        if self.suspended or not self.library.has(name):
            self.continuations_dropped += 1
            return
        try:
            async with self._lock:
                if a.epoch != self.epoch:
                    self.continuations_dropped += 1
                    return
            await self._play(
                name,
                key=f"next-{a.command_id}",  # deterministic: a repeated completion cannot send it twice
                source=source,
                priority=Priority.AGENT if source == "continuation" else Priority.AUTO,
            )
            self.continuations_sent += 1
        except (ActionRefused, RobotAPIError, UnknownBehavior) as e:
            self.continuations_dropped += 1
            log.info("continuation %s not sent: %s", name, e)

    async def _refresher(self) -> None:
        while True:
            await asyncio.sleep(self.tools_cfg.library_refresh_s)
            with contextlib.suppress(Exception):
                await self.library.refresh()

    async def _rest_if_owned(self, why: str) -> None:
        """Idles loop forever: when the session ends, stop the one the companion started."""
        a = self.active
        if not self.cfg.rest_on_stop or a is None or a.terminal or a.kind == "rest" or self.suspended:
            return
        try:
            await asyncio.wait_for(self.go_rest(key=f"stop-{uuid.uuid4().hex}", source="session"), 2.0)
            log.info("robot sent to rest (%s)", why)
        except (ActionRefused, RobotAPIError, RobotRejected, TimeoutError) as e:
            log.info("rest on %s not sent: %s", why, e)

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.append(t)
        t.add_done_callback(lambda task: self._tasks.remove(task) if task in self._tasks else None)

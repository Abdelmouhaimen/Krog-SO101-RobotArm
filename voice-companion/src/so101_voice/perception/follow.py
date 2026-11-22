"""Explicit Follow Me switch and asynchronous bearing forwarding; never selects animations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from so101_voice.character.director import BehaviorDirector
from so101_voice.config import FollowConfig
from so101_voice.perception.lan import PerceptionClient
from so101_voice.robot.follow import FollowAPI

log = logging.getLogger(__name__)


class FollowController:
    def __init__(
        self,
        perception: PerceptionClient,
        follow: FollowAPI | None,
        cfg: FollowConfig,
        director: BehaviorDirector | None = None,
        notice: Callable[[str, str], None] | None = None,
    ):
        self.perception, self.follow, self.cfg, self.director = perception, follow, cfg, director
        self.requested = False
        self.session = False
        self.generation = 0
        self.reason = "disabled"
        self.enabled = False
        self._exclusive_motion = False
        self._pending_stop = False
        self._last: tuple[int, str, int] | None = None
        self._candidate_since: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._notice = notice or (lambda level, message: None)
        self._diagnostic = ""
        self._tracked_once = False
        self._last_error: str | None = None

    @property
    def status(self) -> str:
        if not self.requested or not self.session:
            return "OFF"
        if self.enabled and not self._pending_stop and self.perception.face_ready():
            return "TRACKING"
        return "FACE LOST" if self._tracked_once else "SEARCHING"

    async def set_enabled(self, enabled: bool) -> dict[str, object]:
        if enabled and not self.session:
            raise ValueError("Start a voice session before enabling Follow Me")
        self._notice("info", f"Follow Me requested {'ON' if enabled else 'OFF'}")
        if enabled and self.requested and self.enabled:
            return {"requested": True, "enabled": self.enabled, "status": self.status, "reason": self.reason}
        if self.requested != enabled:
            self.generation += 1
            self._candidate_since = None
            self._last = None
            self._tracked_once = False
        self.requested = enabled
        self._exclusive_motion = False
        if enabled:
            # Intention can stay ON while data/hardware is unavailable; step gates all movement.
            await self.step()
        else:
            async with self._lock:
                await self._release()
            self.reason = "disabled"
        return {
            "requested": self.requested,
            "enabled": self.enabled,
            "status": self.status,
            "reason": self.reason,
        }

    def on_phase(self, phase: str) -> None:
        running = phase not in ("idle", "error")
        if self.session != running:
            self.generation += 1
            self._candidate_since = None
            self.session = running
            self.requested = running
            self._last = None
            self._tracked_once = False
            self._exclusive_motion = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="follow-me")

    async def _release(self) -> None:
        self._candidate_since = None
        if self.follow and (self.enabled or self._pending_stop):
            self._pending_stop = True
            await self.follow.disable()
            self.enabled = self._pending_stop = False

    async def step(self) -> None:
        async with self._lock:
            if self._pending_stop:
                await self._release()
                self._last = None
                return
            generation = self.generation
            status = await self.perception.poll()
            received_mono = self.perception.received_mono
            face = status.face
            key = (self.perception.generation, face.session, face.seq)
            previous, self._last = self._last, key
            valid = (
                not self._exclusive_motion
                and self.session
                and self.requested
                and self.cfg.enabled
                and self.cfg.calibration_valid
                and self.follow is not None
                and self.perception.connected
                and face.found
                and face.age_s is not None
                and face.age_s + time.monotonic() - received_mono <= 0.5
                and face.angle_deg is not None
            )
            if not valid or generation != self.generation:
                self.reason = "disabled/session/calibration/observation gate"
                await self._release()
                return
            # First packet after any reconnect or enable request is a baseline, never replayed.
            if previous is None or previous[0] != key[0]:
                await self._release()
                self.reason = "awaiting new observation after reconnect"
                return
            if self.director is None or self.director.suspended:
                await self._release()
                self.reason = "behavior director suspended or unavailable"
                return
            assert self.follow is not None
            assert face.age_s is not None and face.angle_deg is not None
            st = await self.follow.robot.status()
            driver = str((st.get("robot") or {}).get("driver") or "")
            command = st.get("active_command") or {}
            if command.get("source") == "studio":
                self.director.suspended = "operator"
            active = self.director.active if hasattr(self.director, "active") else None
            if active is not None and not active.terminal:
                record = await self.follow.robot.command(active.command_id)
                if record.get("state") in ("superseded", "cancelled", "failed"):
                    # A play request can supersede this idle while the ledger read is
                    # in flight. Wait for its acknowledgement to be recorded before
                    # deciding whether ownership was lost; never suspend a newer action.
                    async with getattr(self.director, "_lock", contextlib.nullcontext()):
                        replaced_by = record.get("superseded_by")
                        if self.director.active is not active or (
                            replaced_by and replaced_by in getattr(self.director, "_our_ids", ())
                        ):
                            self.reason = "behavior changed; refreshing"
                            return
                        self.director.suspended = "operator or cancelled behavior"
            ready = (st.get("ready") or {}).get("follow") or {}
            if (
                self.director.suspended
                or not ready.get("ok")
                or st.get("fault")
                or st.get("emergency_hold_latched")
                or not driver
                or ("fake" not in driver.lower() and not self.cfg.allow_physical)
                or command.get("source") == "studio"
                or command.get("kind") == "go_rest"
            ):
                self.reason = "robot safety/operator gate"
                await self._release()
                return
            if (
                generation != self.generation
                or not self.session
                or not self.requested
                or self.director.suspended
                or face.age_s + time.monotonic() - received_mono > 0.5
            ):
                await self._release()
                return
            if key == previous:
                self.reason = "awaiting new observation"
                return
            now = time.monotonic()
            if self._candidate_since is None:
                self._candidate_since = now
            if now - self._candidate_since < self.cfg.acquire_s:
                self.reason = "acquiring stable face"
                return
            if not self.enabled:
                existing = await self.follow.status()
                if existing.get("enabled"):
                    self.reason = "Follow Me already owned externally"
                    self._candidate_since = None
                    return
                if (
                    generation != self.generation
                    or self._exclusive_motion
                    or face.age_s + time.monotonic() - received_mono > 0.5
                ):
                    return
                # Mark before awaiting: shutdown/cancellation must release even an uncertain enable.
                self._pending_stop = True
                await self.follow.enable(face.angle_deg)
                self.enabled = True
                self._tracked_once = True
                self._pending_stop = False
            else:
                await self.follow.target(face.angle_deg)
            if generation != self.generation:
                await self._release()
            self.reason = "tracking" if self.enabled else "released"

    async def _run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                if self.requested or self.enabled or self._pending_stop:
                    await self.step()
            except Exception as exc:
                error = f"Follow Me API unavailable ({type(exc).__name__}); retrying safely"
                if error != self._last_error:
                    self._notice("warning", error)
                self._last_error = error
                self.reason = "tracking API unavailable; release pending"
                with contextlib.suppress(Exception):
                    await self._release()
            else:
                self._last_error = None
            diagnostic = f"Follow Me requested={self.requested}, enabled={self.enabled}: {self.reason}"
            if diagnostic != self._diagnostic:
                log.debug("%s", diagnostic)
                self._diagnostic = diagnostic
            await asyncio.sleep(max(0.001, 1 / self.cfg.target_rate_hz - time.monotonic() + started))

    async def release_for_motion(self) -> None:
        """Release pan before deliberate Go Rest/pose, including an in-flight enable."""
        self.generation += 1
        self._exclusive_motion = True
        self.requested = False
        async with self._lock:
            await self._release()

    async def aclose(self) -> None:
        self.on_phase("idle")
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            async with self._lock:
                await self._release()
        finally:
            if self.follow:
                await self.follow.robot.aclose()

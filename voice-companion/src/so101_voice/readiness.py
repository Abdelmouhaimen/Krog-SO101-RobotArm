"""Read-only startup/live service checklist. Never starts providers, audio capture or robot control."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from so101_voice.config import AppConfig
from so101_voice.perception.lan import PerceptionClient
from so101_voice.robot.client import RobotAPIClient


@dataclass(frozen=True)
class Check:
    status: Literal["OK", "Waiting", "Unavailable", "Error"]
    text: str


class Readiness:
    def __init__(
        self,
        cfg: AppConfig,
        robot: RobotAPIClient | None,
        perception: PerceptionClient | None,
        *,
        credentials: tuple[bool, bool],
        audio: Check,
    ):
        self.cfg, self.robot, self.perception = cfg, robot, perception
        self.credentials, self.audio = credentials, audio
        self.provider_check: Callable[[], Check] | None = None
        self.checks: dict[str, Check] = {}
        self.robot_status: dict[str, object] | None = None
        self._task: asyncio.Task[None] | None = None

    async def _robot(self) -> dict[str, Check]:
        if self.robot is None:
            return {
                "robot": Check("Unavailable", "Robot API disabled or token not configured"),
                "arm": Check("Unavailable", "Arm connection unknown"),
                "physical": Check("Unavailable", "Physical control unavailable"),
            }
        try:
            status = await self.robot.status()
        except Exception:
            self.robot_status = None
            return {
                "robot": Check("Unavailable", "Robot API unavailable or authentication rejected"),
                "arm": Check("Unavailable", "Arm connection unknown"),
                "physical": Check("Unavailable", "Physical control unavailable"),
            }
        self.robot_status = status
        driver = str((status.get("robot") or {}).get("driver") or "unknown")
        connection = str(status.get("connection") or "unknown")
        connected = connection not in ("DISCONNECTED", "unknown") and driver != "unknown"
        fake = "fake" in driver.lower()
        physical = Check("Waiting", "Physical movement unauthorized in companion")
        if status.get("fault") or status.get("emergency_hold_latched"):
            physical = Check("Error", "Physical control faulted or Emergency Hold latched")
        elif fake:
            physical = Check("Waiting", "Fake hardware: no physical arm control")
        elif not connected:
            physical = Check("Unavailable", "Physical arm disconnected")
        elif self.cfg.robot_tools.allow_physical and status.get("physical_control_enabled"):
            ready = (status.get("ready") or {}).get("play_behavior") or {}
            physical = Check(
                "OK" if ready.get("ok") else "Waiting",
                "Physical movement ready"
                if ready.get("ok")
                else f"Physical movement not ready: {ready.get('reason') or connection}",
            )
        return {
            "robot": Check("OK", "Robot API reachable"),
            "arm": Check(
                "OK" if connected else "Unavailable",
                f"Arm {'connected' if connected else 'disconnected'} — {driver}, {connection}",
            ),
            "physical": physical,
        }

    async def _perception(self) -> dict[str, Check]:
        p = self.perception
        if p is None:
            return {
                "receiver": Check("Unavailable", "LAN receiver disabled"),
                "screen": Check("Waiting", "Gaming screen unavailable"),
                "face": Check("Waiting", "Face angle unavailable"),
                "calibration": Check("Waiting", "Face tracking not ready"),
            }
        await p.poll()
        status = p.snapshot()
        receiver = (
            Check("OK", "Receiver reachable; authentication accepted")
            if p.connected
            else Check(
                "Error" if p.reachable else "Unavailable",
                "Receiver authentication rejected"
                if p.auth == "rejected"
                else "Receiver unavailable or invalid response",
            )
        )

        def observation(available: bool, age: float | None, name: str) -> Check:
            age_text = f" — last observation {age:.1f}s ago" if age is not None else ""
            return Check(
                "OK" if available else "Waiting",
                name + (" available" if available else " unavailable; waiting for gaming PC") + age_text,
            )

        calibrated = self.cfg.follow.calibration_valid
        return {
            "receiver": receiver,
            "screen": observation(status.screen.available, status.screen.age_s, "Gaming screen"),
            "face": observation(status.face.found, status.face.age_s, "Webcam face angle"),
            "calibration": Check(
                "OK" if calibrated else "Waiting",
                "Camera-to-robot calibration checked"
                if calibrated
                else "Face tracking not ready: camera-to-robot calibration unchecked",
            ),
        }

    async def refresh(self) -> None:
        robot, perception = await asyncio.gather(self._robot(), self._perception())
        dg, oa = self.credentials
        self.checks = {
            **robot,
            **perception,
            "audio": self.audio,
            "credentials": Check(
                "OK" if dg and oa else "Unavailable",
                f"Deepgram credentials {'configured' if dg else 'missing'}, "
                f"OpenAI {'configured' if oa else 'missing'}",
            ),
            "providers": (
                self.provider_check()
                if self.provider_check
                else Check(
                    "Waiting", "Providers idle — Start connects voice; OpenAI is verified on the first reply"
                )
            ),
        }

    def text(self) -> str:
        return "\n".join(f"[{check.status}] {check.text}" for check in self.checks.values())

    async def start(self) -> None:
        await self.refresh()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="readiness")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.refresh()

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

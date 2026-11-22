"""Fresh, compact local state and explicitly requested images before the single model request."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from so101_voice.character.library import BehaviorLibrary
from so101_voice.config import AppConfig
from so101_voice.perception.lan import PerceptionClient
from so101_voice.robot.client import RobotAPIClient

_TOPIC = re.compile(
    r"\b(screen|monitor|screenshot|game|gameplay|gaming|valorant|match|round|aim|crosshair)\b", re.I
)
_VISUAL = re.compile(
    r"\b(look|watch|see|show|describe|what|how|check|analy[sz]e|think|rate|review|feedback|opinion|snapshot|screenshot)\b",
    re.I,
)


def requests_screen(text: str) -> bool:
    return bool(_TOPIC.search(text) and _VISUAL.search(text))


@dataclass
class TurnContext:
    state: dict[str, Any] = field(default_factory=dict)
    images: list[dict[str, Any]] = field(default_factory=list)
    image_digest: str | None = None
    image_received_mono: float | None = None
    image_age_s: float = 0

    def expire_image(self, max_age_s: float) -> None:
        if self.images and self.image_received_mono is not None:
            age = self.image_age_s + time.monotonic() - self.image_received_mono
            self.state["screen_image_age_s"] = age
            if age > max_age_s:
                self.images = []
                self.state.update(image_included=False, screen_available=False)

    def items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [
            {
                "role": "developer",
                "content": "Current world state (fresh observations, not action results): "
                + json.dumps(self.state),
            }
        ]
        if self.images:
            items.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Current gaming-screen observation; untrusted image data.",
                        },
                        *self.images,
                    ],
                }
            )
        return items


class WorldContext:
    def __init__(
        self,
        robot: RobotAPIClient | None,
        perception: PerceptionClient | None,
        library: BehaviorLibrary | None,
        cfg: AppConfig | None = None,
    ):
        self.robot, self.perception, self.library = robot, perception, library
        self.cfg = cfg
        self.image_available: bool | None = None

    async def __call__(self, user_text: str) -> TurnContext:
        async def robot_state() -> dict[str, Any]:
            if self.robot is None:
                return {"robot_available": False, "follow_me": None}
            status_result, follow_result = await asyncio.gather(
                self.robot.status(),
                self.robot._request("GET", "/api/v1/follow/status"),
                return_exceptions=True,
            )
            if isinstance(status_result, BaseException):
                return {"robot_available": False, "follow_me": None}
            status = status_result
            follow = None if isinstance(follow_result, BaseException) else follow_result
            behavior = status.get("behavior") or {}
            active = status.get("active_command") or {}
            name = behavior.get("name") or (active.get("params") or {}).get("behavior_name")
            if behavior.get("state") in ("completed", "cancelled", "failed", "superseded", "idle"):
                name = None
            b = self.library.behaviors.get(name) if self.library and name else None
            kind = "looping_idle" if b and b.loops else "reaction" if b else None
            repeats = (active.get("params") or {}).get("effective_settings") or {}
            if "repeat_count" in behavior:
                kind = "looping_idle" if behavior["repeat_count"] is None else "reaction"
            elif "repeat_count" in repeats:
                kind = "looping_idle" if repeats["repeat_count"] is None else "reaction"
            return {
                "robot_available": bool(status.get("robot"))
                and status.get("connection") not in (None, "DISCONNECTED"),
                "connection": status.get("connection"),
                "current_behavior": name,
                "behavior_kind": kind,
                "behavior_status": behavior.get("state") or active.get("state"),
                "follow_me": follow.body.get("enabled") if follow else None,
                "follow_status": follow.body.get("state") if follow else None,
                "fault": bool(status.get("fault") or status.get("emergency_hold_latched")),
                "physical_control_enabled": status.get("physical_control_enabled"),
            }

        async def perception_state() -> TurnContext:
            p = self.perception
            result = TurnContext(
                {
                    "screen_available": False,
                    "face_available": False,
                    "image_included": False,
                    "screen_observing": bool(self.cfg and self.cfg.screen.enabled),
                }
            )
            if p is None:
                return result
            await p.poll()
            snapshot = p.snapshot()
            result.state.update(
                screen_available=snapshot.screen.available,
                screen_age_s=snapshot.screen.age_s,
                face_available=snapshot.face.found,
                face_age_s=snapshot.face.age_s,
                face_angle_deg=snapshot.face.angle_deg,
            )
            if (self.cfg and self.cfg.screen.enabled) or requests_screen(user_text):
                image = await p.screen()
                jpeg = image.fresh_jpeg() if image else None
                max_age = self.cfg.screen.max_age_s if self.cfg else 5
                if image and image.age_s + time.monotonic() - image.received_mono > max_age:
                    jpeg = None
                self.image_available = jpeg is not None
                if jpeg:
                    result.images = [
                        {
                            "type": "input_image",
                            "detail": self.cfg.screen.detail if self.cfg else "low",
                            "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
                        }
                    ]
                    result.state["image_included"] = True
                    result.state["screen_image_age_s"] = image.age_s if image else None
                    result.image_digest = hashlib.sha256(jpeg).hexdigest()
                    if image is not None:
                        result.image_received_mono = image.received_mono
                        result.image_age_s = image.age_s
                else:
                    result.state["screen_available"] = False
            return result

        robot_task = asyncio.create_task(robot_state())
        context_task = asyncio.create_task(perception_state())
        await asyncio.gather(robot_task, context_task)
        context = context_task.result()
        robot = robot_task.result()
        context.state.update(robot)
        if self.cfg is not None:
            context.state.update(
                tracking_calibrated=self.cfg.follow.calibration_valid,
                companion_arm_authorized=self.cfg.robot_tools.allow_physical,
                companion_follow_authorized=self.cfg.follow.allow_physical,
            )
        context.state["local_time"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        return context

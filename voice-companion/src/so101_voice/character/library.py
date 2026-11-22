"""Dynamic SO-101 behavior and pose library, discovered from the robot API (never hardcoded).

``GET /api/v1/behaviors`` returns for each behavior:
* ``id`` (stable), ``name``, ``description``, ``duration_s``;
* ``saved_settings`` (``repeat_count``: ``null`` = until stopped, ``after.action``, trims, speed).

``GET /api/v1/poses`` returns named poses with ``usable_on_connected_arm``.

* **Kind** is inferred from the saved repetitions: ``repeat_count: null`` is a looping **idle**, a finite
  count is a one-shot **reaction**. A behavior recorded later in SO-101 Studio is therefore usable
  without code changes.
* **Names** resolve to ids from the latest response: exact name first, then case-insensitive, then the
  id itself. If two behaviors share a name, the most recently updated one wins (and it is reported).
* **Missing behaviors:** an expected behavior the robot no longer has is reported and simply not
  offered.
* **Refresh:** the library is refreshed at start, every ``library_refresh_s``, and on demand.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from so101_voice.robot.client import RobotAPIClient

log = logging.getLogger(__name__)


class UnknownBehavior(LookupError):
    pass


@dataclass(frozen=True)
class BehaviorInfo:
    id: str
    name: str
    description: str
    duration_s: float
    repeat_count: int | None  # saved: None = repeat until stopped
    after: str  # saved ending: rest | hold | pose
    speed: float
    trimmed: bool
    updated_at: str = ""

    @property
    def loops(self) -> bool:
        return self.repeat_count is None

    @property
    def kind(self) -> str:
        return "idle" if self.loops else "reaction"


@dataclass(frozen=True)
class PoseInfo:
    id: str
    name: str
    usable: bool | None  # None: no arm connected, so not checked


@dataclass(frozen=True)
class LibraryChange:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    missing_expected: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def _info(raw: dict[str, Any]) -> BehaviorInfo:
    s = raw.get("saved_settings") or {}
    after = s.get("after") or {}
    rc = s.get("repeat_count")
    return BehaviorInfo(
        id=str(raw["id"]),
        name=str(raw.get("name") or raw["id"]),
        description=str(raw.get("description") or ""),
        duration_s=float(raw.get("duration_s") or 0.0),
        repeat_count=None if rc is None else int(rc),
        after=str(after.get("action") or "rest"),
        speed=float(s.get("speed") or 1.0),
        trimmed=bool(s.get("trim_start_s")) or s.get("trim_end_s") is not None,
        updated_at=str(raw.get("updated_at_utc") or ""),
    )


@dataclass
class BehaviorLibrary:
    robot: RobotAPIClient
    expected: tuple[str, ...] = ()
    on_change: Callable[[LibraryChange], None] | None = None
    clock: Callable[[], float] = time.monotonic
    behaviors: dict[str, BehaviorInfo] = field(default_factory=dict)  # by name
    poses: dict[str, PoseInfo] = field(default_factory=dict)  # by name
    last_refresh: float | None = None
    last_error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.last_refresh is not None

    def stale(self, max_age_s: float) -> bool:
        return self.last_refresh is None or self.clock() - self.last_refresh > max_age_s

    async def refresh(self) -> LibraryChange:
        """Reload from the API. On failure, the previous library is kept and the error is re-raised."""
        try:
            raw_b = await self.robot.list_behaviors()
            raw_p = await self.robot.list_poses()
        except Exception as e:
            self.last_error = str(e)
            raise
        by_name: dict[str, BehaviorInfo] = {}
        dups: list[str] = []
        for raw in raw_b:
            try:
                b = _info(raw)
            except (KeyError, TypeError, ValueError):
                log.warning("ignoring a malformed behavior entry: %.200s", raw)
                continue
            prev = by_name.get(b.name)
            if prev is not None:
                dups.append(b.name)
                if prev.updated_at >= b.updated_at:
                    continue
            by_name[b.name] = b
        poses = {
            str(p.get("name") or p["id"]): PoseInfo(
                str(p["id"]), str(p.get("name") or p["id"]), p.get("usable_on_connected_arm")
            )
            for p in raw_p
            if "id" in p
        }
        old = set(self.behaviors)
        self.behaviors, self.poses = by_name, poses
        self.last_refresh = self.clock()
        self.last_error = None
        change = LibraryChange(
            added=tuple(sorted(set(by_name) - old)) if old else (),
            removed=tuple(sorted(old - set(by_name))),
            missing_expected=tuple(n for n in self.expected if n not in by_name),
            duplicates=tuple(sorted(set(dups))),
        )
        if change.missing_expected:
            log.warning("behaviors missing from the robot library: %s", ", ".join(change.missing_expected))
        if self.on_change is not None:
            self.on_change(change)
        return change

    # ------------------------------------------------------------------ queries
    def resolve(self, name_or_id: str) -> BehaviorInfo:
        key = name_or_id.strip()
        if key in self.behaviors:
            return self.behaviors[key]
        low = key.lower()
        for b in self.behaviors.values():
            if b.name.lower() == low or b.id == key:
                return b
        raise UnknownBehavior(
            f"no behavior named {name_or_id!r} in the robot library "
            f"(available: {', '.join(sorted(self.behaviors)) or 'none'})"
        )

    def resolve_pose(self, name_or_id: str) -> PoseInfo:
        key = name_or_id.strip()
        for p in self.poses.values():
            if p.name == key or p.name.lower() == key.lower() or p.id == key:
                return p
        raise UnknownBehavior(
            f"no named pose {name_or_id!r} (available: {', '.join(sorted(self.poses)) or 'none'})"
        )

    def idles(self) -> list[BehaviorInfo]:
        return sorted((b for b in self.behaviors.values() if b.loops), key=lambda b: b.name)

    def reactions(self) -> list[BehaviorInfo]:
        return sorted((b for b in self.behaviors.values() if not b.loops), key=lambda b: b.name)

    def has(self, name: str) -> bool:
        return name in self.behaviors

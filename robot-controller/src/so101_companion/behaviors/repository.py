"""Local behavior library: ``behaviors/<uuid>.json`` (owner decision D6; ADR-013).

* Stable UUID identity, independent of the file's display name; renaming never changes the id.
* Atomic writes (temp file in the same directory + ``os.replace``), never a partial file.
* Every update bumps ``revision``, stamps ``updated_at_utc`` and appends an ``edits`` entry; a write
  based on a stale revision is refused (no lost updates).
* Plain JSON only — no executable content. No serial, Qt or MuJoCo imports.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from so101_companion.behaviors.model import Behavior, EditEntry, Origin, PlaybackSettings, Sample
from so101_companion.behaviors.validate import (
    BehaviorFormatError,
    behavior_from_json,
    check_behavior,
    quality_report,
    settings_problems,
)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class LoadProblem:
    path: str
    error: str


@dataclass(frozen=True)
class BehaviorSummary:
    id: str
    name: str
    description: str
    origin: Origin
    revision: int
    duration_s: float
    updated_at_utc: str
    calibration_fingerprint: str | None


class BehaviorRepository:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def path_for(self, behavior_id: str) -> Path:
        uuid.UUID(behavior_id)  # never build paths from arbitrary text
        return self.directory / f"{behavior_id}.json"

    # ------------------------------------------------------------------------------- reading
    def list(self) -> tuple[list[BehaviorSummary], list[LoadProblem]]:
        found: list[BehaviorSummary] = []
        problems: list[LoadProblem] = []
        if not self.directory.is_dir():
            return found, problems
        for p in sorted(self.directory.glob("*.json")):
            try:
                b = self._read(p)
            except BehaviorFormatError as exc:
                problems.append(LoadProblem(str(p), str(exc)))
                continue
            found.append(
                BehaviorSummary(
                    b.id,
                    b.name,
                    b.description,
                    b.origin,
                    b.revision,
                    b.duration_s,
                    b.updated_at_utc,
                    b.calibration_fingerprint,
                )
            )
        found.sort(key=lambda s: (s.name.lower(), s.id))
        return found, problems

    def load(self, behavior_id: str) -> Behavior:
        return self._read(self.path_for(behavior_id))

    def _read(self, p: Path) -> Behavior:
        try:
            raw = json.loads(p.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BehaviorFormatError([f"unreadable or corrupt JSON: {exc}"], str(p)) from exc
        b = behavior_from_json(raw, str(p))
        if p.stem != b.id:
            raise BehaviorFormatError([f"file name does not match the behavior id {b.id}"], str(p))
        return b

    # ------------------------------------------------------------------------------- writing
    def _write(self, b: Behavior) -> Path:
        check_behavior(b)
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(b.id)
        tmp = target.with_name(f".{b.id}.json.tmp")
        with open(tmp, "w") as fh:
            fh.write(json.dumps(b.to_json(), indent=1) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return target

    def create(self, b: Behavior) -> Behavior:
        if self.path_for(b.id).exists():
            raise BehaviorFormatError([f"a behavior with id {b.id} already exists"])
        self._write(b)
        return b

    def update(
        self, b: Behavior, kind: str, details: Mapping[str, Any] | None = None, **changes: Any
    ) -> Behavior:
        """Apply ``changes`` to ``b`` as a new revision. Refused if the file moved on meanwhile."""
        current = self.load(b.id)
        if current.revision != b.revision:
            raise BehaviorFormatError(
                [
                    f"behavior changed on disk (revision {current.revision}, "
                    f"edit based on {b.revision}); reload"
                ]
            )
        now = utc_now()
        revision = b.revision + 1
        entry = EditEntry(revision, now, kind, dict(details or {}))
        new = b.with_changes(revision=revision, updated_at_utc=now, edits=(*b.edits, entry), **changes)
        self._write(new)
        return new

    def rename(self, b: Behavior, name: str, description: str) -> Behavior:
        if not name.strip():
            raise BehaviorFormatError(["name must not be empty"])
        return self.update(
            b,
            "renamed",
            {"name": [b.name, name.strip()], "description_changed": b.description != description},
            name=name.strip(),
            description=description,
        )

    def set_settings(self, b: Behavior, settings: PlaybackSettings) -> Behavior:
        problems = settings_problems(settings, b.duration_s)
        if problems:
            raise BehaviorFormatError(problems)
        return self.update(
            b, "settings", {"before": b.settings.to_json(), "after": settings.to_json()}, settings=settings
        )

    def set_keyframes(self, b: Behavior, keyframes: tuple[Sample, ...]) -> Behavior:
        """Only authored keyframe behaviors are editable this way; recorded captures stay immutable."""
        if b.origin is not Origin.SIM_KEYFRAMES:
            raise BehaviorFormatError(["only sim_keyframes behaviors have editable keyframes"])
        report = quality_report(keyframes, b.origin)
        if not report.valid:
            raise BehaviorFormatError(list(report.problems))
        settings = b.settings
        if settings_problems(settings, keyframes[-1].t):  # trim no longer fits: reset it explicitly
            settings = PlaybackSettings(0.0, None, settings.speed, settings.repeat_count)
        return self.update(
            b,
            "keyframes",
            {"before": [s.to_json() for s in b.samples], "after_count": len(keyframes)},
            samples=keyframes,
            settings=settings,
            quality=report.to_json(),
        )

    def delete(self, behavior_id: str) -> None:
        self.path_for(behavior_id).unlink()


def new_behavior(
    name: str,
    origin: Origin,
    samples: tuple[Sample, ...],
    *,
    description: str = "",
    calibration_fingerprint: str | None = None,
    calibration_robot_id: str | None = None,
    app_version: str | None = None,
) -> Behavior:
    """Build revision 1 of a new behavior; refuses invalid captures (gaps, bad samples...)."""
    report = quality_report(samples, origin)
    if not report.valid:
        raise BehaviorFormatError(list(report.problems), "recording not playable")
    now = utc_now()
    b = Behavior(
        id=str(uuid.uuid4()),
        name=name.strip(),
        description=description,
        created_at_utc=now,
        updated_at_utc=now,
        revision=1,
        origin=origin,
        samples=samples,
        calibration_fingerprint=calibration_fingerprint,
        calibration_robot_id=calibration_robot_id,
        app_version=app_version,
        quality=report.to_json(),
        edits=(EditEntry(1, now, "created", {"origin": origin.value}),),
    )
    check_behavior(b)
    return b

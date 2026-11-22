"""Semantic validation of behavior data (what JSON Schema alone cannot prove), ADR-013.

* strictly increasing timestamps starting at 0;
* adjacent gap <= 0.250 s for timed recordings (0.250 itself is valid), with the offending intervals;
* exactly six finite joints in every canonical sample, gripper within 0..100;
* provenance: hand-guided samples have ``actual`` and NO ``requested``/``sent`` (nothing is invented);
  hand-guided behaviors carry a calibration fingerprint;
* no adjacent jump > 180° on an arm joint (an encoder seam / wrap or a corrupt read — never "repaired").
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from so101_companion.behaviors.model import (
    AFTER_ACTIONS,
    JOINT_FRAME_ID,
    JOINT_UNITS,
    REPEAT_MAX,
    ROBOT_MODEL,
    SCHEMA_VERSION,
    Behavior,
    EditEntry,
    Origin,
    PlaybackSettings,
    Pose,
    Sample,
    finite,
)
from so101_companion.domain.joints import ARM_JOINTS, GRIPPER, JOINT_NAMES
from so101_companion.safety import policy

SEAM_JUMP_DEG = 180.0
MIN_CONTENT_S = 0.05  # a trimmed behavior must keep at least this much recorded time


class BehaviorFormatError(ValueError):
    """A behavior file or edit is invalid. ``problems`` lists every finding (actionable)."""

    def __init__(self, problems: Sequence[str], where: str = "") -> None:
        self.problems = list(problems)
        prefix = f"{where}: " if where else ""
        super().__init__(prefix + "; ".join(self.problems))


@dataclass(frozen=True)
class Gap:
    index: int  # gap between sample index-1 and index
    t_start: float
    t_end: float

    @property
    def seconds(self) -> float:
        return self.t_end - self.t_start

    def describe(self) -> str:
        return f"{self.seconds * 1000:.0f} ms gap between t={self.t_start:.3f}s and t={self.t_end:.3f}s"


@dataclass(frozen=True)
class QualityReport:
    sample_count: int
    duration_s: float
    max_gap_s: float
    gaps: tuple[Gap, ...]  # every interval > the limit
    problems: tuple[str, ...]  # everything that makes the take unplayable (incl. gaps)

    @property
    def valid(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "duration_s": round(self.duration_s, 6),
            "max_gap_s": round(self.max_gap_s, 6),
            "gap_limit_s": policy.RECORDING_MAX_GAP_S,
            "valid": self.valid,
        }


def pose_problems(pose: Any, label: str) -> list[str]:
    if not isinstance(pose, dict):
        return [f"{label}: expected a six-joint object, got {type(pose).__name__}"]
    problems = []
    keys = set(pose)
    if keys != set(JOINT_NAMES):
        problems.append(
            f"{label}: joint set mismatch (missing {sorted(set(JOINT_NAMES) - keys)}, "
            f"unexpected {sorted(str(k) for k in keys - set(JOINT_NAMES))})"
        )
        return problems
    for n in JOINT_NAMES:
        v = pose[n]
        if not finite(v):
            problems.append(f"{label}.{n}: not a finite number ({v!r})")
        elif n == GRIPPER and not 0.0 <= float(v) <= 100.0:
            problems.append(f"{label}.{n}: {v} outside the gripper range 0..100")
    return problems


def quality_report(samples: Sequence[Sample], origin: Origin) -> QualityReport:
    """Timing/continuity/value checks on a capture or keyframe list."""
    problems: list[str] = []
    gaps: list[Gap] = []
    channel = origin.canonical_channel
    if len(samples) < 2:
        problems.append(f"needs at least 2 samples, has {len(samples)}")
    if samples and samples[0].t != 0.0:
        problems.append(f"first sample must be at t=0 (is {samples[0].t})")
    max_gap = 0.0
    for i, s in enumerate(samples):
        if not finite(s.t) or s.t < 0:
            problems.append(f"sample {i}: invalid timestamp {s.t!r}")
            continue
        pose = s.channel(channel)
        if pose is None:
            problems.append(f"sample {i} (t={s.t:.3f}s): missing {channel!r} position (invalid sample)")
        else:
            problems += pose_problems(pose, f"sample {i} {channel}")
        if origin is Origin.REAL_HAND_GUIDED and (s.requested is not None or s.sent is not None):
            problems.append(f"sample {i}: hand-guided capture must not contain requested/sent targets")
        if i == 0:
            continue
        prev = samples[i - 1]
        if not finite(prev.t):
            continue
        dt = s.t - prev.t
        if dt <= 0:
            problems.append(f"sample {i}: timestamps not strictly increasing ({prev.t} -> {s.t})")
            continue
        max_gap = max(max_gap, dt)
        if origin.is_recording and dt > policy.RECORDING_MAX_GAP_S:
            gap = Gap(i, prev.t, s.t)
            gaps.append(gap)
            problems.append(gap.describe() + f" exceeds {policy.RECORDING_MAX_GAP_S * 1000:.0f} ms")
        a, b = prev.channel(channel), s.channel(channel)
        if origin.is_recording and isinstance(a, dict) and isinstance(b, dict):
            for n in ARM_JOINTS:
                if finite(a.get(n)) and finite(b.get(n)) and abs(b[n] - a[n]) > SEAM_JUMP_DEG:
                    problems.append(
                        f"sample {i}: {n} jumps {b[n] - a[n]:+.1f}° in {dt * 1000:.0f} ms "
                        "(encoder seam/wrap or corrupt read; not repaired)"
                    )
    duration = samples[-1].t if samples and finite(samples[-1].t) else 0.0
    return QualityReport(len(samples), duration, max_gap, tuple(gaps), tuple(problems))


def settings_problems(settings: PlaybackSettings, duration_s: float) -> list[str]:
    problems = []
    if not finite(settings.speed) or not (
        policy.PLAYBACK_SPEED_MIN <= settings.speed <= policy.PLAYBACK_SPEED_MAX
    ):
        problems.append(
            f"speed {settings.speed!r} outside {policy.PLAYBACK_SPEED_MIN}–{policy.PLAYBACK_SPEED_MAX}×"
        )
    rc = settings.repeat_count
    if rc is not None and (isinstance(rc, bool) or not isinstance(rc, int) or not 1 <= rc <= REPEAT_MAX):
        problems.append(f"repeat count {rc!r} must be 1..{REPEAT_MAX} or 'until stopped'")
    if settings.after_action not in AFTER_ACTIONS:
        problems.append(
            f"after-behavior action {settings.after_action!r} must be one of {', '.join(AFTER_ACTIONS)}"
        )
    elif settings.after_action == "pose" and not (
        isinstance(settings.after_pose_id, str) and settings.after_pose_id.strip()
    ):
        problems.append("after-behavior 'pose' needs a saved pose id")
    start = settings.trim_start_s
    end = duration_s if settings.trim_end_s is None else settings.trim_end_s
    if not finite(start) or not finite(end):
        problems.append("trim bounds must be finite numbers")
    elif not (0.0 <= start < end <= duration_s + 1e-9) or end - start < MIN_CONTENT_S:
        problems.append(
            f"trim {start:.3f}s–{end:.3f}s invalid for a {duration_s:.3f}s source "
            f"(need 0 <= start < end <= duration, at least {MIN_CONTENT_S}s kept)"
        )
    return problems


# ------------------------------------------------------------------------------------ JSON <-> model
def _parse_pose(value: Any) -> Pose | None:
    if value is None:
        return None
    return {n: float(value[n]) for n in JOINT_NAMES}


def _iso(value: Any, label: str, problems: list[str]) -> str:
    if not isinstance(value, str):
        problems.append(f"{label}: expected an ISO-8601 date-time string")
        return ""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        problems.append(f"{label}: not an ISO-8601 date-time ({value!r})")
    return value


def migrate(data: Any) -> dict[str, Any]:
    """Bring a stored document to the current schema. Only v1 exists; unknown versions are refused
    (never guessed)."""
    if not isinstance(data, dict):
        raise BehaviorFormatError(["root must be a JSON object"])
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise BehaviorFormatError(
            [f"unsupported schema_version {version!r} (this application reads version {SCHEMA_VERSION})"]
        )
    return data


def behavior_from_json(raw: Any, where: str = "") -> Behavior:
    data = migrate(raw)
    problems: list[str] = []
    bid = data.get("id")
    try:
        uuid.UUID(str(bid))
    except ValueError:
        problems.append(f"id {bid!r} is not a UUID")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("name must be a non-empty string")
    description = data.get("description", "")
    if not isinstance(description, str):
        problems.append("description must be a string")
    created = _iso(data.get("created_at_utc"), "created_at_utc", problems)
    updated = _iso(data.get("updated_at_utc"), "updated_at_utc", problems)
    revision = data.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        problems.append(f"revision {revision!r} must be an integer >= 1")
    try:
        origin = Origin(str(data.get("origin")))
    except ValueError:
        raise BehaviorFormatError([f"unknown origin {data.get('origin')!r}", *problems], where) from None
    if data.get("robot_model") != ROBOT_MODEL:
        problems.append(f"robot_model must be {ROBOT_MODEL!r}")
    if data.get("joint_units") != dict(JOINT_UNITS):
        problems.append(f"joint_units must be exactly {dict(JOINT_UNITS)}")
    frame = data.get("joint_frame", JOINT_FRAME_ID)
    if frame != JOINT_FRAME_ID:
        problems.append(f"joint_frame {frame!r} is not this application's frame {JOINT_FRAME_ID!r}")
    fingerprint = data.get("calibration_fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
        problems.append("calibration_fingerprint must be a non-empty string or null")
    if origin is Origin.REAL_HAND_GUIDED and not fingerprint:
        problems.append("a real hand-guided capture must record its calibration_fingerprint")

    samples: list[Sample] = []
    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list):
        problems.append("samples must be a list")
        raw_samples = []
    for i, rs in enumerate(raw_samples):
        if not isinstance(rs, dict) or "t_seconds" not in rs:
            problems.append(f"sample {i}: expected an object with t_seconds")
            continue
        extra = set(rs) - {"t_seconds", "actual", "requested", "sent"}
        if extra:
            problems.append(f"sample {i}: unexpected fields {sorted(extra)}")
        channel_problems = []
        for ch in ("actual", "requested", "sent"):
            if rs.get(ch) is not None:
                channel_problems += pose_problems(rs[ch], f"sample {i} {ch}")
        if channel_problems:
            problems += channel_problems
            continue
        t = rs["t_seconds"]
        samples.append(
            Sample(
                float(t) if finite(t) else float("nan"),
                _parse_pose(rs.get("actual")),
                _parse_pose(rs.get("requested")),
                _parse_pose(rs.get("sent")),
            )
        )
    if not problems:
        problems += list(quality_report(samples, origin).problems)

    settings = PlaybackSettings()
    raw_settings = data.get("settings", {})
    if not isinstance(raw_settings, dict):
        problems.append("settings must be an object")
    else:
        after = raw_settings.get("after") or {}  # absent in files written before ADR-017 -> rest
        if not isinstance(after, dict):
            problems.append("settings.after must be an object")
            after = {}
        try:
            pose_id = after.get("pose_id")
            settings = PlaybackSettings(
                trim_start_s=float(raw_settings.get("trim_start_s", 0.0)),
                trim_end_s=None
                if raw_settings.get("trim_end_s") is None
                else float(raw_settings["trim_end_s"]),
                speed=float(raw_settings.get("speed", 1.0)),
                repeat_count=raw_settings.get("repeat_count", 1),
                after_action=str(after.get("action", "rest")),
                after_pose_id=None if pose_id is None else str(pose_id),
            )
        except (TypeError, ValueError) as exc:
            problems.append(f"settings: {exc}")
    if samples and not problems:
        problems += settings_problems(settings, samples[-1].t)

    edits: list[EditEntry] = []
    for i, e in enumerate(data.get("edits", []) or []):
        if not isinstance(e, dict) or not isinstance(e.get("kind"), str):
            problems.append(f"edits[{i}]: expected an object with a kind")
            continue
        edits.append(
            EditEntry(
                int(e.get("revision", 0)), str(e.get("at_utc", "")), e["kind"], dict(e.get("details", {}))
            )
        )
    if problems:
        raise BehaviorFormatError(problems, where)
    assert isinstance(name, str) and isinstance(revision, int)
    return Behavior(
        id=str(bid),
        name=name.strip(),
        description=str(description),
        created_at_utc=created,
        updated_at_utc=updated,
        revision=revision,
        origin=origin,
        samples=tuple(samples),
        settings=settings,
        calibration_fingerprint=fingerprint,
        calibration_robot_id=data.get("calibration_robot_id"),
        joint_frame=frame,
        app_version=data.get("app_version"),
        quality=dict(data.get("quality") or {}),
        edits=tuple(edits),
    )


def check_behavior(b: Behavior) -> None:
    """Validate an in-memory behavior exactly like a loaded file (round trip through JSON form)."""
    behavior_from_json(b.to_json(), b.name)

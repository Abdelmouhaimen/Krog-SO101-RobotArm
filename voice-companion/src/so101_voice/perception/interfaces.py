"""Perception interfaces (future ManySight / VLM integration). NO vision is implemented here.

Data flow planned for later milestones::

    camera ──► PerceptionProvider ──► Observation ──► TrackingAdapter ──► Follow Me angle (deg, clockwise)
                    (ManySight / VLM)       (bbox, confidence…)   (calibrated geometry)   PUT /follow/target

A VLM may *describe* the scene (labels, boxes). It must never output joint angles: the angle comes from
a deterministic, calibrated ``TrackingAdapter`` and the robot service owns all motion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class BoundingBox:
    """Normalized image coordinates, origin top-left, all in [0, 1]."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"BoundingBox.{name} must be within [0, 1], got {v}")

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)


@dataclass(frozen=True)
class Detection:
    label: str  # "person", "monitor", ...
    confidence: float  # 0..1
    bbox: BoundingBox | None = None
    track_id: str | None = None
    position_m: tuple[float, float, float] | None = None  # camera frame, if a depth/geometry source exists

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class CameraSnapshot:
    camera_id: str
    timestamp_utc: str  # ISO 8601
    t_mono: float
    width: int
    height: int
    jpeg: bytes | None = None


@dataclass(frozen=True)
class Observation:
    snapshot: CameraSnapshot
    detections: tuple[Detection, ...] = ()
    source: Literal["manysight", "vlm", "mock"] = "mock"
    model: str | None = None  # e.g. the VLM model id that produced it
    notes: str = ""


class PerceptionProvider(Protocol):
    """Produces observations on demand. Implementations: ManySight adapter, VLM adapter (later)."""

    @property
    def available(self) -> bool: ...
    async def observe(self, camera_id: str | None = None) -> Observation | None: ...
    async def aclose(self) -> None: ...


class TrackingAdapter(Protocol):
    """Converts a calibrated camera observation to a horizontal Follow Me target angle (degrees,
    clockwise seen from above, 0 = the robot's configured front). Returns None when no target."""

    def target_angle_deg(self, obs: Observation) -> float | None: ...


@dataclass(frozen=True)
class VLMConfig:
    """Future visual-perception model choice. Not used until perception is enabled; needs its own key."""

    enabled: bool = False
    provider: Literal["openai"] = "openai"
    model: str = "gpt-6-luna"
    alternative_model: str = "gpt-6.1-terra"
    api_key_env: str = "OPENAI_API_KEY"


class NullPerceptionProvider:
    """Default: perception disabled. Never needs any API key."""

    available = False

    async def observe(self, camera_id: str | None = None) -> Observation | None:
        return None

    async def aclose(self) -> None:
        return None


@dataclass
class MockPerceptionProvider:
    """Returns scripted observations — for tests and for designing the tracking adapter."""

    observations: list[Observation] = field(default_factory=list)
    available: bool = True

    async def observe(self, camera_id: str | None = None) -> Observation | None:
        if not self.observations:
            return None
        obs = self.observations.pop(0)
        if camera_id is not None and obs.snapshot.camera_id != camera_id:
            return None
        return obs

    async def aclose(self) -> None:
        return None

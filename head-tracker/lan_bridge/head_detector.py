"""Visible face center, zoomed reacquisition, and explicitly estimated head position."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

from .nose_detector import DEFAULT_MODEL, Detector as FaceDetector

POSE_MODEL = DEFAULT_MODEL.with_name("pose_landmarker_lite.task")


@dataclass(frozen=True)
class HeadObservation:
    x: float
    y: float
    confidence: float
    estimated: bool = False


def visible_head_center(points, width, height):
    # Face outline: forehead, chin and the two sides, rather than nose direction.
    if len(points) < 455:
        return None
    selected = [points[i] for i in (10, 152, 234, 454)]
    if not all(math.isfinite(v) and 0 <= v <= 1 for p in selected for v in (p.x, p.y)):
        return None
    return ((min(p.x for p in selected) + max(p.x for p in selected)) * width / 2,
            (min(p.y for p in selected) + max(p.y for p in selected)) * height / 2)


def body_head_estimate(points, width, height):
    """Extrapolate above the shoulder midpoint along the visible torso axis."""
    if len(points) < 25:
        return None
    selected = [points[i] for i in (11, 12)]
    if not all(math.isfinite(v) and 0 <= v <= 1 for p in selected for v in (p.x, p.y)):
        return None
    confidence = min(min(p.visibility or 0, p.presence or 0) for p in selected)
    if not math.isfinite(confidence) or confidence < 0.6:
        return None
    left, right = selected
    sx, sy = (left.x + right.x) * width / 2, (left.y + right.y) * height / 2
    shoulder_dx, shoulder_dy = (right.x - left.x) * width, (right.y - left.y) * height
    if math.hypot(shoulder_dx, shoulder_dy) < 20:
        return None
    # If hips are cropped out, use the upward shoulder normal (upright estimate).
    dx, dy = shoulder_dy, -shoulder_dx
    if dy > 0:
        dx, dy = -dx, -dy
    x, y = sx + 0.5 * dx, sy + 0.5 * dy
    hips = [points[i] for i in (23, 24)]
    if all(math.isfinite(v) and 0 <= v <= 1 for p in hips for v in (p.x, p.y)) and all(
        min(p.visibility or 0, p.presence or 0) >= 0.6 for p in hips
    ):
        hx, hy = sum(p.x for p in hips) * width / 2, sum(p.y for p in hips) * height / 2
        dx, dy = sx - hx, sy - hy
        if math.hypot(dx, dy) >= 15:
            x, y = sx + 0.45 * dx, sy + 0.45 * dy
            confidence = min(confidence, *(min(p.visibility, p.presence) for p in hips))
    # Only horizontal bearing is transmitted. A supported torso can place the
    # head above the view; retain that estimate without clamping its coordinates.
    if not (0 <= x < width and -height <= y < height):
        return None
    return HeadObservation(x, y, confidence, estimated=True)


def zoom_box(width, height, anchor=None):
    """A 2x search crop; never use the crop width to compute camera bearing."""
    cw, ch = max(1, width // 2), max(1, height // 2)
    cx, cy = anchor if anchor is not None else (width / 2, height / 2)
    left = max(0, min(width - cw, round(cx - cw / 2)))
    top = max(0, min(height - ch, round(cy - ch / 2)))
    return left, top, left + cw, top + ch


class CenterFaceDetector(FaceDetector):
    point_pixels = staticmethod(visible_head_center)


class Detector:
    mode = "Head center"

    def __init__(self, model, cv2):
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        if not POSE_MODEL.is_file():
            raise ValueError("Missing pose model. Run: python -m lan_bridge.download_mediapipe")
        self.face = CenterFaceDetector(model, cv2)
        self.zoom = None
        try:
            self.pose = vision.PoseLandmarker.create_from_options(
                vision.PoseLandmarkerOptions(
                    base_options=python.BaseOptions(model_asset_path=str(POSE_MODEL)),
                    running_mode=vision.RunningMode.VIDEO,
                    num_poses=1,
                    min_pose_detection_confidence=0.5,
                    min_pose_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
            )
        except Exception:
            self.face.close()
            raise
        self.model, self.cv2 = model, cv2
        self.anchor = None
        self.last_seen = 0.0
        self.timestamp_ms = -1

    def find(self, frame):
        height, width = frame.shape[:2]
        point = self.face.find(frame)
        self.mode = "Head center (face)"
        if point is None:
            if time.monotonic() - self.last_seen > 1:
                self.anchor = None
            left, top, right, bottom = zoom_box(width, height, self.anchor)
            if self.zoom is None:
                self.zoom = CenterFaceDetector(self.model, self.cv2)
            crop = frame[top:bottom, left:right].copy()
            point = self.zoom.find(crop)
            if point is not None:
                point = point[0] + left, point[1] + top
                self.mode = "Head center (2x search)"
        if point is not None:
            observation = HeadObservation(*point, self.face.minimum_presence_confidence)
        else:
            mp = self.face.mp
            rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            self.timestamp_ms = max(self.timestamp_ms + 1, time.monotonic_ns() // 1_000_000)
            result = self.pose.detect_for_video(image, self.timestamp_ms)
            observation = body_head_estimate(result.pose_landmarks[0], width, height) if result.pose_landmarks else None
            self.mode = "Head ESTIMATE (body)" if observation else "Head LOST"
        if observation is not None:
            self.anchor = observation.x, observation.y
            self.last_seen = time.monotonic()
        return observation

    def close(self):
        self.face.close()
        if self.zoom is not None:
            self.zoom.close()
        self.pose.close()

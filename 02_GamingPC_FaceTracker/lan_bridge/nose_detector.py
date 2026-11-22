"""Local MediaPipe face landmarks; landmark 1 is the nose tip."""
from __future__ import annotations

import math
from pathlib import Path
import time

DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "models" / "face_landmarker.task"


def nose_pixels(landmarks, width: int, height: int):
    """Return the visible nose tip, or LOST for absent/invalid landmarks."""
    if len(landmarks) <= 1:
        return None
    nose = landmarks[1]
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in (nose.x, nose.y)):
        return None
    return nose.x * width, nose.y * height


class Detector:
    mode = "MediaPipe nose tip"
    minimum_presence_confidence = 0.5
    point_pixels = staticmethod(nose_pixels)

    def __init__(self, model: str | None, cv2):
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        model_path = Path(model) if model else DEFAULT_MODEL
        if not model_path.is_file():
            raise ValueError("Missing MediaPipe model. Run: python -m lan_bridge.download_mediapipe")
        self.mp = mp
        self.cv2 = cv2
        self.timestamp_ms = -1
        self.landmarker = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=python.BaseOptions(model_asset_path=str(model_path)),
                running_mode=vision.RunningMode.VIDEO,
                num_faces=1,
                min_face_detection_confidence=0.5,
                min_face_presence_confidence=self.minimum_presence_confidence,
                min_tracking_confidence=0.5,
                output_face_blendshapes=False,
                output_facial_transformation_matrixes=False,
            )
        )

    def find(self, frame):
        height, width = frame.shape[:2]
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        self.timestamp_ms = max(self.timestamp_ms + 1, time.monotonic_ns() // 1_000_000)
        result = self.landmarker.detect_for_video(image, self.timestamp_ms)
        if not result.face_landmarks:
            return None
        return self.point_pixels(result.face_landmarks[0], width, height)

    def close(self):
        self.landmarker.close()

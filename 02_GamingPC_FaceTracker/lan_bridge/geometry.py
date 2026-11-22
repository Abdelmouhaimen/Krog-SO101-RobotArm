"""Camera-relative bearing estimates, with no robot control logic."""
from __future__ import annotations

import math


def camera_bearing_deg(face_center_x: float, image_width: int, horizontal_fov_deg: float) -> float:
    """Pinhole-camera estimate; + means image-right, centered means 0 degrees.

    Assumes a rectilinear, undistorted image. Horizontal FOV must describe the
    actual camera mode, not a made-up 180-degree servo coordinate.
    """
    if image_width <= 0 or not 1.0 < horizontal_fov_deg < 179.0:
        raise ValueError("Invalid image width or horizontal field of view")
    if not math.isfinite(face_center_x):
        raise ValueError("Non-finite face center")
    focal_pixels = (image_width / 2.0) / math.tan(math.radians(horizontal_fov_deg) / 2.0)
    return math.degrees(math.atan((face_center_x - image_width / 2.0) / focal_pixels))


def robot_bearing_deg(camera_deg: float, yaw_offset_deg: float = 0.0, invert_x: bool = False) -> float:
    """Approximate bearing if webcam and robot viewpoints have been aligned.

    The yaw offset cannot compensate for large camera/robot translations.
    """
    if not all(math.isfinite(v) for v in (camera_deg, yaw_offset_deg)):
        raise ValueError("Non-finite angle")
    angle = yaw_offset_deg + (-camera_deg if invert_x else camera_deg)
    return (angle + 180.0) % 360.0 - 180.0


class SmoothBearing:
    """Small EMA in angular space. Reset when the target is lost."""

    def __init__(self, alpha: float = 0.45):
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha outside (0,1]")
        self.alpha = alpha
        self.last: float | None = None

    def update(self, angle: float) -> float:
        if not math.isfinite(angle):
            raise ValueError("Non-finite angle")
        if self.last is None:
            self.last = angle
        else:
            delta = (angle - self.last + 180.0) % 360.0 - 180.0
            self.last = (self.last + self.alpha * delta + 180.0) % 360.0 - 180.0
        return self.last

    def reset(self) -> None:
        self.last = None

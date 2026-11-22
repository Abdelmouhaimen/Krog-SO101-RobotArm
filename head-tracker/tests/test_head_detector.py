from types import SimpleNamespace
from unittest.mock import Mock
import time

import numpy as np
import pytest

from lan_bridge.head_detector import Detector, body_head_estimate, visible_head_center, zoom_box


def point(x, y, confidence=0.9):
    return SimpleNamespace(x=x, y=y, visibility=confidence, presence=confidence)


def test_head_center_does_not_follow_nose():
    points = [point(0.9, 0.9) for _ in range(478)]
    for i, xy in {10: (0.5, 0.2), 152: (0.5, 0.6), 234: (0.3, 0.4), 454: (0.7, 0.4)}.items():
        points[i] = point(*xy)
    assert visible_head_center(points, 640, 480) == pytest.approx((320, 192))


def back_pose():
    points = [point(float('nan'), float('nan'), 0) for _ in range(33)]
    for i, xy in {11: (0.3, 0.4), 12: (0.7, 0.4), 23: (0.4, 0.8), 24: (0.6, 0.8)}.items():
        points[i] = point(*xy)
    return points


def test_back_facing_torso_produces_explicit_estimate_without_face():
    observation = body_head_estimate(back_pose(), 640, 480)
    assert observation.estimated
    assert observation.x == pytest.approx(320)
    assert observation.y == pytest.approx(105.6)


def test_hidden_torso_is_lost():
    points = back_pose()
    points[11].visibility = 0.1
    assert body_head_estimate(points, 640, 480) is None
    assert body_head_estimate([], 640, 480) is None


def test_cropped_hips_allow_explicit_upright_shoulder_estimate():
    points = back_pose()
    points[23].visibility = 0.1
    observation = body_head_estimate(points, 640, 480)
    assert observation.estimated
    assert observation.x == pytest.approx(320)
    assert observation.y < 192


def test_torso_can_support_head_above_camera_view():
    points = back_pose()
    points[11] = point(0.3, 0.05)
    points[12] = point(0.7, 0.05)
    observation = body_head_estimate(points, 640, 480)
    assert observation.estimated
    assert observation.y < 0
    assert observation.x == pytest.approx(320)


def test_offscreen_horizontal_target_is_still_lost():
    points = back_pose()
    points[11] = point(0.02, 0.05)
    points[12] = point(0.12, 0.05)
    points[23] = point(0.7, 0.8)
    points[24] = point(0.8, 0.8)
    assert body_head_estimate(points, 640, 480) is None


def test_crop_offsets_map_to_original_camera_coordinates():
    detector = Detector.__new__(Detector)
    detector.face = Mock()
    detector.face.find.return_value = None
    detector.face.minimum_presence_confidence = 0.5
    detector.zoom = Mock()
    detector.zoom.find.return_value = (80, 60)
    detector.pose = Mock()
    detector.anchor = None
    detector.last_seen = time.monotonic()
    observation = detector.find(np.zeros((480, 640, 3), dtype=np.uint8))
    assert (observation.x, observation.y) == pytest.approx((240, 180))
    assert not observation.estimated
    detector.pose.detect_for_video.assert_not_called()


def test_failed_search_does_not_reuse_old_target():
    detector = Detector.__new__(Detector)
    detector.face = Mock()
    detector.face.find.return_value = None
    detector.zoom = Mock()
    detector.zoom.find.return_value = None
    detector.pose = Mock()
    detector.pose.detect_for_video.return_value = SimpleNamespace(pose_landmarks=[])
    detector.cv2 = Mock()
    detector.anchor = (300, 200)
    detector.last_seen = 0
    detector.timestamp_ms = -1
    assert detector.find(np.zeros((480, 640, 3), dtype=np.uint8)) is None
    assert detector.mode == 'Head LOST'
    assert detector.anchor is None


def test_zoom_stays_inside_frame():
    assert zoom_box(640, 480, (0, 0)) == (0, 0, 320, 240)
    assert zoom_box(640, 480, (639, 479)) == (320, 240, 640, 480)

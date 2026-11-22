from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from lan_bridge.geometry import camera_bearing_deg
from lan_bridge.nose_detector import Detector, nose_pixels


def landmarks(x, y):
    # A different landmark 0 makes a wrong index or box-center calculation fail.
    return [SimpleNamespace(x=0.1, y=0.1), SimpleNamespace(x=x, y=y)]


def test_nose_tip_drives_bearing():
    center = nose_pixels(landmarks(0.5, 0.4), 640, 480)
    assert center == pytest.approx((320, 192))
    assert camera_bearing_deg(center[0], 640, 70) == pytest.approx(0)
    right = nose_pixels(landmarks(0.75, 0.4), 640, 480)
    assert camera_bearing_deg(right[0], 640, 70) > 0


@pytest.mark.parametrize('points', [[], landmarks(float('nan'), 0.5), landmarks(-0.1, 0.5), landmarks(0.5, 1.1)])
def test_missing_or_invalid_nose_is_lost(points):
    assert nose_pixels(points, 640, 480) is None


def test_video_timestamps_increase_and_loss_does_not_reuse_nose():
    detector = Detector.__new__(Detector)
    detector.cv2 = Mock()
    detector.mp = Mock()
    detector.timestamp_ms = -1
    detector.landmarker = Mock()
    detector.landmarker.detect_for_video.side_effect = [
        SimpleNamespace(face_landmarks=[landmarks(0.75, 0.4)]),
        SimpleNamespace(face_landmarks=[]),
    ]
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch('lan_bridge.nose_detector.time.monotonic_ns', return_value=1_000_000):
        assert detector.find(frame) == pytest.approx((480, 192))
        assert detector.find(frame) is None
    calls = detector.landmarker.detect_for_video.call_args_list
    assert calls[1].args[1] > calls[0].args[1]
    detector.close()
    detector.landmarker.close.assert_called_once()

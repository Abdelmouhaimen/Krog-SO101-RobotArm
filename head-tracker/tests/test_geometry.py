import math
import pytest
from lan_bridge.geometry import camera_bearing_deg, robot_bearing_deg, SmoothBearing


def test_pinhole_center_is_zero():
    assert camera_bearing_deg(320, 640, 70) == pytest.approx(0)


def test_pinhole_edges_match_half_fov():
    assert camera_bearing_deg(0, 640, 70) == pytest.approx(-35)
    assert camera_bearing_deg(640, 640, 70) == pytest.approx(35)


def test_pinhole_quarter_offset():
    assert camera_bearing_deg(480, 640, 70) == pytest.approx(
        math.degrees(math.atan(0.5 * math.tan(math.radians(35))))
    )


def test_direction_and_yaw():
    assert robot_bearing_deg(25, 10) == pytest.approx(35)
    assert robot_bearing_deg(25, 10, True) == pytest.approx(-15)
    assert robot_bearing_deg(170, 30) == pytest.approx(-160)


def test_smoothing_and_reset():
    s = SmoothBearing(alpha=0.5)
    assert s.update(0) == pytest.approx(0)
    assert s.update(20) == pytest.approx(10)
    s.reset()
    assert s.update(-40) == pytest.approx(-40)


def test_invalid_calibration():
    with pytest.raises(ValueError): camera_bearing_deg(320, 0, 70)
    with pytest.raises(ValueError): camera_bearing_deg(320, 640, 180)
    with pytest.raises(ValueError): robot_bearing_deg(math.nan)

"""Labeled optional smoke tests needing a real MuJoCo GL backend (EGL by default).

Run: python -m pytest -m render
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

pytestmark = pytest.mark.render


@pytest.fixture
def renderer(pose_scene):  # type: ignore[no-untyped-def]
    from so101_companion.simulation.render import OffscreenRenderer

    r = OffscreenRenderer(pose_scene.model)
    yield r
    r.close()


def test_renders_official_model_non_blank(renderer, pose_scene) -> None:  # type: ignore[no-untyped-def]
    frame = renderer.render(pose_scene.data)
    assert frame.shape == (480, 640, 3)
    assert frame.dtype == np.uint8
    assert float(frame.std()) > 5.0
    info = renderer.info()
    assert info["mujoco_gl"] in ("egl", "glfw", "osmesa")
    assert "gl_renderer" in info


@pytest.mark.parametrize("joint", ["shoulder_pan", "elbow_flex", "wrist_roll", "gripper"])
def test_joint_change_changes_rendered_image(renderer, pose_scene, joint: str) -> None:  # type: ignore[no-untyped-def]
    before = renderer.render(pose_scene.data).copy()
    lo, hi = pose_scene.joints[joint].range_rad
    pose_scene.set_model_joint_rad({joint: lo + 0.85 * (hi - lo)})
    after = renderer.render(pose_scene.data)
    changed = np.mean(np.any(before != after, axis=2))
    assert changed > 0.001, f"{joint}: only {changed:.4%} pixels changed"


def test_offscreen_render_rate_at_least_15_fps(renderer, pose_scene) -> None:  # type: ignore[no-untyped-def]
    renderer.render(pose_scene.data)
    n = 30
    t0 = time.perf_counter()
    for i in range(n):
        pose_scene.set_model_joint_rad({"shoulder_pan": math.radians(i)})
        renderer.render(pose_scene.data)
    fps = n / (time.perf_counter() - t0)
    print(f"offscreen fps={fps:.1f} info={renderer.info()}")
    assert fps >= 15.0


def test_renderer_refuses_foreign_thread(renderer, pose_scene) -> None:  # type: ignore[no-untyped-def]
    import threading

    from so101_companion.simulation.render import RenderBackendError

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            renderer.render(pose_scene.data)
        except BaseException as exc:
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(errors) == 1 and isinstance(errors[0], RenderBackendError)


@pytest.mark.gui
def test_full_app_offscreen_with_real_renderer_and_logical_gripper(qapp) -> None:  # type: ignore[no-untyped-def]
    """Real EGL renderer inside the real window (offscreen QPA); gripper 0 vs 100 renders differently."""
    from so101_companion.app import build_application, build_parser
    from so101_companion.config import load_config

    built = build_application(build_parser().parse_args([]), load_config(None), start_worker=False)
    w, c = built.window, built.coordinator
    w.show()
    try:
        assert w.viewport.render_once(), w.viewport.error
        img = w.viewport.last_image()
        default_size = build_parser().parse_args([]).render_size  # Studio default (960x720)
        assert img is not None and (img.width(), img.height()) == tuple(default_size)
        assert [x for x in qapp.topLevelWidgets() if x.isVisible()] == [w]
        frames = []
        for g in (0.0, 100.0):
            c.set_requested({**dict(c.snapshot().requested.values), "gripper": g})
            for _ in range(15):
                c.tick()
            w.apply_snapshot(c.snapshot())
            assert w.viewport.render_once()
            frames.append(w.viewport.last_frame().copy())
        assert np.mean(np.any(frames[0] != frames[1], axis=2)) > 0.001
    finally:
        w.close()

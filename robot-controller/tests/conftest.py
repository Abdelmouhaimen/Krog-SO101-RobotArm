"""Test-wide safety net and fixtures.

* The device guard is installed for the whole session: any serial-device open, ``/dev``
  enumeration or hardware-driver import fails the offending test AND the session.
* Qt uses the offscreen platform (no display needed); MuJoCo uses EGL unless MUJOCO_GL is set.
  The backend must be chosen before ``mujoco`` is imported, hence at the top of this file.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Never pick up the developer's config/local.toml (real port, API, physical control) in tests.
os.environ["SO101_COMPANION_CONFIG"] = ""

from so101_companion.diagnostics import device_guard
from so101_companion.simulation import gl_backend, model_assets

gl_backend.select_backend(os.environ.get("MUJOCO_GL") or "egl")

REQUIRE_ASSETS = os.environ.get("SO101_REQUIRE_ASSETS") == "1"


def pytest_configure(config: pytest.Config) -> None:
    device_guard.install()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if device_guard.violations():
        print("\nDEVICE GUARD VIOLATIONS:", *device_guard.violations(), sep="\n  ")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _no_device_access(request: pytest.FixtureRequest) -> Iterator[None]:
    before = len(device_guard.violations())
    yield
    if "expect_device_violation" in request.fixturenames:
        return
    new = device_guard.violations()[before:]
    if new:
        pytest.fail(f"Device access attempted during test: {new}")


@pytest.fixture
def expect_device_violation() -> Iterator[None]:
    """For guard self-tests: violations raised inside the test are expected and cleared afterwards."""
    yield
    device_guard.reset_violations()


@pytest.fixture
def calibration_file(tmp_path: Path) -> Path:
    from so101_companion.devices.fake import write_demo_calibration

    return write_demo_calibration(tmp_path / "calibration" / "robots" / "so_follower", "test_follower")


@pytest.fixture
def allow_driver_imports() -> Iterator[None]:
    """LeRobot contract tests: permit importing lerobot/serial/scservo_sdk. Device OPENS stay blocked."""
    previous = device_guard.set_forbidden_modules(frozenset())
    yield
    device_guard.set_forbidden_modules(previous)


@pytest.fixture(scope="session")
def vendor_dir() -> Path:
    path = model_assets.default_vendor_dir()
    if not path.exists():
        msg = f"Official model not fetched ({path}); run: python scripts/fetch_model_assets.py"
        if REQUIRE_ASSETS:
            pytest.fail(msg)
        pytest.skip(msg)
    return path


@pytest.fixture(scope="session")
def scene_path(vendor_dir: Path) -> Path:
    model_assets.verify_assets(vendor_dir)
    return vendor_dir / model_assets.load_manifest().scene


@pytest.fixture
def pose_scene(scene_path: Path):  # type: ignore[no-untyped-def]
    from so101_companion.simulation.mujoco_pose import PoseScene

    return PoseScene.from_scene_path(scene_path)


@pytest.fixture(scope="session")
def qapp():  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["pytest"])
    yield app

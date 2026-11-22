"""CLI bootstrap. The application ALWAYS starts in SIM_ONLY with no serial access; hardware is only
reached through explicit, confirmed actions inside the window.

    python -m so101_companion                          # interactive window (+ config/local.toml if present)
    python -m so101_companion --config my.toml         # pre-fill port / calibration fields
    python -m so101_companion --fake-hardware          # offline rehearsal with a FAKE robot (no device)
    python -m so101_companion --smoke-seconds 5        # self-checking smoke run, writes artifacts
    python -m so101_companion --api                    # Studio + HTTP API (ADR-018), 127.0.0.1:8765
    python -m so101_companion --headless --api --fake-hardware --fake-arm   # API dev without a window
    python -m so101_companion --create-api-token NAME  # store a new API token hash, print it once

Without ``--config`` the machine's gitignored ``config/local.toml`` is used when it exists (current
directory first, then the repository); ``SO101_COMPANION_CONFIG`` overrides that ("" = built-in only).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from so101_companion import __version__
from so101_companion.config import AppConfig, ConfigError, load_config
from so101_companion.diagnostics import device_guard
from so101_companion.safety import policy
from so101_companion.simulation import gl_backend, model_assets

EXIT_OK = 0
EXIT_SMOKE_FAILED = 1
EXIT_SETUP_ERROR = 2
MIN_SMOKE_FPS = 15.0  # UX-05


def _parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = (int(v) for v in text.lower().split("x"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT, e.g. 640x480") from exc
    if not (64 <= w <= 4096 and 64 <= h <= 4096):
        raise argparse.ArgumentTypeError("render size out of range 64..4096")
    return w, h


LOCAL_CONFIG_ENV = "SO101_COMPANION_CONFIG"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="so101_companion", description=__doc__)
    p.add_argument(
        "--mode",
        default="sim-only",
        choices=["sim-only", "real-to-sim", "sim-to-real"],
        help="Start mode. Only sim-only is accepted: hardware modes are chosen in the window after an "
        "explicit, confirmed connection.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="TOML config (default: config/local.toml if it exists, else the built-in config/default.toml; "
        f"${LOCAL_CONFIG_ENV} overrides, empty = built-in)",
    )
    p.add_argument(
        "--model", type=Path, default=None, help="Official scene.xml (default: pinned vendor copy)"
    )
    p.add_argument("--gl-backend", choices=gl_backend.SUPPORTED_BACKENDS, default=None)
    p.add_argument("--render-size", type=_parse_size, default=(960, 720), help="Offscreen frame WxH")
    p.add_argument("--fake-hardware", action="store_true", help="Use a FAKE robot (offline UI rehearsal)")
    p.add_argument("--smoke-seconds", type=float, default=None, help="Self-checking smoke run for N seconds")
    p.add_argument(
        "--screenshot",
        type=Path,
        default=None,
        help="Periodically save a PNG of the app window here (supervised commissioning aid)",
    )
    p.add_argument("--screenshot-every", type=float, default=1.0, help="Seconds between --screenshot saves")
    p.add_argument(
        "--event-log", type=Path, default=None, help="Stream all events/ticks to this JSONL file (redacted)"
    )
    p.add_argument("--artifacts-dir", type=Path, default=None, help="Smoke output dir")
    api = p.add_argument_group("HTTP API (Milestone 2.5, ADR-018)")
    api.add_argument("--api", action="store_true", help="Start the authenticated HTTP API ([api] config)")
    api.add_argument("--api-port", type=int, default=None, help="Override [api] port")
    api.add_argument(
        "--api-allow-physical-control",
        action="store_true",
        help="Let authenticated API requests move the REAL arm when the controller is ready "
        "(default: [api] allow_physical_control, false)",
    )
    api.add_argument(
        "--headless",
        action="store_true",
        help="No window: controller + HTTP API only. Fake hardware only (real-arm API use goes through the "
        "Studio window, where the operator connects and selects Simulation → Real)",
    )
    api.add_argument(
        "--fake-arm",
        action="store_true",
        help="With --headless --fake-hardware: connect the FAKE arm, enter Simulation → Real and save its "
        "start pose as the rest pose (no real device is involved)",
    )
    api.add_argument(
        "--create-api-token",
        metavar="NAME",
        default=None,
        help="Create an API token, store its hash in the token file and print the token once",
    )
    return p


@dataclass
class Built:
    app: Any
    window: Any
    coordinator: Any
    scene: Any
    mapping: Any
    backend: str
    scene_path: Path
    built_api: Any = None


class SetupError(RuntimeError):
    pass


def real_robot_factory(cfg: AppConfig) -> Any:
    from so101_companion.devices.lerobot_so101 import LeRobotSO101Adapter

    def factory() -> LeRobotSO101Adapter:
        # Reached only from a confirmed Connect request (coordinator). From here on the device
        # guard stops blocking device access for this process.
        device_guard.set_enabled(False)
        return LeRobotSO101Adapter(allowed_versions=cfg.hardware.allowed_lerobot_versions)

    return factory


def fake_robot_setup(cfg: AppConfig) -> tuple[AppConfig, Any]:
    from so101_companion.devices.fake import FakeRobot, write_demo_calibration

    cal = write_demo_calibration(Path(tempfile.mkdtemp(prefix="so101-fake-")))
    cfg = replace(cfg, hardware=replace(cfg.hardware, port="/dev/FAKE-so101-demo", calibration_path=str(cal)))

    def factory() -> FakeRobot:
        return FakeRobot(
            initial_pose={
                "shoulder_pan": 0.0,
                "shoulder_lift": -20.0,
                "elbow_flex": 30.0,
                "wrist_flex": 10.0,
                "wrist_roll": 0.0,
                "gripper": 20.0,
            },
            follow=0.6,
        )

    return cfg, factory


@dataclass
class Core:
    """Everything except the window: the ONE coordinator and what it needs (shared by both modes)."""

    cfg: AppConfig
    coordinator: Any
    scene: Any
    mapping: Any
    backend: str
    scene_path: Path
    manifest: Any


def build_core(args: argparse.Namespace, cfg: AppConfig) -> Core:
    backend = gl_backend.select_backend(args.gl_backend)
    scene_path: Path = args.model or (
        Path(cfg.simulation.model_path) if cfg.simulation.model_path else model_assets.default_scene_path()
    )
    try:
        manifest = model_assets.verify_assets(model_assets.vendor_dir_for_scene(scene_path))
    except (model_assets.AssetVerificationError, IndexError) as exc:
        raise SetupError(str(exc)) from exc

    try:
        # With some backends (e.g. osmesa without libOSMesa) `import mujoco` itself fails.
        from so101_companion.control.coordinator import ControllerCoordinator
        from so101_companion.simulation.mapping import mapping_from_config, sim_provisional_bounds
        from so101_companion.simulation.mujoco_pose import ModelStructureError, PoseScene
        from so101_companion.telemetry.events import EventLog
    except Exception as exc:
        raise SetupError(
            f"MuJoCo could not initialise with MUJOCO_GL={backend}: {type(exc).__name__}: {exc}\n"
            "No viewer was opened. Try another backend (--gl-backend egl|glfw); see "
            "milestone-1/runbooks/UBUNTU_SETUP.md."
        ) from exc

    try:
        scene = PoseScene.from_scene_path(scene_path)
    except (ModelStructureError, ValueError, FileNotFoundError) as exc:
        raise SetupError(f"Model error: {exc}") from exc

    if args.fake_hardware:
        cfg, factory = fake_robot_setup(cfg)
    else:
        factory = real_robot_factory(cfg)
    mapping = mapping_from_config(scene.model_ranges_rad(), cfg.simulation)
    sim_bounds = sim_provisional_bounds(mapping, policy.ANGULAR_MARGIN_DEG)
    events = EventLog(cfg.app.event_ring_size)
    if getattr(args, "event_log", None):
        events.stream_to(args.event_log)
    from so101_companion.devices.rest_pose import FileRestPoseStore, MemoryRestPoseStore, RestPoseStore

    rest_dir = model_assets.project_root() / "config"
    rest_store: RestPoseStore = MemoryRestPoseStore() if args.fake_hardware else FileRestPoseStore(rest_dir)

    coordinator = ControllerCoordinator(
        cfg,
        sim_bounds,
        scene.logical_pose(mapping),
        factory,
        events=events,
        rest_store=rest_store,
    )
    # Follow Me / Talk Mode (ADR-019): the controller's own FK direction analyzer (event-time only) and the
    # per-calibration front reference (gitignored; the fake demo keeps it in memory).
    from so101_companion.control.front import FileFrontStore, MemoryFrontStore
    from so101_companion.simulation.direction import DirectionAnalyzer

    coordinator.directions = DirectionAnalyzer(scene_path, cfg.simulation)
    coordinator.front_store = MemoryFrontStore() if args.fake_hardware else FileFrontStore(rest_dir)
    coordinator._load_front()
    events.emit(
        "build_info",
        version=__version__,
        config=cfg.source,
        gl_backend=backend,
        fake_hardware=bool(args.fake_hardware),
        model=f"{manifest.repo_url}@{manifest.commit}",
    )
    return Core(cfg, coordinator, scene, mapping, backend, scene_path, manifest)


def _service_directions(scene_path: Path, cfg: AppConfig) -> Any:
    """A separate FK analyzer for the UI/API threads (the controller has its own: no lock contention)."""
    from so101_companion.simulation.direction import DirectionAnalyzer

    return DirectionAnalyzer(scene_path, cfg.simulation)


def library_dirs(fake_hardware: bool) -> tuple[Path, Path]:
    """Operator behavior library and named poses (gitignored, D6). The fake-hardware demo keeps its own
    scratch libraries so demo takes (demo calibration) never mix with the real arm's."""
    root = model_assets.project_root()
    if fake_hardware:
        return root / "artifacts/fake-hardware-behaviors", root / "artifacts/fake-hardware-poses"
    return root / "behaviors", root / "poses"


def build_application(
    args: argparse.Namespace, cfg: AppConfig, *, confirmer: Any = None, start_worker: bool = True
) -> Built:
    core = build_core(args, cfg)
    cfg, coordinator, scene, mapping = core.cfg, core.coordinator, core.scene, core.mapping
    manifest, backend, scene_path = core.manifest, core.backend, core.scene_path
    from PySide6.QtWidgets import QApplication

    from so101_companion.simulation.render import OffscreenRenderer
    from so101_companion.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    width, height = args.render_size
    repo = manifest.repo_url.removesuffix(".git")
    build_info = {
        "model": f"{repo}@{manifest.commit[:12]} ({manifest.license_spdx}), {manifest.scene} — "
        "sha256-verified, unmodified; arm/gripper mapping PROVISIONAL (ADR-006)",
    }
    window = MainWindow(
        scene,
        mapping,
        coordinator,
        lambda: OffscreenRenderer(scene.model, width=width, height=height),
        cfg,
        confirmer=confirmer,
        build_info=build_info,
        start_worker=start_worker,
        behaviors_dir=library_dirs(args.fake_hardware)[0],
        poses_dir=library_dirs(args.fake_hardware)[1],
        directions=_service_directions(scene_path, cfg),
    )
    if args.fake_hardware:
        window.setWindowTitle("SO-101 Studio — FAKE HARDWARE DEMO (no real device)")
    return Built(app, window, coordinator, scene, mapping, backend, scene_path)


def grab_window_image(window: Any) -> Any:
    """The window as a QImage. Never save the QPixmap from ``grab()`` directly: ``QPixmap.save`` keeps
    the Python GIL for the whole PNG encode (~130 ms for the Studio window), which starves the
    controller thread mid-read and makes the Feetech SDK report a false "no status packet"
    (READ_FAILED). ``QImage.save`` releases the GIL while it encodes."""
    return window.grab().toImage()


class WindowShooter:
    """Periodic ``--screenshot`` aid. The GUI thread only grabs (a few ms); the PNG is encoded and
    written on a background thread, atomically (tmp file + rename). A shot is skipped while the
    previous one is still being written."""

    def __init__(self, window: Any, path: Path) -> None:
        self.window = window
        self.path = path
        self._busy = threading.Lock()

    def shoot(self) -> bool:
        if not self._busy.acquire(blocking=False):
            return False
        try:
            image = grab_window_image(self.window)
        except Exception:
            self._busy.release()
            raise
        threading.Thread(target=self._write, args=(image,), name="so101-screenshot", daemon=True).start()
        return True

    def _write(self, image: Any) -> None:
        try:
            tmp = self.path.with_name(self.path.stem + ".tmp.png")
            if image.save(str(tmp)):
                tmp.replace(self.path)
        except OSError:
            pass  # a commissioning aid: never disturb the session
        finally:
            self._busy.release()


def default_config_path() -> Path | None:
    """The config used when ``--config`` is omitted: ``$SO101_COMPANION_CONFIG`` ("" = built-in only),
    else ``config/local.toml`` in the current directory, else in the repository. None = built-in."""
    env = os.environ.get(LOCAL_CONFIG_ENV)
    if env is not None:
        return Path(env) if env else None
    for base in (Path.cwd(), Path(__file__).resolve().parents[2]):
        candidate = base / "config" / "local.toml"
        if candidate.is_file():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config is None:
        args.config = default_config_path()
        if args.config is not None:
            print(f"Config: {args.config}", file=sys.stderr)

    # Block serial opens and /dev enumeration until the operator confirms a connection.
    device_guard.install(forbidden_modules=frozenset())

    if args.mode != "sim-only":
        print(
            f"--mode {args.mode} refused: the app always starts in SIM_ONLY. Choose REAL_TO_SIM / "
            "SIM_TO_REAL in the window after an explicit, confirmed hardware connection. "
            "Nothing was connected.",
            file=sys.stderr,
        )
        return EXIT_SETUP_ERROR
    try:
        cfg = load_config(args.config)
        cfg = api_overrides(args, cfg)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return EXIT_SETUP_ERROR
    if args.create_api_token:
        return create_api_token_cli(cfg, args.create_api_token)
    if args.headless:
        return run_headless(args, cfg)
    try:
        built = build_application(args, cfg)
    except (SetupError, gl_backend.BackendSelectionError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETUP_ERROR
    api_server = None
    if cfg.api.enabled:
        try:
            api_server = start_api(
                cfg,
                built.coordinator,
                built.window.behavior_service,
                built.scene_path,
                built.mapping,
                fake_hardware=args.fake_hardware,
            )
        except (RuntimeError, OSError) as exc:
            print(f"HTTP API not started: {exc}", file=sys.stderr)
            built.window.close()
            return EXIT_SETUP_ERROR
        built.built_api = api_server
        built.window.setWindowTitle(built.window.windowTitle() + f" — API {api_server.url}")

    built.window.show()
    if args.screenshot is not None:
        from PySide6.QtCore import QTimer

        shot_path: Path = args.screenshot
        shot_path.parent.mkdir(parents=True, exist_ok=True)

        shooter = WindowShooter(built.window, shot_path)
        shot_timer = QTimer(built.window)
        shot_timer.setInterval(max(200, round(args.screenshot_every * 1000)))
        shot_timer.timeout.connect(shooter.shoot)
        shot_timer.start()
    if args.smoke_seconds is None:
        try:
            return int(built.app.exec())
        finally:
            if api_server is not None:
                api_server.stop()

    artifacts = args.artifacts_dir or (
        model_assets.project_root() / "artifacts" / "smoke" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    runner = SmokeRunner(built, seconds=args.smoke_seconds, out_dir=artifacts)
    runner.start()
    built.app.exec()
    if api_server is not None:
        api_server.stop()
    return runner.exit_code


# ============================================================================ HTTP API (ADR-018)
def api_overrides(args: argparse.Namespace, cfg: AppConfig) -> AppConfig:
    from so101_companion.config import validate_api

    api = cfg.api
    if args.api or args.headless:
        api = replace(api, enabled=True)
    if args.api_port is not None:
        api = replace(api, port=args.api_port)
    if args.api_allow_physical_control:
        api = replace(api, allow_physical_control=True)
    validate_api(api)
    return replace(cfg, api=api)


def create_api_token_cli(cfg: AppConfig, name: str) -> int:
    from so101_companion.api.auth import TokenFileError, create_token, resolve_token_file

    path = resolve_token_file(cfg.api.token_file)
    try:
        token = create_token(path, name)
    except (TokenFileError, OSError) as exc:
        print(f"Cannot create API token: {exc}", file=sys.stderr)
        return EXIT_SETUP_ERROR
    print(f"API token '{name}' created; its hash is stored in {path} (mode 0600).")
    print("Store this token now — it is not shown again:")
    print(token)
    return EXIT_OK


def start_api(
    cfg: AppConfig,
    coordinator: Any,
    behavior_service: Any,
    scene_path: Path,
    mapping: Any,
    *,
    fake_hardware: bool,
) -> Any:
    """Start the HTTP API as the SECOND interface of ``coordinator`` (no second controller)."""
    from so101_companion.api.auth import TokenVerifier, resolve_token_file
    from so101_companion.api.runner import ApiServer
    from so101_companion.api.server import create_app
    from so101_companion.api.service import ApiCommands
    from so101_companion.api.video import MujocoFrameRenderer, SimulationVideo

    api = cfg.api
    verifier = TokenVerifier(path=resolve_token_file(api.token_file))
    commands = ApiCommands(
        coordinator,
        behavior_service,
        # FAKE hardware has no physical device: API motion is allowed against the fake arm.
        allow_physical_control=api.allow_physical_control or fake_hardware,
        ack_timeout_s=api.ack_timeout_s,
    )

    def pose() -> Any:
        snap = coordinator.published.get()
        if snap is None:
            return None
        return snap.displayed.values if snap.displayed.values is not None else snap.requested.values

    video = SimulationVideo(
        lambda: MujocoFrameRenderer(scene_path, mapping, api.frame_width, api.frame_height),
        pose,
        max_fps=api.stream_max_fps,
        jpeg_quality=api.jpeg_quality,
    )
    server = ApiServer(create_app(commands, verifier, video), api)
    server.start()
    coordinator.events.emit(
        "api_started",
        url=server.url,
        physical_control=commands.allow_physical_control,
        fake_hardware=fake_hardware,
        token_file=str(verifier.path),
    )
    server.video = video
    return server


def run_headless(args: argparse.Namespace, cfg: AppConfig) -> int:
    """Controller + HTTP API without a window, for development and tests with FAKE hardware only."""
    import signal
    import threading

    if not args.fake_hardware:
        print(
            "--headless needs --fake-hardware: real-arm API use goes through the Studio window "
            "(so101-companion --api), where the operator connects and selects Simulation → Real.",
            file=sys.stderr,
        )
        return EXIT_SETUP_ERROR
    try:
        core = build_core(args, cfg)
    except (SetupError, gl_backend.BackendSelectionError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SETUP_ERROR
    from so101_companion.behaviors.poses import PoseRepository
    from so101_companion.behaviors.repository import BehaviorRepository
    from so101_companion.behaviors.service import BehaviorService
    from so101_companion.control import requests as rq
    from so101_companion.control.worker import ControllerWorker
    from so101_companion.domain.state import Mode

    cfg, coordinator = core.cfg, core.coordinator
    behaviors_dir, poses_dir = library_dirs(True)
    service = BehaviorService(
        coordinator,
        BehaviorRepository(behaviors_dir),
        poses=PoseRepository(poses_dir),
        directions=_service_directions(core.scene_path, cfg),
    )
    worker = ControllerWorker(coordinator, lambda _snap: None, cfg.app.command_hz)
    worker.start()
    if args.fake_arm:  # explicit flag, FAKE robot only: nothing real is connected or moved
        coordinator.submit(
            rq.EnterMode(Mode.SIM_TO_REAL, cfg.hardware.port, cfg.hardware.calibration_path, confirmed=True)
        )
        coordinator.submit(rq.RecordRestPose(confirmed=True))
    try:
        server = start_api(cfg, coordinator, service, core.scene_path, core.mapping, fake_hardware=True)
    except (RuntimeError, OSError) as exc:
        print(f"HTTP API not started: {exc}", file=sys.stderr)
        worker.stop()
        return EXIT_SETUP_ERROR
    print(
        json.dumps(
            {
                "api": server.url,
                "docs": server.url + "/docs",
                "fake_hardware": True,
                "fake_arm_in_sim_to_real": bool(args.fake_arm),
                "behaviors": str(behaviors_dir),
                "poses": str(poses_dir),
            }
        ),
        flush=True,
    )
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: done.set())
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    seconds = args.smoke_seconds
    done.wait(timeout=seconds)
    server.stop()
    worker.stop()
    return EXIT_OK


class SmokeRunner:
    """Sweeps every slider (SIM_ONLY), measures render/controller rates, checks the one-window
    invariant and that no device was touched, and saves evidence."""

    def __init__(self, built: Built, *, seconds: float, out_dir: Path) -> None:
        from PySide6.QtCore import QTimer

        self.built = built
        self.window = built.window
        self.seconds = seconds
        self.out_dir = out_dir
        self.exit_code = EXIT_SMOKE_FAILED
        self._t0 = 0.0
        self._first_frame: Any = None
        self._max_visible_windows = 0
        self._timer = QTimer()
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        from PySide6.QtCore import QTimer

        self._t0 = time.monotonic()
        QTimer.singleShot(0, self._timer.start)

    def _visible_top_levels(self) -> list[str]:
        from PySide6.QtGui import QGuiApplication

        return [
            w.title() or w.objectName() or type(w).__name__
            for w in QGuiApplication.topLevelWindows()
            if w.isVisible()
        ]

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._t0
        self._max_visible_windows = max(self._max_visible_windows, len(self._visible_top_levels()))
        if self._first_frame is None and self.window.viewport.last_frame() is not None and elapsed > 0.5:
            self._first_frame = self.window.viewport.last_frame().copy()
        if elapsed > 1.0:
            for i, slider in enumerate(self.window.sliders.values()):
                mid = (slider.minimum() + slider.maximum()) / 2
                amp = 0.4 * (slider.maximum() - slider.minimum()) / 2
                slider.setValue(round(mid + amp * math.sin(2 * math.pi * 0.25 * (elapsed - 1.0) + i)))
        if elapsed >= self.seconds:
            self._timer.stop()
            self._finish()

    def _finish(self) -> None:
        import numpy as np
        from PySide6.QtGui import QGuiApplication

        vp = self.window.viewport
        snap = self.built.coordinator.snapshot()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        grab_window_image(self.window).save(str(self.out_dir / "window.png"))
        if vp.last_image() is not None:
            vp.last_image().save(str(self.out_dir / "viewport_frame.png"))
        last = vp.last_frame()
        frame_changed = (
            bool(np.any(self._first_frame != last))
            if self._first_frame is not None and last is not None
            else False
        )
        stats = vp.stats.summary()
        visible = self._visible_top_levels()
        violations = device_guard.violations()
        checks = {
            "render_ok": vp.error is None and stats["frames_total"] > 0,
            "fps_at_least_15": stats["fps"] >= MIN_SMOKE_FPS,
            "exactly_one_visible_top_level_window": len(visible) == 1 and self._max_visible_windows == 1,
            "six_sliders": len(self.window.sliders) == 6,
            "slider_motion_changes_rendered_frame": frame_changed,
            "controller_ticking": snap.tick_hz_actual > 10.0,
            "still_sim_only_disconnected": snap.mode.value == "SIM_ONLY"
            and snap.connection.value == "DISCONNECTED",
            "no_device_access": not violations and device_guard.is_active(),
        }
        metrics = {
            "version": __version__,
            "timestamp_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "duration_s": self.seconds,
            "checks": checks,
            "passed": all(checks.values()),
            "render": stats,
            "controller": {"tick_hz_actual": round(snap.tick_hz_actual, 2), "overruns": snap.overruns},
            "render_error": vp.error,
            "renderer": vp.renderer_info(),
            "visible_top_level_windows": visible,
            "max_visible_top_level_windows": self._max_visible_windows,
            "device_guard_violations": violations,
            "environment": {
                "qt_platform": QGuiApplication.platformName(),
                "xdg_session_type": os.environ.get("XDG_SESSION_TYPE", ""),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "mujoco_gl": self.built.backend,
                "numpy": np.__version__,
            },
            "scene": str(self.built.scene_path),
        }
        (self.out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
        print(
            json.dumps(
                {"artifacts": str(self.out_dir), "passed": metrics["passed"], "checks": checks}, indent=2
            )
        )
        self.exit_code = EXIT_OK if metrics["passed"] else EXIT_SMOKE_FAILED
        self.window.close()
        self.built.app.quit()

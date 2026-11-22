#!/usr/bin/env python3
"""Simulation evidence for Follow Me + Talk Mode (Milestone 2.6, ADR-019). No hardware.

Drives the REAL controller (FakeRobot, fake clock) through the five requested scenarios and renders the
official MuJoCo model top-down (the gripper bearing is visible directly), annotated with the FK-measured
bearing about the shoulder-pan axis. Writes PNG sheets and ``summary.json`` to
``docs/evidence/follow-me/``.

    MUJOCO_GL=egl .venv/bin/python scripts/follow_me_evidence.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ.setdefault("MUJOCO_GL", "egl")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from so101_companion.behaviors.direction import wrap180  # noqa: E402
from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample  # noqa: E402
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior  # noqa: E402
from so101_companion.behaviors.service import BehaviorService  # noqa: E402
from so101_companion.config import AppConfig  # noqa: E402
from so101_companion.control import requests as rq  # noqa: E402
from so101_companion.control.clock import FakeClock  # noqa: E402
from so101_companion.control.coordinator import ControllerCoordinator  # noqa: E402
from so101_companion.devices.fake import FakeRobot, write_demo_calibration  # noqa: E402
from so101_companion.domain.joints import ARM_JOINTS  # noqa: E402
from so101_companion.domain.state import Connection, Mode  # noqa: E402
from so101_companion.safety.bounds import build_bounds  # noqa: E402
from so101_companion.safety.policy import TransitionProfile  # noqa: E402
from so101_companion.simulation import model_assets  # noqa: E402
from so101_companion.simulation.direction import DirectionAnalyzer  # noqa: E402
from so101_companion.simulation.mapping import mapping_from_config  # noqa: E402
from so101_companion.simulation.mujoco_pose import PoseScene  # noqa: E402
from so101_companion.simulation.render import OffscreenRenderer  # noqa: E402

OUT = ROOT / "docs" / "evidence" / "follow-me"
NORMAL = {
    "shoulder_pan": 5.0,
    "shoulder_lift": -20.0,
    "elbow_flex": 30.0,
    "wrist_flex": 10.0,
    "wrist_roll": 0.0,
    "gripper": 0.0,
}
REVERSED = {
    "shoulder_pan": -75.0,
    "shoulder_lift": -60.0,
    "elbow_flex": -51.0,
    "wrist_flex": -55.0,
    "wrist_roll": 0.0,
    "gripper": 3.0,
}
SIZE = 300


class Stage:
    def __init__(self, tmp: Path) -> None:
        cfg = AppConfig()
        self.clock = FakeClock()
        cal = write_demo_calibration(tmp / "cal", "evidence")
        sim_bounds = build_bounds({n: (-100.0, 100.0) for n in ARM_JOINTS}, 5.0, "evidence", "SIM")
        self.c = ControllerCoordinator(
            cfg,
            sim_bounds,
            dict(NORMAL),
            lambda: FakeRobot(initial_pose=dict(NORMAL), clock=self.clock, sleep=self.clock.sleep),
            clock=self.clock,
        )
        scene_path = model_assets.default_scene_path()
        self.c.directions = DirectionAnalyzer(scene_path, cfg.simulation)
        self.fk = DirectionAnalyzer(scene_path, cfg.simulation)
        self.service = BehaviorService(self.c, BehaviorRepository(tmp / "behaviors"), directions=self.fk)
        self.run(rq.EnterMode(Mode.SIM_TO_REAL, "/dev/fake-evidence", str(cal), confirmed=True))
        self.run(rq.RecordRestPose(confirmed=True))
        self.fp = self.c.snapshot().extra["calibration_fingerprint"]
        self.scene = PoseScene.from_scene_path(scene_path)
        self.mapping = mapping_from_config(self.scene.model_ranges_rad(), cfg.simulation)
        self.renderer = OffscreenRenderer(self.scene.model, width=SIZE, height=SIZE)
        cam = self.renderer.camera
        cam.elevation, cam.azimuth, cam.distance = -89.9, 90.0, 1.0
        cam.lookat[:] = [self.fk.pan_axis_xy[0], 0.0, 0.1]

    def run(self, *reqs: Any) -> None:
        for r in reqs:
            self.c.submit(r)
        self.c.process_requests()

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.clock.advance(0.05)
            self.c.tick()

    def behavior(
        self, name: str, base: dict[str, float], joint: str, sweep: tuple[str, float, float] | None = None
    ) -> Any:
        samples = []
        for i in range(41):
            p = dict(base)
            p[joint] = base[joint] + 6.0 * (1 - abs(20 - i) / 20)
            if sweep:
                p[sweep[0]] = sweep[1] + (sweep[2] - sweep[1]) * i / 40
            samples.append(Sample(round(i * 0.05, 6), actual=p))
        b = self.service.repository.create(
            new_behavior(name, Origin.REAL_HAND_GUIDED, tuple(samples), calibration_fingerprint=self.fp)
        )
        return self.service.repository.set_settings(b, PlaybackSettings(repeat_count=None))

    def play(self, b: Any) -> None:
        self.service.play(
            b, b.settings, None, confirmed=True, enable_torque=False, entry_profile=TransitionProfile.FAST
        )
        self.c.process_requests()

    def frame(self, title: str) -> tuple[Image.Image, dict[str, Any]]:
        snap = self.c.snapshot()
        pose = dict(snap.sent.values or snap.displayed.values or {})
        f, t = snap.extra["follow"], snap.extra["talk"]
        front = f["front"]["bearing_deg"]
        fk = wrap180(self.fk.bearing_at_pan(pose) - front)
        self.scene.set_logical_pose(pose, self.mapping)
        img = Image.fromarray(self.renderer.render(self.scene.data))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, SIZE, 62], fill=(0, 0, 0))
        d.text((5, 3), title, fill=(255, 255, 255))
        d.text(
            (5, 18),
            f"target {f['target_deg']:+.0f}  FK facing {fk:+.1f}  pan {pose['shoulder_pan']:+.1f}",
            fill=(255, 230, 0),
        )
        reach = f["state"] if f["state"] in ("SUSPENDED", "OFF") else f"{f['state']} {f['reach']}"
        if f.get("residual_deg"):
            reach += f" residual {f['residual_deg']:+.0f}"
        ok = f["state"] == "TRACKING" and f["reach"] == "REACHABLE"
        d.text((5, 33), reach, fill=(120, 255, 120) if ok else (255, 140, 120))
        ref = f.get("reference") or {}
        d.text(
            (5, 47),
            f"ref {ref.get('classification')}  gripper {pose['gripper']:.1f}"
            + ("  TALK" if t["active"] else ""),
            fill=(180, 200, 255),
        )
        record = {
            "title": title,
            "target_deg": f["target_deg"],
            "fk_facing_deg": round(fk, 2),
            "pan": round(pose["shoulder_pan"], 3),
            "state": f["state"],
            "reach": f["reach"],
            "residual_deg": f["residual_deg"],
            "reference": ref.get("classification"),
            "suspended_reason": f.get("suspended_reason"),
            "gripper": round(pose["gripper"], 2),
            "talk": t["state"],
        }
        return img, record


def sheet(images: list[Image.Image], path: Path) -> None:
    out = Image.new("RGB", (SIZE * len(images), SIZE))
    for i, im in enumerate(images):
        out.paste(im, (SIZE * i, 0))
    out.save(path)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        s = Stage(Path(tmp))
        # 1. normal-side behavior following several angles
        nod = s.behavior("nod (normal side)", NORMAL, "wrist_flex")
        s.run(rq.SetFollow(True, 0.0, TransitionProfile.FAST))
        s.play(nod)
        imgs, recs = [], []
        for th in (-60.0, 0.0, 45.0, 90.0):
            s.c.set_follow_target(th)
            s.tick(60)
            im, r = s.frame(f"1 NORMAL behavior, target {th:+.0f}")
            imgs.append(im)
            recs.append(r)
        sheet(imgs, OUT / "1_normal_following.png")
        summary["1_normal_following"] = recs
        # 2. reversed-side behavior, front set to the working direction, reachable angles
        talk = s.behavior("talk (reversed side)", REVERSED, "elbow_flex")
        s.play(talk)
        s.tick(80)
        s.run(rq.SetFollow(False))
        s.tick(60)
        s.run(rq.SetFrontReference(bearing_deg=105.0))  # the owner's working direction (review §2)
        s.run(rq.SetFollow(True, 0.0, TransitionProfile.FAST))
        imgs, recs = [], []
        for th in (-20.0, 0.0, 60.0, 150.0):
            s.c.set_follow_target(th)
            s.tick(70)
            im, r = s.frame(f"2 REVERSED, front 105, target {th:+.0f}")
            imgs.append(im)
            recs.append(r)
        sheet(imgs, OUT / "2_reversed_following.png")
        summary["2_reversed_following"] = recs
        # 3. unreachable angle (reported, never "tracking")
        s.c.set_follow_target(-90.0)
        s.tick(70)
        im3, r3 = s.frame("3 REVERSED, target -90 (unreachable)")
        summary["3_unreachable"] = [r3]
        # 4. ambiguous behavior: pan suspended while the other joints animate
        cross = s.behavior(
            "crossing (ambiguous)", REVERSED, "wrist_flex", sweep=("shoulder_lift", -60.0, 20.0)
        )
        s.c.set_follow_target(0.0)
        s.tick(40)
        s.play(cross)
        imgs4, recs4 = [], []
        for _ in range(2):
            s.tick(25)
            im, r = s.frame("4 MIXED behavior: pan held")
            imgs4.append(im)
            recs4.append(r)
        sheet([im3, *imgs4], OUT / "3_unreachable_4_ambiguous.png")
        summary["4_ambiguous"] = recs4
        # 5. simultaneous Follow Me + Talk Mode on a reversed talking loop
        s.play(talk)
        s.run(rq.TalkStart("normal", 60.0, seed=11))
        s.c.set_follow_target(30.0)
        s.tick(60)
        imgs, recs = [], []
        for _ in range(4):
            s.tick(7)
            im, r = s.frame("5 Follow +30 & Talk (normal)")
            imgs.append(im)
            recs.append(r)
        sheet(imgs, OUT / "5_follow_and_talk.png")
        summary["5_follow_and_talk"] = recs
        assert s.c.snapshot().connection == Connection.PLAYING_BEHAVIOR
        s.renderer.close()
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

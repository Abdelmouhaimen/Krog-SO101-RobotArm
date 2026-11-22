# ruff: noqa: ASYNC220, ASYNC240
"""Real loopback receiver → companion → real Studio API, fake arm. Sender is simulated."""

import asyncio
import contextlib
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from so101_voice.character.director import BehaviorDirector
from so101_voice.character.library import BehaviorLibrary
from so101_voice.config import CharacterConfig, FollowConfig, RobotToolsConfig
from so101_voice.perception.follow import FollowController
from so101_voice.perception.lan import PerceptionClient
from so101_voice.robot.client import RobotAPIClient
from so101_voice.robot.follow import FollowAPI
from so101_voice.robot.motion import RobotMotion

from .test_integration_so101_fake import _free_port, run_so101_service

pytestmark = pytest.mark.robot_fake


async def test_lan_to_real_fake_arm_and_selected_monitor(tmp_path):
    root = Path(__file__).resolve().parents[2]
    port = _free_port()
    base_receiver = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            str(root / "robot-controller/.venv/bin/python"),
            "-m",
            "lan_bridge.receiver",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=root / "lan-receiver",
        env=os.environ | {"BRIDGE_TOKEN": "offline-test-token"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(
            base_url=base_receiver, trust_env=False, headers={"X-Bridge-Token": "offline-test-token"}
        ) as sender:
            for _ in range(100):
                try:
                    if (await sender.get("/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("receiver did not start")
            with run_so101_service(tmp_path, seed_behaviors=True) as (base, token, _):
                robot = RobotAPIClient(base, token, timeout_s=2)
                follow = FollowAPI(RobotAPIClient(base, token, timeout_s=1))
                lan = PerceptionClient(base_receiver, "offline-test-token")
                library = BehaviorLibrary(robot)
                director = BehaviorDirector(
                    robot,
                    RobotMotion(robot),
                    library,
                    CharacterConfig(session_start_reaction=None, min_switch_interval_s=0),
                    RobotToolsConfig(command_poll_s=0.05),
                )
                ctl = FollowController(
                    lan, follow, FollowConfig(calibration_valid=True, acquire_s=0), director
                )
                try:
                    assert "fake" in (await robot.status())["robot"]["driver"].lower()
                    await director.start()
                    await director.play("idle_watching", key="test-watching")
                    ctl.on_phase("listening")
                    # Two distinct synthetic monitor images: the latest selected source must win.
                    for seq, monitor in enumerate((b"monitor-1", b"SELECTED-monitor-2")):
                        jpeg = b"\xff\xd8\xff" + monitor + b"\xff\xd9"
                        response = await sender.post(
                            "/v1/frame",
                            content=jpeg,
                            headers={
                                "Content-Type": "image/jpeg",
                                "X-Session-Id": "test-monitor",
                                "X-Sequence": str(seq),
                            },
                        )
                        assert response.json()["accepted"]
                    obs = await lan.screen()
                    assert obs.fresh_jpeg() == jpeg and obs.seq == 1
                    packet = dict(
                        session="fake-webcam-session",
                        seq=0,
                        found=True,
                        angle_deg=10,
                        camera_angle_deg=0,
                        confidence=0.95,
                    )
                    await sender.post("/v1/angle", json=packet)
                    await ctl.set_enabled(True)
                    latencies = []
                    update_times = []
                    for seq in range(1, 45):
                        start = time.monotonic()
                        packet = dict(
                            session="fake-webcam-session",
                            seq=seq,
                            found=True,
                            angle_deg=10 + seq / 10,
                            camera_angle_deg=seq / 10,
                            confidence=0.95,
                        )
                        assert (await sender.post("/v1/angle", json=packet)).json()["accepted"]
                        await ctl.step()
                        if ctl.enabled:
                            update_times.append(time.monotonic())
                            latencies.append(1000 * (update_times[-1] - start))
                        await asyncio.sleep(max(0, 0.05 - time.monotonic() + start))
                    assert ctl.enabled, ctl.reason
                    for _ in range(30):
                        fs = (await robot._request("GET", "/api/v1/follow/status")).body
                        if fs.get("target_updates", 0) >= 43:
                            break
                        await asyncio.sleep(0.05)
                    assert fs["enabled"] and fs["target_deg"] == pytest.approx(14.4, abs=0.5)
                    assert (await robot.command(director.active.command_id))["state"] == "running"
                    # Independent gripper overlay while a meaningful reaction runs.
                    await director.play("wavy", key="test-wave", next_idle="idle_curious")
                    wave = director.active.command_id
                    await robot.talk_start(lease_s=2)
                    assert (await robot.talk_status())["active"]
                    assert (await robot._request("GET", "/api/v1/follow/status")).body["enabled"]
                    await ctl.set_enabled(False)
                    await ctl.step()
                    for _ in range(30):
                        if not (await robot._request("GET", "/api/v1/follow/status")).body["enabled"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        pytest.fail("Follow Me did not release")
                    assert (await robot.talk_status())["active"]
                    assert director.active.command_id == wave
                    await robot.talk_stop()
                    # Wait for the real command completion → planned idle, not a fixed timer.
                    deadline = time.monotonic() + 25
                    while director.active.label != "idle_curious" and time.monotonic() < deadline:
                        await asyncio.sleep(0.1)
                    assert director.active.label == "idle_curious"
                    rate = (len(update_times) - 1) / (update_times[-1] - update_times[0])
                    report = {
                        "source": "simulated senders / loopback / fake arm",
                        "updates": len(update_times),
                        "update_hz": round(rate, 2),
                        "median_ingest_to_api_ms": round(statistics.median(latencies), 2),
                        "max_ingest_to_api_ms": round(max(latencies), 2),
                    }
                    print("M3C_MEASUREMENTS " + json.dumps(report))
                    (root / "voice-companion/planning/M3C_MEASUREMENTS.json").write_text(
                        json.dumps(report, indent=2) + "\n"
                    )
                finally:
                    await ctl.aclose()
                    await director.aclose()
                    await lan.aclose()
                    await robot.aclose()
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(5)
        if process.poll() is None:
            process.kill()
            process.wait()

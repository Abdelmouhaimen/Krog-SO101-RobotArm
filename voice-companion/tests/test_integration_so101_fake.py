"""Integration against the REAL SO-101 service code, headless, with a FAKE arm (never real hardware).

Run with:  SO101_REPO=/path/to/robot-controller pytest -m robot_fake
It starts ``python -m so101_companion --headless --api --fake-hardware --fake-arm`` from that repo's
own virtualenv, with a throw-away token file, on a free port; nothing in ~/.config is touched.
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from so101_voice.audio.playback import PlaybackEvent, PlaybackState
from so101_voice.motion.speech_motion import RobotLink, SpeechMotionCoordinator
from so101_voice.robot.client import RobotAPIClient

pytestmark = pytest.mark.robot_fake


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def seeded_fake_library(repo: Path):
    """Temporarily copy the owner's recorded behaviors into the service's FAKE-hardware scratch library
    (marked simulation-authored, see below).

    ``--fake-hardware`` reads ``artifacts/fake-hardware-behaviors`` (git-ignored scratch, never the real
    ``behaviors/`` library), and the service has no option to choose another directory. Only files that
    were missing are copied, and exactly those are removed afterwards. No robot code or real library
    file is modified.
    """
    src = repo / "behaviors"
    dst = repo / "artifacts" / "fake-hardware-behaviors"
    created_dir = not dst.exists()
    dst.mkdir(parents=True, exist_ok=True)
    added = []
    for f in sorted(src.glob("*.json")):
        target = dst / f.name
        if not target.exists():
            data = json.loads(f.read_text())
            # The real recordings are bound to the REAL arm's calibration, and the fake arm (correctly)
            # refuses them. The scratch copies are marked simulation-authored so the fake controller
            # accepts them: same ids, names, durations, trims, repetitions and endings. Tests therefore
            # exercise the API semantics with the exact library metadata, not physical playback.
            data["origin"] = "sim_sliders"
            for sample in data.get("samples", []):  # simulation origins replay the "requested" channel
                if sample.get("requested") is None and sample.get("actual") is not None:
                    sample["requested"] = sample["actual"]
            target.write_text(json.dumps(data))
            added.append(target)
    try:
        yield len(added)
    finally:
        for f in added:
            f.unlink(missing_ok=True)
        if created_dir and not any(dst.iterdir()):
            dst.rmdir()


@contextlib.contextmanager
def run_so101_service(tmp: Path, *, seed_behaviors: bool = False):
    """Start the SO-101 service headless with a FAKE arm; yield ``(base_url, token, proc)``."""
    repo = os.environ.get("SO101_REPO")
    if not repo:
        pytest.skip("SO101_REPO not set")
    repo = Path(repo)
    py = repo / ".venv/bin/python"
    if not py.exists():
        pytest.skip(f"{py} not found")
    with contextlib.ExitStack() as stack:
        if seed_behaviors:
            stack.enter_context(seeded_fake_library(repo))
        yield from _run(repo, py, tmp)


def _run(repo: Path, py: Path, tmp: Path):
    env = os.environ | {
        "SO101_API_TOKEN_FILE": str(tmp / "tokens.json"),
        "XDG_CONFIG_HOME": str(tmp / "xdg-config"),  # keep the fake run away from the user's config
        "MUJOCO_GL": os.environ.get("MUJOCO_GL", "egl"),
    }
    out = subprocess.run(
        [str(py), "-m", "so101_companion", "--create-api-token", "voice-companion-test"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    token = next(w for w in out.stdout.split() if w.startswith("so101_"))
    port = _free_port()
    proc = subprocess.Popen(
        [
            str(py),
            "-m",
            "so101_companion",
            "--headless",
            "--api",
            "--fake-hardware",
            "--fake-arm",
            "--api-port",
            str(port),
        ],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"SO-101 service exited: {proc.stdout.read()[-2000:]}")
            try:
                r = httpx.get(
                    f"{base}/api/v1/status",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=1,
                    trust_env=False,
                )
                if r.status_code == 200 and r.json().get("connection") not in (None, "DISCONNECTED"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("SO-101 service did not become ready")
        yield base, token, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        print((proc.stdout.read() or "")[-1500:], file=sys.stderr)


@pytest.fixture(scope="module")
def so101_service(tmp_path_factory):
    with run_so101_service(tmp_path_factory.mktemp("so101")) as (base, token, _proc):
        yield base, token


async def _wait_state(client, want, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = await client.talk_status()
        if st.get("state") == want:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(f"talk state never became {want}: {st}")


async def test_talk_mode_lifecycle_on_fake_arm(so101_service):
    base, token = so101_service
    status_client = RobotAPIClient(base, token, timeout_s=2)
    robot = RobotAPIClient(base, token, timeout_s=2)
    print(json.dumps((await status_client.status()).get("ready", {}).get("talk"), indent=1))
    c = SpeechMotionCoordinator(robot, lease_s=1.5, renew_interval_s=0.5, stop_grace_s=0.1)
    c.start()
    try:
        c.on_playback(PlaybackEvent(PlaybackState.PLAYING, "started", 0))
        st = await _wait_state(status_client, "ON")
        assert c.status.link is RobotLink.OK
        await asyncio.sleep(2.0)  # longer than the lease: renewals keep it on
        st = await status_client.talk_status()
        assert st["state"] == "ON", st
        assert c.stats.renewals >= 2
        c.on_playback(PlaybackEvent(PlaybackState.IDLE, "flushed:barge_in", 0))
        await _wait_state(status_client, "OFF")
        # Crash safety: start, then kill the coordinator without a stop; the robot lease ends it.
        c.on_playback(PlaybackEvent(PlaybackState.PLAYING, "started", 0))
        await _wait_state(status_client, "ON")
        c._task.cancel()
        await asyncio.sleep(1.5 + 0.8)
        assert (await status_client.talk_status())["state"] in ("OFF", "STOPPING")
    finally:
        await c.aclose()
        await robot.aclose()
        await status_client.aclose()

"""ADR-018 CLI wiring, as a subprocess: token creation, and the headless FAKE-hardware API service
(--headless --api --fake-hardware --fake-arm). Real hardware cannot be used headless."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run(*args: str, env: dict[str, str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "so101_companion", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_token_cli_and_headless_fake_service(tmp_path: Path) -> None:
    env = os.environ | {"SO101_API_TOKEN_FILE": str(tmp_path / "tokens.json"), "QT_QPA_PLATFORM": "offscreen"}
    made = run("--create-api-token", "ci", env=env)
    assert made.returncode == 0, made.stderr
    token = made.stdout.strip().splitlines()[-1]
    assert token.startswith("so101_") and token not in (tmp_path / "tokens.json").read_text()

    refused = run("--headless", env=env)  # real hardware is never driven headless
    assert refused.returncode == 2 and "--fake-hardware" in refused.stderr

    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "so101_companion",
            "--headless",
            "--fake-hardware",
            "--fake-arm",
            "--api-port",
            str(port),
            "--smoke-seconds",
            "40",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        info = json.loads(proc.stdout.readline())
        assert info["api"] == f"http://127.0.0.1:{port}" and info["fake_hardware"] is True
        auth = {"Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=info["api"], timeout=10.0) as c:
            assert c.get("/api/v1/status").status_code == 401
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                st = c.get("/api/v1/status", headers=auth).json()
                if st["connection"] == "ARMED" and st["rest_pose_ready"]:
                    break
                time.sleep(0.1)
            assert st["mode"] == "SIM_TO_REAL" and st["ready"]["go_rest"]["ok"], st
            assert st["robot"]["driver"].startswith("FakeRobot")
            r = c.post("/api/v1/rest", headers=auth)
            assert r.status_code == 202 and r.json()["state"] == "running"
            r = c.post("/api/v1/emergency-hold", headers=auth)
            assert r.status_code == 202 and r.json()["state"] == "completed"
            r = c.post("/api/v1/rest", headers=auth)
            assert r.status_code == 409 and "Emergency Hold is latched" in r.json()["detail"]
    finally:
        proc.terminate()
        proc.wait(timeout=15)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""

"""Minimal Python client for the SO-101 HTTP API (Milestone 2.5, ADR-018). Standard library only.

Intended as the starting point for the future Milestone 3 companion (a separate repository). It shows
the reactive pattern: fire a command, get its id back at once, and supersede it whenever the situation
changes. Nothing waits for a motion to finish unless you ask it to.

    export SO101_API_TOKEN=so101_...           # from: so101-companion --create-api-token my-agent
    python examples/api_client.py status
    python examples/api_client.py behaviors
    python examples/api_client.py play <behavior-id> --repeat until_stopped
    python examples/api_client.py pose <pose-id>
    python examples/api_client.py rest
    python examples/api_client.py hold
    python examples/api_client.py frame out.jpg
    python examples/api_client.py demo <loop-behavior-id> <pose-id>   # loop, then supersede with a pose
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

TERMINAL = {"completed", "superseded", "cancelled", "rejected", "failed"}


class ApiError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class SO101Client:
    def __init__(
        self, base_url: str = "http://127.0.0.1:8765", token: str | None = None, timeout: float = 5.0
    ):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.token = token or os.environ.get("SO101_API_TOKEN", "")
        self.timeout = timeout

    # -- transport --------------------------------------------------------------------------
    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, idem: str | None = None
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if idem:
            req.add_header("Idempotency-Key", idem)  # safe retries: the same key never dispatches twice
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed: Any = json.loads(payload)
            except ValueError:
                parsed = payload.decode(errors="replace")
            raise ApiError(exc.code, parsed) from None

    def _command(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a motion command. Returns its record at once (state 'running' or 'queued'); a controller
        refusal raises ApiError(409) with the reason in body['detail']."""
        key = str(uuid.uuid4())
        for attempt in range(3):  # retry transport errors with the SAME key
            try:
                rec: dict[str, Any] = self._call("POST", path, body or {}, idem=key)
                return rec
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(0.2)
        raise AssertionError("unreachable")

    # -- state ------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return dict(self._call("GET", "/status"))

    def behaviors(self) -> list[dict[str, Any]]:
        return list(self._call("GET", "/behaviors")["behaviors"])

    def poses(self) -> list[dict[str, Any]]:
        return list(self._call("GET", "/poses")["poses"])

    def command(self, command_id: str) -> dict[str, Any]:
        return dict(self._call("GET", f"/commands/{command_id}"))

    # -- commands (each supersedes whatever motion is running) ------------------------------
    def play(
        self,
        behavior_id: str,
        *,
        speed: float | None = None,
        repeat: int | str | None = None,  # 1..1000 or "until_stopped"
        getting_into_position: str = "normal",  # gentle | normal | fast
        between_repetitions: str = "fast",
        after_behavior: str = "saved",  # saved | rest | hold | pose
        after_pose_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "getting_into_position": getting_into_position,
            "between_repetitions": between_repetitions,
            "after_behavior": after_behavior,
        }
        if speed is not None:
            body["speed"] = speed
        if repeat is not None:
            body["repeat"] = repeat
        if after_pose_id is not None:
            body["after_pose_id"] = after_pose_id
        return self._command(f"/behaviors/{behavior_id}/play", body)

    def go_pose(self, pose_id: str, transition: str = "normal") -> dict[str, Any]:
        return self._command(f"/poses/{pose_id}/go", {"transition": transition})

    def go_rest(self) -> dict[str, Any]:
        return self._command("/rest")

    def pause(self) -> dict[str, Any]:
        return self._command("/playback/pause")

    def resume(self) -> dict[str, Any]:
        return self._command("/playback/resume")

    def emergency_hold(self) -> dict[str, Any]:
        return self._command("/emergency-hold")

    def frame(self) -> bytes:
        data: bytes = self._call("GET", "/simulation/frame")
        return data

    def wait(self, command_id: str, timeout: float = 30.0, every: float = 0.1) -> dict[str, Any]:
        """Optional: poll until the command reaches a terminal state."""
        deadline = time.monotonic() + timeout
        while True:
            rec = self.command(command_id)
            if rec["state"] in TERMINAL or time.monotonic() > deadline:
                return rec
            time.sleep(every)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("SO101_API_URL", "http://127.0.0.1:8765"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("behaviors")
    sub.add_parser("poses")
    pl = sub.add_parser("play")
    pl.add_argument("behavior_id")
    pl.add_argument("--speed", type=float)
    pl.add_argument("--repeat", help="1..1000 or until_stopped")
    pl.add_argument("--after", default="saved", choices=["saved", "rest", "hold", "pose"])
    pl.add_argument("--after-pose")
    po = sub.add_parser("pose")
    po.add_argument("pose_id")
    po.add_argument("--transition", default="normal", choices=["gentle", "normal", "fast"])
    sub.add_parser("rest")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("hold")
    fr = sub.add_parser("frame")
    fr.add_argument("out")
    de = sub.add_parser("demo")
    de.add_argument("loop_behavior_id")
    de.add_argument("pose_id")
    a = p.parse_args(argv)
    c = SO101Client(a.url)
    try:
        if a.cmd == "status":
            out: Any = c.status()
        elif a.cmd == "behaviors":
            out = c.behaviors()
        elif a.cmd == "poses":
            out = c.poses()
        elif a.cmd == "play":
            repeat: int | str | None = a.repeat
            if isinstance(repeat, str) and repeat.isdigit():
                repeat = int(repeat)
            out = c.play(
                a.behavior_id,
                speed=a.speed,
                repeat=repeat,
                after_behavior=a.after,
                after_pose_id=a.after_pose,
            )
        elif a.cmd == "pose":
            out = c.go_pose(a.pose_id, a.transition)
        elif a.cmd == "rest":
            out = c.go_rest()
        elif a.cmd == "pause":
            out = c.pause()
        elif a.cmd == "resume":
            out = c.resume()
        elif a.cmd == "hold":
            out = c.emergency_hold()
        elif a.cmd == "frame":
            with open(a.out, "wb") as fh:
                fh.write(c.frame())
            out = {"saved": a.out}
        else:  # demo: a looping behavior, superseded 3 s later by a pose (no rest in between)
            loop = c.play(a.loop_behavior_id, repeat="until_stopped")
            print("looping:", loop["id"], loop["state"])
            time.sleep(3.0)
            pose = c.go_pose(a.pose_id)
            print("pose:", pose["id"], pose["state"], "replaced", pose["replaced"])
            print("loop is now:", c.command(loop["id"])["state"])
            out = c.wait(pose["id"])
    except ApiError as exc:
        print(json.dumps({"error": exc.status, "body": exc.body}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Test doubles: a fake SO-101 HTTP API and fake audio I/O."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from so101_voice.audio.playback import PlaybackBuffer
from so101_voice.robot.client import RobotAPIClient

TOKEN = "so101_test_token"

# The owner's library (2026-11-08): name -> (id, saved repeat_count (None = until stopped), ending, seconds)
LIBRARY: dict[str, tuple[str, int | None, str, float]] = {
    "idle_watching": ("57934d5b-9846-4a77-deef-c5915aaaaaaa", None, "rest", 12.7),
    "idle_curious": ("5a9f4fa9-fa77-408a-9119-a8e4aac27323", None, "rest", 7.0),
    "idle_talking": ("a0ff5c16-3252-4d83-8473-b6092df19f74", None, "rest", 4.8),
    "idle_listening": ("87f79cf4-f862-4097-9e58-46c261f78132", None, "rest", 5.5),
    "idle_hyped": ("aa1ee438-2acf-4711-8bd5-771629df4206", None, "rest", 8.6),
    "idle_sad": ("e7ab9f57-2c9d-4f72-bdd0-4022b4091233", None, "rest", 4.8),
    "wake_up": ("292819ab-0f08-4edc-bb01-381b2868f590", 1, "hold", 3.9),
    "curious_peak": ("532af302-b096-4fde-9ea5-f1487c1cf71c", 1, "hold", 3.6),
    "thinking": ("67ec2fe7-7b1e-49ad-a96b-9c667dbbdc2f", 1, "hold", 8.7),
    "acknowledge": ("d90dc036-0412-49b5-95ea-8e509e67a8b1", 1, "hold", 4.3),
    "sleepy": ("17a75ca1-eae5-47d9-9a2a-e1ab5f046b3a", 1, "hold", 3.3),
    "wavy": ("2133246b-e326-4a27-a6a0-693f054ce492", 1, "hold", 2.1),
    "high_five_offer": ("68338d92-1ad3-4507-86e3-95cf1c7c665a", 1, "hold", 2.6),
    "shy_reaction": ("9d5ccacb-0eba-4579-acbd-3f7ca0dd1d87", 1, "hold", 3.8),
    "comic_rage": ("1bafbb0a-dfaa-4500-ac96-8e04ca7ac981", 1, "hold", 4.3),
}
TERMINAL = {"completed", "superseded", "cancelled", "failed", "rejected"}


@dataclass
class FakeSO101API:
    """Mimics /api/v1/talk/* of the robot repo (ADR-019): lease renewal, harmless stop, 401/409/down."""

    down: bool = False
    reject_reason: str | None = None  # e.g. "Emergency Hold is latched" -> 409
    latency_s: float = 0.0
    calls: list[tuple[str, dict]] = field(default_factory=list)
    follow_on: bool = False
    follow_angle: float | None = None
    talk_on: bool = False
    lease_until: float = 0.0
    clock: callable = time.monotonic
    # ---- behaviors / poses / command ledger (shape verified against the real service) ----
    behaviors: dict = field(default_factory=lambda: dict(LIBRARY))
    poses: dict = field(default_factory=dict)  # name -> id
    time_scale: float = 0.02  # a 2.1 s wave completes in 42 ms (+ entry_s)
    entry_s: float = 0.05  # "getting into position" before the recording itself
    auto_complete: bool = True  # False: one-shots complete only via complete(command_id)
    fault: str | None = None
    emergency_hold: bool = False
    driver: str = "FakeRobot (offline test double)"
    ledger: dict = field(default_factory=dict)  # id -> record
    keys: dict = field(default_factory=dict)  # Idempotency-Key -> (id, body)
    active_id: str | None = None
    motion_posts: list = field(default_factory=list)  # (path, body, key)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> RobotAPIClient:
        return RobotAPIClient("http://so101.test", TOKEN, transport=self.transport())

    @property
    def talk_calls(self) -> list[str]:
        return [p.rsplit("/", 1)[-1] for p, _ in self.calls if "/talk/" in p and not p.endswith("status")]

    def talk_active(self) -> bool:
        if self.talk_on and self.clock() >= self.lease_until:
            self.talk_on = False  # lease expired on the robot side
        return self.talk_on

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.latency_s:
            import asyncio

            await asyncio.sleep(self.latency_s)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "missing or invalid API token"})
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.calls.append((path, body))
        if path == "/api/v1/talk/start" and request.method == "POST":
            if self.reject_reason:
                return httpx.Response(409, json={"state": "rejected", "detail": self.reject_reason})
            self.talk_on = True
            self.lease_until = self.clock() + float(body.get("lease_s", 10.0))
            return httpx.Response(202, json={"id": f"cmd{len(self.calls)}", "state": "running"})
        if path == "/api/v1/talk/stop" and request.method == "POST":
            self.talk_on = False
            return httpx.Response(202, json={"id": f"cmd{len(self.calls)}", "state": "running"})
        if path == "/api/v1/talk/status":
            on = self.talk_active()
            return httpx.Response(
                200,
                json={
                    "state": "ON" if on else "OFF",
                    "lease_remaining_s": max(0.0, self.lease_until - self.clock()) if on else 0.0,
                },
            )
        if path == "/api/v1/follow/status":
            return httpx.Response(
                200,
                json={
                    "enabled": self.follow_on,
                    "state": "TRACKING" if self.follow_on else "OFF",
                    "target_deg": self.follow_angle,
                },
            )
        if path == "/api/v1/follow" and request.method == "POST":
            if body["enabled"] and (self.fault or self.emergency_hold):
                return httpx.Response(409, json={"detail": "robot safety gate"})
            self.follow_on = body["enabled"]
            if self.follow_on:
                self.follow_angle = body.get("target_angle_deg")
            return httpx.Response(202, json={"state": "completed"})
        if path == "/api/v1/follow/target":
            if not self.follow_on:
                return httpx.Response(409, json={"detail": "Follow Me off"})
            self.follow_angle = body["angle_deg"]
            return httpx.Response(202, json={"accepted": True})
        if path == "/api/v1/status":
            return httpx.Response(200, json=self.status())
        if path == "/api/v1/behaviors":
            return httpx.Response(
                200, json={"behaviors": [self._bjson(n) for n in self.behaviors], "unreadable_files": 0}
            )
        if path == "/api/v1/poses":
            return httpx.Response(
                200,
                json={
                    "poses": [
                        {"id": i, "name": n, "source": "named", "usable_on_connected_arm": True}
                        for n, i in self.poses.items()
                    ],
                    "unreadable_files": 0,
                },
            )
        if path.startswith("/api/v1/commands/"):
            rec = self.ledger.get(path.rsplit("/", 1)[-1])
            return (
                httpx.Response(200, json=self._view(rec))
                if rec
                else httpx.Response(404, json={"detail": "no command"})
            )
        if path == "/api/v1/commands":
            return httpx.Response(
                200, json={"commands": [self._view(r) for r in list(self.ledger.values())[::-1]]}
            )
        if request.method == "POST" and (
            path.startswith("/api/v1/behaviors/")
            or path.startswith("/api/v1/poses/")
            or path == "/api/v1/rest"
        ):
            return self._motion(path, body, request.headers.get("idempotency-key"))
        return httpx.Response(404, json={"detail": "not found"})

    # ------------------------------------------------------------------ behaviors and commands
    def _bjson(self, name: str) -> dict:
        bid, rc, after, dur = self.behaviors[name]
        return {
            "id": bid,
            "name": name,
            "description": "",
            "origin": "real_hand_guided",
            "revision": 1,
            "duration_s": dur,
            "updated_at_utc": "2026-11-08T00:00:00Z",
            "saved_settings": {
                "trim_start_s": 0.0,
                "trim_end_s": None,
                "speed": 1.0,
                "repeat_count": rc,
                "after": {"action": after, "pose_id": None},
            },
        }

    def ready(self) -> dict:
        reason = None
        if self.fault:
            reason = f"fault {self.fault} is latched"
        elif self.emergency_hold:
            reason = "Emergency Hold is latched"
        item = {"ok": reason is None, "reason": reason}
        return {
            "play_behavior": item,
            "go_pose": item,
            "go_rest": item,
            "follow": item,
            "talk": {"ok": True, "reason": None},
        }

    def status(self) -> dict:
        active = self.ledger.get(self.active_id) if self.active_id else None
        if active is not None:
            self._tick(active)
        return {
            "mode": "SIM_TO_REAL",
            "connection": "ARMED",
            "robot": {"driver": self.driver},
            "physical_control_enabled": True,
            "fault": None if not self.fault else {"code": self.fault, "details": {}},
            "emergency_hold_latched": "api" if self.emergency_hold else None,
            "ready": self.ready(),
            "active_command": None if active is None or active["terminal"] else self._view(active),
            "behavior": None if active is None else {"name": active["label"], "state": active["state"]},
        }

    def _tick(self, rec: dict) -> None:
        if (
            not rec["terminal"]
            and rec.get("complete_at") is not None
            and self.auto_complete
            and self.clock() >= rec["complete_at"]
        ):
            self._finish(rec, "completed")

    def _finish(self, rec: dict, state: str, **extra) -> None:
        rec.update(state=state, terminal=True, **extra)

    def _view(self, rec: dict) -> dict:
        self._tick(rec)
        return {k: v for k, v in rec.items() if k != "complete_at"}

    def _new(self, kind: str, label: str, params: dict, key: str | None, source: str = "api") -> dict:
        cid = f"cmd_{len(self.ledger) + 1:04d}"
        rec = {
            "id": cid,
            "kind": kind,
            "label": label,
            "source": source,
            "state": "running",
            "terminal": False,
            "superseded_by": None,
            "cancelled_by": None,
            "idempotency_key": key,
            "params": params,
            "complete_at": None,
            "detail": f"{kind} {label}",
        }
        prev = self.ledger.get(self.active_id) if self.active_id else None
        if prev is not None:
            self._tick(prev)
            if not prev["terminal"]:
                self._finish(prev, "superseded", superseded_by=cid)
        self.ledger[cid] = rec
        self.active_id = cid
        return rec

    def _motion(self, path: str, body: dict, key: str | None) -> httpx.Response:
        if key and key in self.keys:
            cid, old_body = self.keys[key]
            if old_body != body:
                return httpx.Response(409, json={"detail": "Idempotency-Key reused with a different body"})
            return httpx.Response(200, json=self._view(self.ledger[cid]) | {"idempotent_replay": True})
        self.motion_posts.append((path, body, key))
        if self.fault or self.emergency_hold:
            reason = self.ready()["play_behavior"]["reason"]
            cid = f"cmd_{len(self.ledger) + 1:04d}"
            self.ledger[cid] = {
                "id": cid,
                "kind": "rejected",
                "label": "",
                "source": "api",
                "state": "rejected",
                "terminal": True,
                "detail": reason,
                "idempotency_key": key,
                "superseded_by": None,
                "cancelled_by": None,
                "params": body,
                "complete_at": None,
            }
            return httpx.Response(409, json=self._view(self.ledger[cid]))
        if path.startswith("/api/v1/behaviors/"):
            bid = path.split("/")[4]
            name = next((n for n, v in self.behaviors.items() if v[0] == bid), None)
            if name is None:
                return httpx.Response(404, json={"detail": f"no behavior {bid!r}"})
            _, rc, _, dur = self.behaviors[name]
            repeat = body.get("repeat")  # omitted = the saved setting (as in the real service)
            loops = repeat == "until_stopped" or (repeat is None and rc is None)
            count = 1 if loops else (repeat if isinstance(repeat, int) else rc)
            rec = self._new(
                "play_behavior",
                name,
                {
                    "behavior_id": bid,
                    "options": body,
                    "effective_settings": {"repeat_count": None if loops else count},
                },
                key,
            )
            if not loops:
                speed = float(body.get("speed") or 1.0)
                rec["complete_at"] = self.clock() + self.entry_s + dur * count / speed * self.time_scale
        elif path.startswith("/api/v1/poses/"):
            pid = path.split("/")[4]
            name = next((n for n, i in self.poses.items() if i == pid), None)
            if name is None:
                return httpx.Response(404, json={"detail": f"no pose {pid!r}"})
            rec = self._new("go_pose", name, body, key)
            rec["complete_at"] = self.clock() + self.entry_s
        else:
            rec = self._new("go_rest", "rest", body, key)
            rec["complete_at"] = self.clock() + self.entry_s
        if key:
            self.keys[key] = (rec["id"], body)
        return httpx.Response(202, json=self._view(rec))

    def complete(self, cid: str) -> None:
        """Manual completion (with ``auto_complete = False``)."""
        rec = self.ledger[cid]
        if not rec["terminal"]:
            self._finish(rec, "completed")

    def studio_takeover(self, label: str = "idle_watching") -> str:
        return self._new("play_behavior", label, {}, None, source="studio")["id"]

    def latch_emergency_hold(self) -> None:
        self.emergency_hold = True
        active = self.ledger.get(self.active_id) if self.active_id else None
        if active is not None and not active["terminal"]:
            self._finish(active, "cancelled", cancelled_by="emergency_hold")

    def played(self) -> list[str]:
        """Behavior/pose/rest labels in the order the robot received them (excluding replays)."""
        return [
            r["label"] for r in self.ledger.values() if r["kind"] in ("play_behavior", "go_pose", "go_rest")
        ]

    def active(self) -> dict | None:
        rec = self.ledger.get(self.active_id) if self.active_id else None
        if rec is not None:
            self._tick(rec)
        return rec


class FakeAudioIO:
    """Captures nothing by itself: tests push mic chunks with ``speak()`` and pull playback with
    ``pump()`` (simulating the device callback at real-time pace or faster)."""

    def __init__(self, fail_input: str | None = None, fail_output: str | None = None) -> None:
        self.on_audio = None
        self.buffer: PlaybackBuffer | None = None
        self.fail_input = fail_input
        self.fail_output = fail_output
        self.closed = 0
        self.played = bytearray()

    @property
    def output_latency_s(self) -> float:
        return 0.0

    def start_input(self, on_audio) -> None:
        from so101_voice.audio.devices import AudioDeviceError

        if self.fail_input:
            raise AudioDeviceError(self.fail_input)
        self.on_audio = on_audio

    def start_output(self, buffer: PlaybackBuffer) -> None:
        from so101_voice.audio.devices import AudioDeviceError

        if self.fail_output:
            raise AudioDeviceError(self.fail_output)
        self.buffer = buffer

    def close(self) -> None:
        self.closed += 1

    def speak(self, pcm: bytes) -> None:
        assert self.on_audio is not None
        self.on_audio(pcm)

    def pump(self, nframes: int) -> bytes:
        assert self.buffer is not None
        out = self.buffer.read(nframes)
        self.played.extend(out)
        return out

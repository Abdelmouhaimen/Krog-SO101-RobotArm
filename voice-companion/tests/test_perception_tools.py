"""Offline receiver/tools/vision/readiness/follow checks. No external provider or real robot calls."""

import asyncio
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from so101_voice.config import AppConfig, FollowConfig
from so101_voice.perception.follow import FollowController
from so101_voice.perception.lan import PerceptionClient
from so101_voice.readiness import Check, Readiness
from so101_voice.robot.client import RobotAPIClient
from so101_voice.robot.follow import FollowAPI
from so101_voice.tools.perception_tools import register_perception_tools
from so101_voice.tools.registry import FunctionCall, ToolRegistry

from .fake_providers import FakeFlux, FakeFluxTTS, FakeOpenAI, say, until
from .rig import Rig as VoiceRig

JPEG = (Path(__file__).parent / "fixtures/screen.jpg").read_bytes()


class Rig:
    def __init__(self):
        self.seq = 1
        self.down = False
        self.auth = True
        self.face = dict(
            found=True,
            angle_deg=17,
            camera_angle_deg=2,
            confidence=0.95,
            age_s=0.01,
            session="test-face",
            seq=1,
        )
        self.screen = dict(available=True, age_s=1, seq=1, session="test-screen", bytes=len(JPEG))
        self.calls = []
        self.robot_status = {
            "robot": {"driver": "FakeRobot"},
            "connection": "HOLDING",
            "ready": {"follow": {"ok": True}},
            "physical_control_enabled": False,
        }
        self.external_follow = False
        self.robot_down = False
        self.director = SimpleNamespace(active=None, suspended=None)
        self.client = PerceptionClient(
            "http://receiver.test", "test-token", transport=httpx.MockTransport(self.receive)
        )
        self.robot = RobotAPIClient("http://robot.test", "test", transport=httpx.MockTransport(self.control))
        self.follow = FollowController(
            self.client,
            FollowAPI(self.robot),
            FollowConfig(calibration_valid=True, acquire_s=0),
            self.director,
        )
        self.follow.on_phase("listening")
        self.registry = ToolRegistry(allow_physical_tools=True)
        register_perception_tools(self.registry, self.client, self.follow)

    def receive(self, request):
        if self.down:
            raise httpx.ConnectError("offline")
        if not self.auth:
            return httpx.Response(401)
        if request.url.path == "/v1/frame":
            if not self.screen["available"]:
                return httpx.Response(404)
            return httpx.Response(
                200, content=JPEG, headers={"X-Session-Id": "test-screen", "X-Sequence": "1", "X-Age-S": "1"}
            )
        return httpx.Response(200, json={"face": self.face, "screen": self.screen})

    def control(self, request):
        if self.robot_down:
            raise httpx.ConnectError("offline")
        if request.url.path == "/api/v1/status":
            return httpx.Response(200, json=self.robot_status)
        if request.url.path == "/api/v1/follow/status":
            return httpx.Response(200, json={"enabled": self.external_follow})
        assert request.url.path in ("/api/v1/follow", "/api/v1/follow/target")
        self.calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(202, json={"state": "completed"})

    async def call(self, name, arguments=None):
        return await self.registry.execute(FunctionCall(str(time.monotonic_ns()), name, arguments or {}))

    async def step(self):
        self.face["seq"] += 1
        await self.follow.step()

    async def enable(self):
        await self.follow.set_enabled(True)
        await self.step()
        assert self.follow.enabled

    async def close(self):
        await self.follow.aclose()
        await self.client.aclose()


async def test_tools_status_bearing_actual_image_and_deduplication():
    r = Rig()
    try:
        assert set(r.registry.names) == {
            "get_perception_status",
            "get_latest_screen",
            "get_face_bearing",
            "set_follow_me",
        }
        result = json.loads((await r.call("get_perception_status")).content)["result"]
        assert result["receiver_connected"] and result["screen"]["available"]
        face = json.loads((await r.call("get_face_bearing")).content)["result"]
        assert face["angle_deg"] == 17 and face["fresh"] and face["calibration_valid"]
        assert face["bearing_ready"] and face["follow_api_configured"]
        call = FunctionCall("image", "get_latest_screen", {})
        image = await r.registry.execute(call)
        assert base64.b64decode(image.output[1]["image_url"].split(",")[1]) == JPEG
        assert "base64" not in image.content
        duplicate = await r.registry.execute(call)
        assert duplicate.duplicate and duplicate.output == image.output
        r.screen["available"] = False
        assert not json.loads((await r.call("get_latest_screen")).content)["result"]["available"]
    finally:
        await r.close()


async def test_observation_expiry_and_auth_outage_detection():
    r = Rig()
    try:
        await r.client.poll()
        r.client.received_mono -= 6
        old = r.client.snapshot()
        assert not old.face.found and old.face.angle_deg is None and not old.screen.available
        r.auth = False
        await r.client.poll()
        assert r.client.reachable and r.client.auth == "rejected" and not r.client.connected
        r.down = True
        await r.client.poll()
        assert not r.client.reachable and not r.client.snapshot().face.found
    finally:
        await r.close()


@pytest.mark.parametrize(
    "gate", ["lost", "stale", "calibration", "rest", "operator", "hold", "fault", "outage", "real", "session"]
)
async def test_follow_gates_and_no_animation_commands(gate):
    r = Rig()
    try:
        await r.enable()
        if gate == "lost":
            r.face.update(found=False, angle_deg=None, camera_angle_deg=None, confidence=None)
        elif gate == "stale":
            r.face["age_s"] = 0.6
        elif gate == "calibration":
            r.follow.cfg.calibration_valid = False
        elif gate == "rest":
            r.director.suspended = "resting"
        elif gate == "operator":
            r.robot_status["active_command"] = {"source": "studio"}
        elif gate == "hold":
            r.robot_status["emergency_hold_latched"] = True
        elif gate == "fault":
            r.robot_status["fault"] = {"code": "fault"}
        elif gate == "outage":
            r.down = True
        elif gate == "real":
            r.robot_status["robot"]["driver"] = "LeRobot SOFollower"
        elif gate == "session":
            r.follow.on_phase("idle")
        before = len(r.calls)
        await r.step()
        assert not r.follow.enabled
        assert r.calls[before:] == [("/api/v1/follow", {"enabled": False})]
    finally:
        await r.close()


async def test_follow_switch_has_no_animation_effect_and_reconnect_requires_new_packet():
    r = Rig()
    r.director.active = SimpleNamespace(terminal=True, label="idle_sad", command_id="sad")
    try:
        await r.enable()
        assert r.director.active.label == "idle_sad"
        r.down = True
        await r.step()
        r.down = False
        await r.follow.step()
        await r.follow.step()
        assert not r.follow.enabled
        await r.step()
        assert r.follow.enabled
        await r.follow.set_enabled(False)
        assert r.director.active.label == "idle_sad" and not r.follow.enabled
        assert all("follow" in path for path, _ in r.calls)
    finally:
        await r.close()


async def test_enable_searches_with_uncalibrated_or_missing_face_and_respects_external_owner():
    r = Rig()
    try:
        r.follow.cfg.calibration_valid = False
        result = await r.call("set_follow_me", {"enabled": True})
        assert result.ok and r.follow.requested and r.follow.status == "SEARCHING" and not r.calls
        r.follow.cfg.calibration_valid = True
        r.face["found"] = False
        assert (await r.call("set_follow_me", {"enabled": True})).ok
        assert r.follow.requested and r.follow.status == "SEARCHING" and not r.calls
        r.face["found"] = True
        r.external_follow = True
        await r.follow.set_enabled(True)
        await r.step()
        assert not r.follow.enabled and not r.calls
    finally:
        await r.close()


async def test_checklist_updates_missing_streams_auth_and_robot_readiness():
    r = Rig()
    ready = Readiness(
        AppConfig(), r.robot, r.client, credentials=(True, True), audio=Check("OK", "Audio devices available")
    )
    try:
        await ready.refresh()
        assert ready.checks["receiver"].status == ready.checks["screen"].status == "OK"
        assert ready.checks["providers"].status == "Waiting"
        r.face["found"] = False
        r.screen["available"] = False
        await ready.refresh()
        assert ready.checks["face"].status == ready.checks["screen"].status == "Waiting"
        r.face["found"] = True
        r.screen["available"] = True
        await ready.refresh()
        assert ready.checks["face"].status == ready.checks["screen"].status == "OK"
        r.auth = False
        r.robot_status["fault"] = {"code": "fault"}
        await ready.refresh()
        assert ready.checks["receiver"].status == ready.checks["physical"].status == "Error"
        r.robot_down = True
        await ready.refresh()
        assert ready.checks["robot"].status == "Unavailable"
        assert not r.calls  # checklist performs only read-only requests
    finally:
        await r.close()


async def test_actual_image_reaches_mocked_responses_and_is_not_replayed_next_turn():
    r = Rig()
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        oai = FakeOpenAI([say("I can inspect this image."), say("Still here.")])
        voice = VoiceRig(flux, aura, oai)
        voice.conv.tools = r.registry
        from so101_voice.turn_context import WorldContext

        voice.conv._prepare_turn = WorldContext(None, r.client, None)
        try:
            assert await voice.start()
            await flux.wait_connections(1)
            await flux.utter("What is on the screen?")
            await until(lambda: len(oai.requests) == 1, 3)
            content = oai.requests[0]["input"][-1]["content"]
            assert base64.b64decode(content[1]["image_url"].split(",")[1]) == JPEG
            await until(lambda: voice.conv._gen is None, 5)
            await flux.utter("Are you still there?")
            await until(lambda: len(oai.requests) == 2, 3)
            assert "data:image" not in json.dumps(oai.requests[1])
            await until(lambda: voice.conv._gen is None, 5)
            assert voice.ledger.llm.requests == 2
        finally:
            await voice.close()
            await r.close()


async def test_missing_gaming_pc_does_not_block_real_voice_pipeline_with_fake_providers():
    async with FakeFlux() as flux, FakeFluxTTS() as aura:
        voice = VoiceRig(
            flux,
            aura,
            FakeOpenAI([say("Voice is working.")]),
            perception={"enabled": True, "receiver_url": "http://127.0.0.1:1", "timeout_s": 0.05},
        )
        try:
            assert await voice.start()
            assert voice.companion.readiness.checks["receiver"].status == "Unavailable"
            await flux.wait_connections(1)
            await flux.utter("Hello")
            await until(lambda: "Voice is working." in voice.assistant_text(), 3)
        finally:
            await voice.close()


async def test_startup_is_read_only_and_does_not_start_paid_providers(tmp_path):
    from so101_voice.app import build_companion
    from so101_voice.config import Secrets

    from .fake_providers import DG_KEY, OA_KEY
    from .fakes import TOKEN, FakeAudioIO, FakeSO101API

    api = FakeSO101API(driver="LeRobot SOFollower")
    oai = FakeOpenAI()
    cfg = AppConfig.model_validate(
        {
            "cost": {"ledger_file": None},
            "perception": {"enabled": True, "receiver_url": "http://127.0.0.1:1", "timeout_s": 0.05},
            "robot_tools": {"allow_physical": False},
        }
    )
    comp = build_companion(
        cfg,
        Secrets(deepgram_api_key=DG_KEY, openai_api_key=OA_KEY, so101_api_token=TOKEN),
        on_event=lambda ev: None,
        audio=FakeAudioIO(),
        robot=api.client(),
        llm_transport=oai.transport(),
        root=tmp_path,
    )
    try:
        await comp.startup()
        assert comp.readiness.checks["physical"].status == "Waiting"
        assert comp.readiness.checks["receiver"].status == "Unavailable"
        assert not comp.conversation.running and not oai.requests
        assert not api.motion_posts and not api.talk_calls
        assert "get_latest_screen" in comp.tools.names and "set_attention" not in comp.tools.names
    finally:
        await comp.shutdown()


async def test_readiness_poll_cannot_make_an_older_target_look_fresh():
    r = Rig()
    try:
        r.face["age_s"] = 0.49
        await r.follow.set_enabled(True)
        original = r.robot.status

        async def delayed_status():
            status = await original()
            await asyncio.sleep(0.03)
            # Simulate an independent readiness refresh while the target's robot check awaits.
            r.face["age_s"] = 0.01
            await r.client.poll()
            return status

        r.robot.status = delayed_status
        await r.step()
        assert not r.follow.enabled and not r.calls
    finally:
        await r.close()


@pytest.mark.parametrize("confidence", [0.0, 0.1, 0.5, 1.0, None])
async def test_received_confidence_never_blocks_a_fresh_face_angle(confidence):
    r = Rig()
    try:
        r.face["confidence"] = confidence
        result = json.loads((await r.call("get_face_bearing")).content)["result"]
        assert result["bearing_ready"] and result["confidence"] == confidence
        await r.enable()
        assert r.calls[-1] == (
            "/api/v1/follow",
            {"enabled": True, "target_angle_deg": 17, "transition": "normal"},
        )
        r.face["angle_deg"] = 25
        await r.step()
        assert r.calls[-1] == ("/api/v1/follow/target", {"angle_deg": 25})
    finally:
        await r.close()


async def test_follow_defaults_to_searching_then_tracks_loses_and_resumes_without_log_spam():
    r = Rig()
    notices = []
    r.follow._notice = lambda level, text: notices.append((level, text))
    r.follow.on_phase("idle")
    r.follow.cfg.acquire_s = 0.05
    original = r.receive

    def stream(request):
        r.face["seq"] += 1
        return original(request)

    await r.client.aclose()
    r.client = PerceptionClient("http://receiver.test", "test", transport=httpx.MockTransport(stream))
    r.follow.perception = r.client
    try:
        assert r.follow.status == "OFF" and not r.follow.requested
        r.face["found"] = False
        r.follow.start()
        r.follow.on_phase("connecting")
        assert r.follow.requested and r.follow.status == "SEARCHING"
        await asyncio.sleep(0.12)
        assert not r.calls and not notices
        r.face["found"] = True
        await until(lambda: r.follow.status == "TRACKING", 2)
        assert r.follow.enabled
        await asyncio.sleep(0.12)
        assert not notices  # tracking/duplicate observations do not enter the conversation log
        r.face.update(found=False, angle_deg=None, camera_angle_deg=None)
        await until(lambda: not r.follow.enabled, 2)
        assert r.follow.requested and r.follow.status == "FACE LOST"
        r.face.update(found=True, angle_deg=31, camera_angle_deg=2)
        await until(lambda: r.follow.status == "TRACKING", 2)
        assert any(body.get("target_angle_deg") == 31 for _, body in r.calls)
        assert not notices
        await r.follow.set_enabled(False)
        assert r.follow.status == "OFF"
        r.follow.on_phase("speaking")
        r.follow.on_phase("user_speaking")
        assert not r.follow.requested  # only a new session defaults ON, not speech itself
        assert notices == [("info", "Follow Me requested OFF")]
        r.follow.on_phase("idle")
        r.follow.on_phase("connecting")
        assert r.follow.requested and r.follow.status == "SEARCHING"
        await until(lambda: r.follow.enabled, 2)
        r.follow.on_phase("idle")
        await until(lambda: not r.follow.enabled, 2)
        assert r.follow.status == "OFF"
        assert all(body.get("angle_deg", body.get("target_angle_deg", 17)) != 0 for _, body in r.calls)
    finally:
        await r.close()


async def test_follow_logs_api_error_once_and_explicit_actions_but_not_routine_updates():
    r = Rig()
    notices = []
    r.follow._notice = lambda level, text: notices.append((level, text))
    try:
        await r.enable()
        assert notices == [("info", "Follow Me requested ON")]
        r.robot_down = True
        r.face["seq"] += 1
        r.follow.start()
        await until(lambda: any(level == "warning" for level, _ in notices), 2)
        await asyncio.sleep(0.15)
        assert len([n for n in notices if n[0] == "warning"]) == 1
        assert not any("requested=True" in text for _, text in notices)
        r.robot_down = False
    finally:
        await r.close()


@pytest.mark.parametrize("gate", ["calibration", "robot", "hold", "physical"])
async def test_default_request_never_bypasses_readiness_or_authorization(gate):
    r = Rig()
    try:
        if gate == "calibration":
            r.follow.cfg.calibration_valid = False
        elif gate == "robot":
            r.robot_status["ready"]["follow"]["ok"] = False
        elif gate == "hold":
            r.robot_status["emergency_hold_latched"] = True
        elif gate == "physical":
            r.robot_status["robot"]["driver"] = "LeRobot SOFollower"
        assert r.follow.requested
        await r.step()
        await r.step()
        assert r.follow.status == "SEARCHING" and not r.follow.enabled and not r.calls
    finally:
        await r.close()


@pytest.mark.parametrize("state", ["superseded", "cancelled", "failed"])
async def test_follow_still_releases_on_external_replacement_or_command_failure(state):
    r = Rig()
    try:
        await r.enable()
        r.director.active = SimpleNamespace(terminal=False, command_id="current")

        async def terminal_record(command_id):
            assert command_id == "current"
            return {"state": state, "superseded_by": "external-command"}

        r.robot.command = terminal_record
        before = len(r.calls)
        await r.step()
        assert r.director.suspended == "operator or cancelled behavior"
        assert not r.follow.enabled
        assert r.calls[before:] == [("/api/v1/follow", {"enabled": False})]
    finally:
        await r.close()


@pytest.mark.parametrize("pending_ack", [False, True])
async def test_own_gesture_replacing_idle_during_follow_poll_does_not_release_overlay(pending_ack):
    from .fakes import FakeSO101API
    from .test_character import Harness

    api = FakeSO101API(auto_complete=False)
    h = await Harness(api, session_start_reaction=None, rest_on_stop=False).start()
    r = Rig()
    follow_robot = api.client()
    controller = FollowController(
        r.client, FollowAPI(follow_robot), FollowConfig(calibration_valid=True, acquire_s=0), h.director
    )
    try:
        await h.director.play("idle_watching", key="before-high-five")
        controller.on_phase("listening")
        await controller.step()
        r.face["seq"] += 1
        await controller.step()
        assert controller.enabled and api.follow_on
        old = h.director.active
        original = follow_robot.command
        plays = []

        async def replace_during_poll(command_id):
            if command_id == old.command_id:
                if pending_ack:
                    # The server has accepted the new play, but its HTTP response
                    # has not reached the director. Its lock protects that window.
                    accepted = asyncio.Event()
                    motion_play = h.director.motion.play_behavior

                    async def delayed_ack(*args, **kwargs):
                        rec = await motion_play(*args, **kwargs)
                        accepted.set()
                        await asyncio.sleep(0.02)
                        return rec

                    h.director.motion.play_behavior = delayed_ack
                    plays.append(
                        asyncio.create_task(
                            h.director.play("high_five_offer", key="high-five", next_idle="idle_hyped")
                        )
                    )
                    await accepted.wait()
                    assert h.director.active is old
                else:
                    await h.director.play("high_five_offer", key="high-five", next_idle="idle_hyped")
            return await original(command_id)

        follow_robot.command = replace_during_poll
        r.face["seq"] += 1
        await controller.step()
        await asyncio.gather(*plays)
        assert api.ledger[old.command_id]["state"] == "superseded"
        assert h.director.active.label == "high_five_offer"
        assert h.director.suspended is None
        assert controller.enabled and api.follow_on
        r.face["seq"] += 1
        await controller.step()
        api.complete(h.director.active.command_id)
        await until(lambda: h.director.active.label == "idle_hyped", 2)
        assert h.director.continuations_sent == 1
        assert controller.enabled and api.follow_on
    finally:
        await controller.aclose()
        await h.close()
        await r.close()

"""Screen consumption and coordinator scheduling: local providers/receiver/fake robot only."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from so101_voice.config import AppConfig
from so101_voice.conversation import Phase
from so101_voice.llm.reply import ObservationReply, SpeechStream

from .fake_providers import FakeFlux, FakeFluxTTS, FakeOpenAI, Script, say, until
from .fakes import FakeSO101API
from .rig import Rig

JPEG = (Path(__file__).parent / "fixtures/screen.jpg").read_bytes()
QUICK = {"session_start_reaction": None, "automatic_idles": False, "rest_on_stop": False}


class Receiver:
    def __init__(self):
        self.available = True
        self.age = 0.01
        self.seq = 1
        self.content_seq = None
        self.frame_requests = 0
        self.corrupt = False

    def __call__(self, request):
        if request.url.path == "/v1/frame":
            self.frame_requests += 1
            if not self.available:
                return httpx.Response(404)
            content_seq = self.seq if self.content_seq is None else self.content_seq
            jpeg = b"\xff\xd8\xff\xfe\x00\x04" + content_seq.to_bytes(2, "big") + JPEG[2:]
            return httpx.Response(
                200,
                content=b"corrupt" if self.corrupt else jpeg,
                headers={
                    "X-Session-Id": "screen-test",
                    "X-Sequence": str(self.seq),
                    "X-Age-S": str(self.age),
                },
            )
        return httpx.Response(
            200,
            json={
                "screen": {
                    "available": self.available,
                    "age_s": self.age,
                    "session": "screen-test",
                    "seq": self.seq,
                },
                "face": {"found": False},
            },
        )


class CountingModel(FakeOpenAI):
    def __init__(self, scripts=()):
        super().__init__(scripts)
        self.active = self.maximum_active = 0

    async def _sse(self, body, script):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            async for item in super()._sse(body, script):
                yield item
        finally:
            self.active -= 1


def make_rig(stt, tts, model, receiver, *, enabled=False, interval=3, **overrides):
    return Rig(
        stt,
        tts,
        model,
        api=FakeSO101API(),
        character=QUICK,
        screen={"enabled": enabled, "interval_s": interval},
        perception={"enabled": True, "receiver_url": "http://receiver.test"},
        perception_transport=httpx.MockTransport(receiver),
        **overrides,
    )


def images(body):
    return [
        part
        for item in body["input"]
        if isinstance(item.get("content"), list)
        for part in item["content"]
        if part.get("type") == "input_image"
    ]


def state(body):
    item = next(i for i in body["input"] if i.get("role") == "developer")
    return json.loads(item["content"].split(": ", 1)[1])


async def ask(rig, stt, model, text):
    n = len(model.requests)
    await stt.utter(text)
    await until(lambda: len(model.requests) == n + 1 and rig.conv._gen is None, 3)


def test_screen_defaults_and_silent_protocol():
    cfg = AppConfig()
    assert not cfg.screen.enabled and cfg.screen.interval_s == 3
    parser = SpeechStream(observation=True)
    raw = json.dumps({"speech": "", "actions": [], "observation": ""})
    assert parser.feed(raw) == ""
    assert isinstance(parser.finish(), ObservationReply)


async def test_on_includes_one_latest_image_in_first_request_for_every_turn_without_keywords():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        receiver = Receiver()
        model = FakeOpenAI([say("Hello."), say("Still here.")])
        rig = make_rig(stt, tts, model, receiver, enabled=True)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await ask(rig, stt, model, "Hi")
            receiver.seq += 1
            await ask(rig, stt, model, "And now?")
            assert len(model.requests) == 2 and receiver.frame_requests == 2
            assert all(len(images(body)) == 1 for body in model.requests)
            assert images(model.requests[0]) != images(model.requests[1])
            assert all(
                state(body)["image_included"] and state(body)["screen_observing"] for body in model.requests
            )
            assert images(model.requests[0])[0]["detail"] == "low"
            assert not any("data:image" in json.dumps(item) for item in rig.conv.history.items)
            assert all("tools" not in body for body in model.requests)
        finally:
            await rig.close()


async def test_off_keeps_conversation_and_explicit_manual_snapshot_without_enabling_watch():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        receiver = Receiver()
        model = FakeOpenAI([say("Hello."), say("The screen is here."), say("Still chatting.")])
        rig = make_rig(stt, tts, model, receiver)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await ask(rig, stt, model, "Hello there")
            assert not images(model.requests[0]) and receiver.frame_requests == 0
            await ask(rig, stt, model, "Take a screenshot")
            assert len(images(model.requests[1])) == 1 and not rig.conv.screen_enabled
            await ask(rig, stt, model, "Thanks")
            assert not images(model.requests[2]) and receiver.frame_requests == 1
            assert rig.conv.screen_observations == 0
        finally:
            await rig.close()


async def test_three_second_idle_observation_can_remain_silent_and_preserve_meaningful_visual_memory():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        receiver = Receiver()
        model = FakeOpenAI([Script([], observation="The visible screen is the main menu."), say("Hello.")])
        rig = make_rig(stt, tts, model, receiver, enabled=True)
        now = [0.0]
        rig.conv._clock = lambda: now[0]
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            now[0] = 2.9
            await asyncio.sleep(0.3)
            assert not model.requests
            now[0] = 3
            await until(lambda: model.completed == 1 and rig.conv._gen is None, 3)
            assert len(images(model.requests[0])) == 1
            assert not tts.speaks and not tts.jobs and rig.playback.played_bytes == 0
            assert not any("/talk/start" in path for path, _ in rig.api.calls)
            assert not rig.conv.history.items  # no repeated synthetic user turns
            assert rig.conv._visual_memory["summary"] == "The visible screen is the main menu."
            receiver.seq += 1
            await ask(rig, stt, model, "Hello")
            assert state(model.requests[1])["previous_screen_observation"]["summary"].endswith("main menu.")
        finally:
            await rig.close()


async def test_unchanged_images_are_not_reanalyzed_even_when_receiver_sequence_changes():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        receiver = Receiver()
        model = FakeOpenAI([Script([]), Script([])])
        rig = make_rig(stt, tts, model, receiver, enabled=True, interval=0.05)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await until(lambda: model.completed == 1 and rig.conv._gen is None, 3)
            receiver.content_seq = 1  # Sender increments sequence but sends identical JPEG bytes.
            receiver.seq += 1
            await asyncio.sleep(0.2)
            assert len(model.requests) == 1 and rig.conv.screen_observations == 1
            receiver.content_seq = 2
            await until(lambda: model.completed == 2, 3)
            assert not rig.conv.history.items
        finally:
            await rig.close()


async def test_user_speech_cancels_background_and_model_requests_never_overlap_or_queue():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        receiver = Receiver()
        model = CountingModel([Script([("delay", 1)]), say("Listening to you.")])
        rig = make_rig(stt, tts, model, receiver, enabled=True, interval=0.04)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await until(lambda: model.active == 1, 3)
            await stt.turn("StartOfTurn", "Listen")
            await until(lambda: model.aborted == 1, 3)
            await asyncio.sleep(0.12)
            assert len(model.requests) == 1 and rig.conv.phase is Phase.USER_SPEAKING
            await stt.turn("EndOfTurn", "Listen to me")
            await until(lambda: len(model.requests) == 2 and rig.conv._gen is None, 3)
            assert model.maximum_active == 1
            assert not any(
                i.get("role") == "user" and not isinstance(i.get("content"), str)
                for i in rig.conv.history.items
            )
            assert rig.conv.history.items[0] == {"role": "user", "content": "Listen to me"}
            await asyncio.sleep(0.12)
            assert len(model.requests) == 2  # same image; no missed ticks queued
        finally:
            await rig.close()


async def test_background_does_not_interrupt_user_response_and_user_vision_resets_idle_timer():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        receiver = Receiver()
        model = CountingModel([Script([("delay", 0.16), ("text", "Here with you.")]), Script([])])
        rig = make_rig(stt, tts, model, receiver, enabled=True, interval=0.05)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await stt.utter("Talk to me")
            await until(lambda: len(model.requests) == 1, 2)
            await asyncio.sleep(0.1)
            assert len(model.requests) == 1 and rig.conv.screen_observations == 0
            await until(lambda: rig.conv._gen is None, 3)
            receiver.seq += 1
            await asyncio.sleep(0.02)
            assert len(model.requests) == 1
            await until(lambda: len(model.requests) == 2, 2)
            assert model.maximum_active == 1
        finally:
            await rig.close()


@pytest.mark.parametrize("bad", ["missing", "stale", "corrupt", "negative_age", "nan_age"])
async def test_unavailable_images_skip_background_but_do_not_block_user_conversation(bad):
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        receiver = Receiver()
        if bad == "missing":
            receiver.available = False
        if bad == "stale":
            receiver.age = 6
        if bad == "corrupt":
            receiver.corrupt = True
        if bad == "negative_age":
            receiver.age = -1
        if bad == "nan_age":
            receiver.age = float("nan")
        model = FakeOpenAI([say("I am still here.")])
        rig = make_rig(stt, tts, model, receiver, enabled=True, interval=0.05)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await asyncio.sleep(0.12)
            assert not model.requests
            await ask(rig, stt, model, "Hello")
            assert not images(model.requests[0]) and not state(model.requests[0])["image_included"]
            assert rig.conv.screen_status(True) == "UNAVAILABLE"
        finally:
            await rig.close()


async def test_switching_off_cancels_pending_observation_without_touching_follow_or_behavior():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI([Script([("delay", 1)])])
        rig = make_rig(stt, tts, model, Receiver(), enabled=True, interval=0.04)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await until(lambda: len(model.requests) == 1, 2)
            active = rig.companion.director.active.command_id
            requested = rig.companion.follow.requested
            rig.conv.set_screen_enabled(False)
            await until(lambda: model.aborted == 1, 2)
            await asyncio.sleep(0.15)
            assert len(model.requests) == 1 and rig.conv.screen_status(True) == "OFF"
            assert rig.companion.follow.requested == requested
            assert rig.companion.director.active.command_id == active
            assert not rig.conv.history.items
        finally:
            await rig.close()


@pytest.mark.parametrize("limit", ["cost", "time"])
async def test_background_observations_respect_cost_and_session_limits(limit):
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI([Script([], usage={"input_tokens": 1_000_000, "output_tokens": 1})])
        receiver = Receiver()
        overrides = (
            {"cost": {"max_session_estimated_usd": 0.001}}
            if limit == "cost"
            else {"session": {"max_minutes": 0.002, "warn_minutes": 0}}
        )
        rig = make_rig(stt, tts, model, receiver, enabled=True, interval=0.03, **overrides)
        try:
            assert await rig.start()
            await until(lambda: rig.conv.budget_stopped if limit == "cost" else not rig.conv.running, 3)
            count = len(model.requests)
            receiver.seq += 1
            await asyncio.sleep(0.12)
            assert len(model.requests) == count
            if limit == "cost":
                assert rig.ledger.llm.requests == 1 and rig.ledger.llm.input_tokens == 1_000_000
            else:
                assert rig.conv.ended_by_time_limit
        finally:
            await rig.close()


async def test_image_is_rechecked_for_expiry_at_initial_request_boundary():
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI([say("Still listening.")])
        rig = make_rig(stt, tts, model, Receiver(), enabled=True)
        prepare = rig.conv._prepare_turn

        async def delayed_context(text):
            context = await prepare(text)
            context.image_received_mono -= 10
            return context

        rig.conv._prepare_turn = delayed_context
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await ask(rig, stt, model, "Hello")
            assert not images(model.requests[0])
            assert rig.conv.screen_status(True) == "UNAVAILABLE"
        finally:
            await rig.close()


async def test_aborted_vision_request_estimates_image_tokens_without_counting_base64_as_text():
    from so101_voice.llm.responses import estimate_tokens

    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        rig = make_rig(stt, tts, FakeOpenAI(), Receiver())
        try:
            body = {
                "instructions": "Watch.",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "detail": "low",
                                "image_url": "data:image/jpeg;base64," + "A" * 20000,
                            }
                        ],
                    }
                ],
                "tools": [],
            }
            rig.conv._meter_llm(body, None, "")
            assert rig.ledger.llm.estimated_requests == 1
            assert rig.ledger.llm.input_tokens >= rig.cfg.screen.estimated_image_tokens
            assert rig.ledger.llm.input_tokens < estimate_tokens(json.dumps(body))
        finally:
            await rig.close()


@pytest.mark.parametrize("speech", ["", "That angle leaves you exposed. Stay near cover."])
async def test_observation_can_choose_speech_or_silence_in_the_same_response(speech):
    async with FakeFlux() as stt, FakeFluxTTS() as tts:
        model = FakeOpenAI([Script([("text", speech)], observation="The player is near an open angle.")])
        rig = make_rig(stt, tts, model, Receiver(), enabled=True, interval=0.04)
        try:
            assert await rig.start()
            await stt.wait_connections(1)
            await until(lambda: model.completed == 1 and rig.conv._gen is None, 3)
            assert len(model.requests) == 1 and len(images(model.requests[0])) == 1
            assert rig.conv._visual_memory["summary"] == "The player is near an open angle."
            if speech:
                assert "".join(tts.speaks) == speech
                assert rig.playback.played_bytes > 0
                assert any(
                    item.get("role") == "assistant" and item["content"] == speech
                    for item in rig.conv.history.items
                )
            else:
                assert not tts.speaks and not tts.jobs and rig.playback.played_bytes == 0
                assert not rig.conv.history.items
        finally:
            await rig.close()

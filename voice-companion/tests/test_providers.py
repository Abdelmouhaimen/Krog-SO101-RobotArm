"""Provider clients in isolation: Flux STT, Aura TTS, OpenAI Responses (all against local fakes)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from so101_voice.cost import CostLedger, Prices
from so101_voice.llm.responses import (
    Finished,
    LLMError,
    ResponsesClient,
    TextDelta,
    ToolCallItem,
    build_request,
)
from so101_voice.stt.flux import FluxSTT, TurnEvent
from so101_voice.tts.flux import FluxTTS, SpeechTurn

from .fake_providers import (
    DG_KEY,
    OA_KEY,
    FakeFlux,
    FakeFluxTTS,
    FakeOpenAI,
    Script,
    sample_values,
    say,
    until,
)


# ============================================================================================ Flux (A01, A15)
def make_flux(url: str, events: list, states: list, **kw) -> FluxSTT:
    return FluxSTT(
        url=url,
        api_key=kw.pop("api_key", DG_KEY),
        model="flux-general-en",
        sample_rate=16000,
        on_turn=events.append,
        on_state=lambda s, d: states.append((s, d)),
        reconnect_initial_s=0.05,
        reconnect_max_attempts=3,
        **kw,
    )


async def test_flux_connects_streams_80ms_chunks_and_parses_turns():
    ledger = CostLedger(Prices(), stt_keyterms=True)  # the app sets this from [stt].keyterms
    async with FakeFlux() as server:
        events: list[TurnEvent] = []
        states: list = []
        stt = make_flux(server.url, events, states, keyterms=["SO-101"], eot_threshold=0.8, ledger=ledger)
        stt.start()
        stt.want(True)
        assert await stt.wait_connected(2)
        q = server.queries[0]
        assert (
            q["model"] == ["flux-general-en"]
            and q["encoding"] == ["linear16"]
            and q["sample_rate"] == ["16000"]
        )
        assert q["keyterm"] == ["SO-101"] and q["eot_threshold"] == ["0.8"]
        for _ in range(10):
            stt.send_audio(b"\x01\x00" * 1600)  # 1 s in 100 ms pieces, re-chunked to 80 ms frames
        await until(lambda: server.audio_bytes[0] == 12 * 2560)
        assert set(server.chunks[0]) == {2560}  # the last 40 ms waits for a full chunk ...
        stt.flush_partial()  # ... until the utterance ends
        await until(lambda: server.audio_bytes[0] == 32000)
        await server.turn("StartOfTurn", "hi")
        await server.turn("Update", "hi there")
        await server.turn("EndOfTurn", "hi there", trigger="model")
        await until(lambda: len(events) == 3)
        assert [e.event for e in events] == ["StartOfTurn", "Update", "EndOfTurn"]
        assert (
            events[-1].transcript == "hi there"
            and events[-1].trigger == "model"
            and events[-1].turn_index == 0
        )
        assert events[-1].latency_s is not None and events[-1].latency_s >= 0
        # A15: the audio actually sent is what is metered (and billed)
        assert ledger.stt.audio_s == pytest.approx(1.0)
        assert ledger.stt_usd() == pytest.approx(1.0 / 60 * (0.0077 + 0.0013))  # keyterms add-on included
        stt.want(False)  # graceful close: CloseStream
        await until(lambda: server.closed_by_client == 1)
        await until(lambda: not stt.connected)
        assert ledger.stt.connections == 1 and ledger.stt.connection_s > 0
        await stt.aclose()


async def test_flux_validates_audio_format_and_bad_key_is_fatal():
    with pytest.raises(ValueError):
        FluxSTT(url="ws://x", api_key="k", model="flux-general-en", sample_rate=22050, on_turn=print)
    async with FakeFlux() as server:
        events, states = [], []
        stt = make_flux(server.url, events, states, api_key="wrong")
        with pytest.raises(ValueError):
            stt.send_audio(b"\x00")  # odd byte count is not linear16
        stt.start()
        stt.want(True)
        await until(lambda: stt.failed is not None)
        assert "401" in stt.failed and states[-1][0] == "failed"
        await stt.aclose()


async def test_flux_server_error_reconnects_with_backoff():
    async with FakeFlux() as server:
        events, states = [], []
        stt = make_flux(server.url, events, states)
        stt.start()
        stt.want(True)
        await server.wait_connections(1)
        await server.send_json({"type": "Error", "code": "UNPARSABLE_CLIENT_MESSAGE", "description": "x"})
        await server.wait_connections(2)
        assert any(s == "disconnected" and "UNPARSABLE" in d for s, d in states)
        await stt.aclose()


# ============================================================================================ Aura (A05, A17)
class TtsProbe:
    def __init__(self) -> None:
        self.audio: list[tuple[int, int, bytes]] = []
        self.done: list[SpeechTurn] = []
        self.lost: list[str] = []
        self.notices: list[tuple[str, str]] = []


def make_tts(url: str, probe: TtsProbe, ledger: CostLedger | None = None, **kw) -> FluxTTS:
    return FluxTTS(
        url=url,
        api_key=DG_KEY,
        model="flux-cliff-en",
        sample_rate=24000,
        on_audio=lambda pcm, gen, grp: probe.audio.append((gen, grp, pcm)),
        on_group_done=probe.done.append,
        on_notice=lambda lv, t: probe.notices.append((lv, t)),
        on_lost=probe.lost.append,
        ledger=ledger,
        **kw,
    )


async def test_flux_tts_streams_before_final_flush_and_meters_inserted_separators():
    ledger = CostLedger(Prices())
    async with FakeFluxTTS() as server:
        probe = TtsProbe()
        tts = make_tts(server.url, probe, ledger)
        tts.begin(1)
        assert await tts.speak(1, "Hello there.")
        await until(lambda: bool(probe.audio))
        assert not probe.done and server.flushes == 0
        assert await tts.speak(1, "How are you today?")
        assert await tts.flush(1, 0, final=True)
        await until(lambda: len(probe.done) == 1)
        q = server.queries[0]
        assert q == {
            "model": ["flux-cliff-en"],
            "encoding": ["linear16"],
            "sample_rate": ["24000"],
            "expressivity": ["0"],
        }
        assert {grp for _, grp, _ in probe.audio} == {0} and probe.done[0].final
        pcm = b"".join(p for _, _, p in probe.audio)
        text = "Hello there. How are you today?"
        assert len(pcm) == len(text) * 480 and sample_values(pcm) == {FakeFluxTTS.job_value(0)}
        assert ledger.tts.characters == len(text) and ledger.tts.segments == 2
        assert ledger.tts.provider_billable_characters == len(text)
        assert ledger.tts_usd() == pytest.approx(len(text) / 1000 * 0.045)
        await tts.aclose()


async def test_flux_tts_clear_drops_late_audio_until_cleared():
    async with FakeFluxTTS(
        stale_frames_after_interrupt=4, frame_delay_s=0.01, interrupted_delay_s=0.05
    ) as server:
        probe = TtsProbe()
        tts = make_tts(server.url, probe)
        tts.begin(1)
        await tts.speak(1, "A fairly long sentence that takes a while to synthesize.")
        await tts.flush(1, 0, final=True)
        await until(lambda: len(probe.audio) >= 2)
        tts.cancel(1)  # local invalidation is immediate
        n = len(probe.audio)
        await until(lambda: server.interrupts == 1)
        await asyncio.sleep(0.15)
        assert len(probe.audio) == n and tts.discarded_audio_bytes > 0
        tts.begin(2)
        assert await tts.speak(2, "New answer.")
        await tts.flush(2, 0, final=True)
        await until(lambda: any(g.gen == 2 for g in probe.done))
        new = b"".join(p for gen, _, p in probe.audio if gen == 2)
        assert sample_values(new) == {FakeFluxTTS.job_value(1)}  # only the new job's audio
        assert not await tts.speak(1, "stale")  # old generation can no longer speak
        await tts.aclose()


async def test_flux_tts_unacknowledged_clear_replaces_the_socket():
    async with FakeFluxTTS(ack_interrupt=False, frame_delay_s=0.01) as server:
        probe = TtsProbe()
        tts = make_tts(server.url, probe, interrupt_timeout_s=0.2)
        tts.begin(1)
        await tts.speak(1, "Something long enough to still be playing.")
        await tts.flush(1, 0, final=True)
        tts.cancel(1)
        tts.begin(2)
        await tts.speak(2, "Fresh.")
        assert len(server.conns) == 2 and tts.connections == 2
        await tts.aclose()


async def test_flux_tts_flush_ack_is_not_completion_and_odd_frames_are_reassembled():
    probe = TtsProbe()
    async with FakeFluxTTS(first_audio_delay_s=1) as server:
        tts = make_tts(server.url, probe)
        tts.begin(1)
        await tts.speak(1, "Hello.")
        await until(lambda: tts._speech_id is not None)
        sid = tts._speech_id
        tts._message(json.dumps({"type": "Flushed", "speech_id": sid}))
        assert not probe.done
        tts._audio(b"\x01")
        assert not probe.audio
        tts._audio(b"\x02\x03\x04")
        assert probe.audio == [(1, 0, b"\x01\x02\x03\x04")]
        tts._message(json.dumps({"type": "SpeechMetadata", "speech_id": "wrong"}))
        assert not probe.done
        tts._message(json.dumps({"type": "SpeechMetadata", "speech_id": sid}))
        assert len(probe.done) == 1
        await tts.aclose()


async def test_flux_tts_connection_loss_while_speaking_is_reported():
    async with FakeFluxTTS(frame_delay_s=0.02) as server:
        probe = TtsProbe()
        tts = make_tts(server.url, probe)
        tts.begin(1)
        await tts.speak(1, "This will be cut off by the network.")
        await tts.flush(1, 0, final=True)
        await until(lambda: len(probe.audio) >= 1)
        await server.drop()
        await until(lambda: len(probe.lost) == 1)
        assert not tts.connected
        await tts.aclose()


# ======================================================================================== OpenAI (A03, A16)
def oai_client(fake: FakeOpenAI) -> ResponsesClient:
    return ResponsesClient(api_key=OA_KEY, base_url="http://openai.test/v1", transport=fake.transport())


def body(**kw) -> dict:
    return build_request(
        model="gpt-6-luna",
        instructions="be brief",
        input_items=[{"role": "user", "content": "hi"}],
        tools=kw.pop("tools", []),
        reasoning_effort=kw.pop("effort", "none"),
        verbosity="low",
        temperature=kw.pop("temperature", None),
        max_output_tokens=200,
    )


async def test_responses_streaming_events_and_usage():
    fake = FakeOpenAI(
        [
            Script(
                [("text", "Hello "), ("text", "world."), ("tool", "get_local_time", '{"timezone": null}')],
                usage={
                    "input_tokens": 1200,
                    "input_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
                    "output_tokens": 30,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            )
        ]
    )
    c = oai_client(fake)
    evs = [e async for e in c.stream(body())]
    assert [e.text for e in evs if isinstance(e, TextDelta)] == ["Hello ", "world."]
    call = next(e for e in evs if isinstance(e, ToolCallItem))
    assert call.name == "get_local_time" and json.loads(call.arguments) == {"timezone": None} and call.call_id
    fin = evs[-1]
    assert isinstance(fin, Finished) and fin.status == "completed" and fin.usage is not None
    assert fin.usage.cached_tokens == 1024 and fin.usage.output_tokens == 30
    req = fake.requests[0]
    assert req["store"] is False and req["stream"] is True and req["reasoning"] == {"effort": "none"}
    assert fake.headers[0]["authorization"] == f"Bearer {OA_KEY}"
    # A16: provider usage → cost
    ledger = CostLedger(Prices())
    u = fin.usage
    ledger.llm_usage(
        input_tokens=u.input_tokens, cached_tokens=u.cached_tokens, output_tokens=u.output_tokens
    )
    assert ledger.llm_usd() == pytest.approx((176 * 0.10 + 1024 * 0.01 + 30 * 0.50) / 1e6)
    await c.aclose()


def test_request_options_are_validated():
    with pytest.raises(ValueError):
        body(effort="medium", temperature=0.5)  # temperature only with effort "none"
    b = body(temperature=0.3, tools=[{"type": "function", "name": "x"}])
    assert b["temperature"] == 0.3 and b["parallel_tool_calls"] is False and b["tool_choice"] == "auto"
    assert "temperature" not in body()


async def test_responses_errors_are_classified():
    fake = FakeOpenAI([Script(status=404), Script(status=500)])
    c = oai_client(fake)
    with pytest.raises(LLMError) as e404:
        _ = [e async for e in c.stream(body())]
    assert e404.value.fatal and "not available to this API key" in str(e404.value)
    with pytest.raises(LLMError) as e500:
        _ = [e async for e in c.stream(body())]
    assert not e500.value.fatal
    assert await c.retrieve_model("gpt-6-luna") is not None
    assert await c.retrieve_model("gpt-6-nonexistent") is None
    bad = ResponsesClient(api_key="sk-wrong", base_url="http://openai.test/v1", transport=fake.transport())
    with pytest.raises(LLMError, match="401"):
        _ = [e async for e in bad.stream(body())]
    await c.aclose()
    await bad.aclose()


async def test_responses_cancel_closes_the_stream():
    fake = FakeOpenAI([say("one ", "two ", "three ", delay=0.2)])
    c = oai_client(fake)

    async def consume() -> None:
        async for _ in c.stream(body()):
            pass

    t = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await until(lambda: fake.aborted == 1)
    assert fake.completed == 0
    await c.aclose()


async def test_sse_parser_tolerates_done_and_unknown_events():
    raw = (
        b'event: response.created\ndata: {"type":"response.created","response":{}}\n\n'
        b": keep-alive comment\n\n"
        b'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n'
        b'data: {"type":"response.something_new","x":1}\n\n'
        b'data: {"type":"response.completed","response":{"status":"completed","output":[],"usage":null}}\n\n'
        b"data: [DONE]\n\n"
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=raw))
    c = ResponsesClient(api_key=OA_KEY, base_url="http://openai.test/v1", transport=transport)
    evs = [e async for e in c.stream(body())]
    assert evs[0] == TextDelta("Hi") and isinstance(evs[-1], Finished) and evs[-1].usage is None
    await c.aclose()

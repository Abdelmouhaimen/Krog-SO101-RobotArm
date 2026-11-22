"""Provider-independent components: chunker, mic gate, cost ledger and budget, history, playback markers."""

from __future__ import annotations

import json

import pytest

from so101_voice.audio.gate import InputMode, MicGate, level_dbfs
from so101_voice.audio.playback import PlaybackBuffer, PlaybackState
from so101_voice.cost import BudgetState, CostLedger, Prices, SlidingWindow
from so101_voice.llm.history import ConversationHistory
from so101_voice.tts.chunker import TextChunker, clean_for_speech


# ------------------------------------------------------------------ chunker (A05/A06 support)
def feed_all(ch: TextChunker, deltas: list[str]) -> list[str]:
    out: list[str] = []
    for d in deltas:
        out += ch.feed(d)
    return out + ch.finish()


def test_chunker_first_sentence_early_then_merges_short_ones():
    ch = TextChunker(min_chars=40)
    assert ch.feed("Hey") == [] and ch.feed("!") == []  # boundary not certain until whitespace follows
    assert ch.feed(" Nice") == ["Hey!"]
    rest = feed_all(ch, [" to see you. ", "Ok. ", "So what are we doing today, my friend? ", "Tell me."])
    assert rest == ["Nice to see you. Ok. So what are we doing today, my friend?", "Tell me."]


def test_chunker_ignores_abbreviations_numbers_and_tokens():
    ch = TextChunker(min_chars=10)
    segs = feed_all(ch, list("Dr. Smith paid 3.5 dollars, e.g. a coffee. Then he left. "))
    assert segs == ["Dr. Smith paid 3.5 dollars, e.g. a coffee.", "Then he left."]


def test_chunker_splits_long_sentences_at_clauses_then_spaces():
    ch = TextChunker(min_chars=20, soft_max_chars=60, hard_max_chars=90)
    long = (
        "This sentence goes on, and on, and on without any real end in sight because it keeps adding words "
    )
    segs = feed_all(ch, [long * 2])
    assert all(len(s) <= 90 for s in segs) and len(segs) >= 3
    assert " ".join(segs).split() == (long * 2).split()


def test_chunker_removes_markdown():
    assert (
        clean_for_speech("**Sure!** Here is `code`\n- item one\n# Title")
        == "Sure! Here is code item one Title"
    )


# ------------------------------------------------------------------ mic gate (cost-aware input)
def pcm(ms: int, value: int = 0) -> bytes:
    return int(value).to_bytes(2, "little", signed=True) * (16 * ms)


def test_gate_continuous_sends_everything():
    g = MicGate(InputMode.CONTINUOUS)
    assert g.process(pcm(20)) == pcm(20) and g.is_open


def test_gate_push_to_talk_with_preroll_and_postroll():
    g = MicGate(InputMode.PUSH_TO_TALK, preroll_ms=100, postroll_ms=60)
    for _ in range(20):
        assert g.process(pcm(20, 5)) == b""  # closed: buffered for pre-roll only
    g.set_push_to_talk(True)
    first = g.process(pcm(20, 9))
    assert len(first) == len(pcm(120))  # 100 ms pre-roll + this frame
    g.set_push_to_talk(False)
    post = [g.process(pcm(20)) for _ in range(5)]
    assert sum(len(p) for p in post) == len(pcm(60)) and not g.is_open
    assert g.suppressed_bytes > 0


def test_gate_vad_opens_on_speech_with_attack_and_closes_after_postroll():
    g = MicGate(InputMode.VAD, preroll_ms=100, postroll_ms=100, threshold_dbfs=-40, attack_ms=40)
    assert level_dbfs(pcm(20)) == -120.0 and level_dbfs(pcm(20, 3000)) > -40
    for _ in range(10):
        assert g.process(pcm(20, 10)) == b""  # quiet
    assert g.process(pcm(20, 3000)) == b""  # 20 ms loud < attack
    burst = g.process(pcm(20, 3000))
    assert g.is_open and len(burst) >= len(pcm(100))  # opened with pre-roll (incl. the onset)
    sent_quiet = [g.process(pcm(20, 10)) for _ in range(8)]
    assert not g.is_open and sum(len(s) for s in sent_quiet) == len(pcm(100))


# ------------------------------------------------------------------ cost ledger (A15–A18)
def test_ledger_totals_and_budget_states():
    snaps = []
    led = CostLedger(Prices(), max_session_usd=0.01, warn_at_fraction=0.5, on_change=snaps.append)
    led.stt_audio(60.0)  # 1 min of Flux = $0.0077
    assert led.stt_usd() == pytest.approx(0.0077)
    led.llm_usage(input_tokens=10_000, cached_tokens=2_000, cache_write_tokens=1_000, output_tokens=1_000)
    expect_llm = (7_000 * 0.10 + 2_000 * 0.01 + 1_000 * 0.125 + 1_000 * 0.50) / 1e6
    assert led.llm_usd() == pytest.approx(expect_llm)
    assert led.state is BudgetState.WARN
    led.tts_sent(100)  # $0.0045
    s = led.snapshot()
    assert s.session_usd == pytest.approx(0.0077 + expect_llm + 0.0045)
    assert s.state is BudgetState.EXCEEDED and not led.allows_new_requests()
    assert snaps[-1].state is BudgetState.EXCEEDED
    led.tts_discarded(40)
    assert led.tts.discarded_characters == 40 and led.tts_usd() == pytest.approx(0.0045)  # discarded ≠ free


def test_ledger_persists_cumulative_and_disabled_budget(tmp_path):
    f = tmp_path / "ledger.jsonl"
    a = CostLedger(Prices(), ledger_file=f)
    a.tts_sent(1000)
    a.persist()
    b = CostLedger(Prices(), ledger_file=f)
    b.tts_sent(1000)
    assert b.previous_usd == pytest.approx(0.045) and b.snapshot().cumulative_usd == pytest.approx(0.09)
    rec = json.loads(f.read_text().splitlines()[0])
    assert rec["estimate"] is True and rec["tts"]["characters"] == 1000
    assert CostLedger(Prices(), max_session_usd=None).state is BudgetState.DISABLED
    CostLedger(Prices(), ledger_file=f).persist()  # nothing spent: nothing written
    assert len(f.read_text().splitlines()) == 1


def test_ledger_estimated_llm_usage_is_flagged():
    led = CostLedger(Prices())
    led.llm_usage(input_tokens=100, cached_tokens=0, output_tokens=10, estimated=True)
    assert led.llm.estimated_requests == 1 and led.llm_usd() > 0


def test_sliding_window():
    t = [0.0]
    w = SlidingWindow(3, 10.0, clock=lambda: t[0])
    for _ in range(3):
        assert w.wait_s() == 0
        w.add()
    assert w.wait_s() == pytest.approx(10.0)
    t[0] = 10.5
    assert w.wait_s() == 0
    c = SlidingWindow(100, 60.0, clock=lambda: t[0])
    c.add(80)
    assert c.wait_s(30) > 0 and c.wait_s(20) == 0


# ------------------------------------------------------------------ history
def test_history_trims_whole_turns_and_keeps_tool_pairs():
    h = ConversationHistory(max_items=6)
    h.add_assistant("Hey!", new_turn=True)
    for i in range(3):
        h.add_user(f"q{i}")
        h.add_tool_round(
            [{"type": "function_call", "call_id": f"c{i}"}],
            [{"type": "function_call_output", "call_id": f"c{i}"}],
        )
        h.add_assistant(f"a{i}")
    items = h.items
    assert len(items) <= 6 and items[0] == {"role": "user", "content": "q2"}  # oldest whole turns dropped
    calls = [i["call_id"] for i in items if i.get("type") == "function_call"]
    outs = [i["call_id"] for i in items if i.get("type") == "function_call_output"]
    assert calls == outs


# ------------------------------------------------------------------ playback markers (A22 support)
def test_playback_markers_fire_when_their_sample_plays_and_flush_drops_them():
    t = [0.0]
    buf = PlaybackBuffer(24000, prebuffer_s=0.0, device_latency_s=0.05, clock=lambda: t[0])
    seen = []
    buf.add_marker_listener(seen.append)
    buf.mark("a")
    buf.write(b"\x01\x00" * 480)  # 20 ms
    buf.mark("b")
    buf.write(b"\x01\x00" * 480)
    buf.read(240)
    assert [m.tag for m in seen] == ["a"] and seen[0].audible_at == pytest.approx(0.05)
    buf.read(240)
    assert [m.tag for m in seen] == ["a"]
    t[0] = 0.02
    buf.read(240)
    assert [m.tag for m in seen] == ["a", "b"]
    buf.mark("never")  # its audio is flushed before it plays
    buf.write(b"\x01\x00" * 480)
    buf.flush("barge_in")
    buf.write(b"\x02\x00" * 480)
    buf.read(480)
    assert [m.tag for m in seen] == ["a", "b"] and buf.dropped_markers == 1
    assert buf.state is PlaybackState.PLAYING and buf.flushed_bytes > 0

from so101_voice.audio.playback import PlaybackBuffer, PlaybackState


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def pcm(seconds: float, rate: int = 24000) -> bytes:
    return b"\x01\x00" * int(seconds * rate)


def make(clock, **kw):
    buf = PlaybackBuffer(24000, prebuffer_s=0.06, device_latency_s=0.04, clock=clock, **kw)
    events = []
    buf.add_listener(lambda e: events.append((e.state, e.reason)))
    return buf, events


def test_waits_for_prebuffer_then_plays_real_samples():
    c = Clock()
    buf, events = make(c)
    buf.write(pcm(0.02))
    assert buf.read(480) == bytes(960)  # below prebuffer: silence, still idle
    assert buf.state is PlaybackState.IDLE and events == []
    buf.write(pcm(0.05))
    out = buf.read(480)
    assert out != bytes(960) and buf.state is PlaybackState.PLAYING
    assert events == [(PlaybackState.PLAYING, "started")]


def test_prebuffer_timeout_and_end_of_stream_start_short_audio():
    c = Clock()
    buf, events = make(c)
    buf.write(pcm(0.01))
    buf.mark_end_of_stream()
    assert buf.read(240) != bytes(480)
    assert events[0][0] is PlaybackState.PLAYING

    c2 = Clock()
    buf2, ev2 = make(c2)
    buf2.write(pcm(0.01))
    c2.t = 0.07  # older than the prebuffer window
    assert buf2.read(240) != bytes(480)
    assert ev2[0][0] is PlaybackState.PLAYING


def test_idle_only_after_last_sample_left_the_speaker():
    """The final TTS Flushed is not the end: IDLE comes when the buffer drained AND device latency elapsed."""
    c = Clock()
    buf, events = make(c)
    buf.write(pcm(0.1))
    buf.mark_end_of_stream()
    for _ in range(5):  # 5 × 20 ms consumes it all
        buf.read(480)
        c.t += 0.02
    assert buf.state is PlaybackState.PLAYING  # last chunk handed to the device is still sounding
    buf.read(480)
    assert buf.state is PlaybackState.PLAYING
    c.t += 0.05  # chunk duration + device latency
    buf.read(480)
    assert buf.state is PlaybackState.IDLE
    assert events[-1] == (PlaybackState.IDLE, "drained")


def test_flush_is_immediate_and_reported():
    c = Clock()
    buf, events = make(c)
    buf.write(pcm(1.0))
    buf.read(480)
    assert buf.flush("barge_in") is True
    assert buf.buffered_s() == 0 and buf.state is PlaybackState.IDLE
    assert events[-1] == (PlaybackState.IDLE, "flushed:barge_in")
    assert buf.read(480) == bytes(960)
    assert buf.flush("again") is False  # no duplicate event when already idle
    assert len(events) == 2


def test_mid_response_gap_stays_playing_then_drains_at_end_of_stream():
    """Live API: TTS arrives sentence by sentence; a 40 ms gap between sentences is not 'stopped talking'."""
    c = Clock()
    buf, events = make(c)
    buf.write(pcm(0.06))
    for _ in range(6):  # 3 chunks of audio, then ~60 ms of empty buffer
        buf.read(480)
        c.t += 0.02
    assert buf.state is PlaybackState.PLAYING
    buf.write(pcm(0.04))  # next sentence
    buf.mark_end_of_stream()  # final TTS Flushed
    for _ in range(2):
        buf.read(480)
        c.t += 0.02
    c.t += 0.05
    buf.read(480)
    assert events == [(PlaybackState.PLAYING, "started"), (PlaybackState.IDLE, "drained")]


def test_long_underrun_without_end_of_stream_goes_idle_and_recovers():
    c = Clock()
    buf, events = make(c, underrun_hold_s=0.3)
    buf.write(pcm(0.06))
    for _ in range(3):
        buf.read(480)
        c.t += 0.02
    c.t += 0.2  # past latency, inside the hold
    buf.read(480)
    assert buf.state is PlaybackState.PLAYING
    c.t += 0.2  # past the hold
    buf.read(480)
    assert buf.state is PlaybackState.IDLE and events[-1] == (PlaybackState.IDLE, "underrun")
    buf.write(pcm(0.1))
    buf.read(480)
    assert [s for s, _ in events] == [PlaybackState.PLAYING, PlaybackState.IDLE, PlaybackState.PLAYING]


def test_buffer_cap_drops_excess():
    c = Clock()
    buf, _ = make(c, max_buffer_s=0.1)
    buf.write(pcm(0.3))
    assert abs(buf.buffered_s() - 0.1) < 1e-6
    assert buf.dropped_bytes > 0


def test_listener_exception_does_not_break_audio_thread():
    c = Clock()
    buf = PlaybackBuffer(24000, clock=c)
    buf.add_listener(lambda e: 1 / 0)
    buf.write(pcm(0.2))
    assert buf.read(480) != bytes(960)

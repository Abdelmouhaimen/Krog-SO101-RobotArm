"""Talk Mode lease logic against the fake SO-101 API (real asyncio time, short intervals)."""

import asyncio
import time

from so101_voice.audio.playback import PlaybackEvent, PlaybackState
from so101_voice.motion.speech_motion import RobotLink, SpeechMotionCoordinator

from .fakes import FakeSO101API

PLAY = PlaybackEvent(PlaybackState.PLAYING, "started", 0)
DRAIN = PlaybackEvent(PlaybackState.IDLE, "drained", 0)
FLUSH = PlaybackEvent(PlaybackState.IDLE, "flushed:barge_in", 0)


def make(api: FakeSO101API, **kw):
    statuses = []
    params = dict(lease_s=0.3, renew_interval_s=0.1, stop_grace_s=0.08, retry_interval_s=0.15)
    params.update(kw)
    c = SpeechMotionCoordinator(api.client(), on_status=statuses.append, **params)
    c.start()
    return c, statuses


async def until(pred, timeout=2.0):
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def test_start_renew_stop_follow_real_playback():
    api = FakeSO101API()
    c, _ = make(api)
    c.on_playback(PLAY)
    await until(lambda: api.talk_calls[:1] == ["start"])
    assert api.calls[0][1]["lease_s"] == 0.3
    await asyncio.sleep(0.35)  # longer than the lease: renewals keep it alive
    assert api.talk_active()
    assert c.stats.renewals >= 2
    c.on_playback(DRAIN)
    await until(lambda: api.talk_calls[-1:] == ["stop"])
    assert not api.talk_active()
    await asyncio.sleep(0.2)
    assert api.talk_calls.count("stop") == 1  # no duplicate stop
    await c.aclose()
    assert api.talk_calls.count("stop") == 1  # already off: aclose sends nothing


async def test_short_underrun_within_grace_does_not_flap():
    api = FakeSO101API()
    c, _ = make(api, renew_interval_s=0.5, lease_s=1.0)
    c.on_playback(PLAY)
    await until(lambda: "start" in api.talk_calls)
    c.on_playback(DRAIN)
    await asyncio.sleep(0.03)  # < stop_grace_s
    c.on_playback(PLAY)
    await asyncio.sleep(0.15)
    assert api.talk_calls == ["start"]  # no stop, no second start
    await c.aclose()
    assert api.talk_calls == ["start", "stop"]


async def test_no_renewal_during_stop_grace():
    """Leases are renewed only while audio plays (seen live: a renewal used to land inside the grace)."""
    api = FakeSO101API()
    c, _ = make(api, renew_interval_s=0.1, lease_s=0.5, stop_grace_s=0.25)
    c.on_playback(PLAY)
    await until(lambda: "start" in api.talk_calls)
    await asyncio.sleep(0.09)  # next renewal is due ~10 ms from now
    c.on_playback(DRAIN)
    n = len(api.talk_calls)
    await until(lambda: api.talk_calls[-1:] == ["stop"])
    assert api.talk_calls[n:] == ["stop"]  # nothing but the stop after the last sample
    await c.aclose()


async def test_barge_in_flush_stops_without_grace():
    api = FakeSO101API()
    c, _ = make(api, stop_grace_s=1.0)
    c.on_playback(PLAY)
    await until(lambda: "start" in api.talk_calls)
    t0 = time.monotonic()
    c.on_playback(FLUSH)
    await until(lambda: "stop" in api.talk_calls)
    assert time.monotonic() - t0 < 0.1
    await c.aclose()


async def test_force_off_while_start_in_flight_ends_off():
    api = FakeSO101API(latency_s=0.05)
    c, _ = make(api)
    c.on_playback(PLAY)
    await asyncio.sleep(0.01)  # start request in flight
    c.force_off("provider disconnected")
    await until(lambda: api.talk_calls == ["start", "stop"])
    assert not api.talk_active()
    await c.aclose()


async def test_robot_unavailable_is_reported_and_retried_slowly_then_recovers():
    api = FakeSO101API(down=True)
    c, statuses = make(api)
    c.on_playback(PLAY)
    await until(lambda: c.status.link is RobotLink.UNAVAILABLE)
    assert "disconnected" in c.status.detail
    await asyncio.sleep(0.2)
    assert c.stats.failures <= 3  # rate-limited (retry_interval_s = 0.15), not a busy loop
    api.down = False
    await until(lambda: "start" in api.talk_calls)
    assert c.status.link is RobotLink.OK
    c.on_playback(FLUSH)
    await until(lambda: "stop" in api.talk_calls)
    await c.aclose()


async def test_rejected_409_reported():
    api = FakeSO101API(reject_reason="Emergency Hold is latched")
    c, _ = make(api)
    c.on_playback(PLAY)
    await until(lambda: c.status.link is RobotLink.REJECTED)
    assert "Emergency Hold" in c.status.detail and not c.status.talk_on
    await c.aclose()


async def test_crash_leaves_gripper_animating_at_most_one_lease():
    """Simulated client crash: reconciler dies without sending stop; the robot's lease ends Talk Mode."""
    api = FakeSO101API()
    c, _ = make(api)
    c.on_playback(PLAY)
    await until(lambda: api.talk_active())
    c._task.cancel()  # crash: no stop is ever sent
    assert api.talk_active()
    await asyncio.sleep(0.35)  # > lease_s
    assert not api.talk_active()
    assert "stop" not in api.talk_calls


async def test_aclose_sends_best_effort_stop():
    api = FakeSO101API()
    c, _ = make(api)
    c.on_playback(PLAY)
    await until(lambda: api.talk_active())
    await c.aclose()
    assert api.talk_calls[-1] == "stop" and not api.talk_active()


async def test_disabled_robot_is_a_no_op():
    c = SpeechMotionCoordinator(None)
    c.start()
    c.on_playback(PLAY)
    assert c.status.link is RobotLink.DISABLED
    await c.aclose()

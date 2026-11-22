"""Choreography foundation (A22, A23): gestures follow the PLAYBACK timeline and die with their turn."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from so101_voice.audio.playback import PlaybackBuffer
from so101_voice.choreography.contracts import BehaviorAction, ChoreographyPlan, PoseAction, SpeakAction
from so101_voice.choreography.scheduler import ChoreographyScheduler, MockGestureExecutor, SegmentTag

PLAN = {
    "actions": [
        {"type": "speak", "text": "Wow, that was incredible!"},
        {"type": "behavior", "behavior_id": "celebration"},
        {"type": "speak", "text": "You should do that again!"},
        {"type": "pose", "pose_id": "watching"},
    ]
}


def test_contract_validation():
    plan = ChoreographyPlan.model_validate(PLAN)
    assert [type(a) for a in plan.actions] == [SpeakAction, BehaviorAction, SpeakAction, PoseAction]
    assert [s.text for s in plan.speech] == ["Wow, that was incredible!", "You should do that again!"]
    with pytest.raises(ValidationError):
        ChoreographyPlan.model_validate({"actions": [{"type": "behavior", "behavior_id": "x; rm -rf /"}]})
    with pytest.raises(ValidationError):
        ChoreographyPlan.model_validate({"actions": [{"type": "move_joint", "angle": 90}]})  # no raw joints
    with pytest.raises(ValidationError):
        ChoreographyPlan.model_validate({"actions": []})


async def play(
    buf: PlaybackBuffer, sched: ChoreographyScheduler, loop_time: list[float], seconds: float
) -> None:
    """Drain the buffer like a 20 ms device callback, forwarding markers to the scheduler."""
    for _ in range(int(seconds / 0.02)):
        buf.read(480)
        loop_time[0] += 0.02
        await asyncio.sleep(0)


async def test_gestures_fire_on_the_playback_timeline_not_at_generation_time():
    t = [0.0]
    ex = MockGestureExecutor(clock=lambda: t[0])
    sched = ChoreographyScheduler(ex)
    buf = PlaybackBuffer(24000, prebuffer_s=0.0, device_latency_s=0.0, clock=lambda: t[0])
    buf.add_marker_listener(lambda m: sched.on_marker(m.tag))
    segs = sched.prepare(1, ChoreographyPlan.model_validate(PLAN))
    # The LLM text and both segments' audio are all available at t=0 ...
    for seg in segs:
        buf.mark(seg.tag)
        buf.write(b"\x01\x00" * int(24000 * 0.5))  # 0.5 s of speech per segment
    await asyncio.sleep(0)
    assert ex.started == []  # ... but nothing fires until playback reaches the anchor
    await play(buf, sched, t, 0.4)
    assert ex.started == []
    await play(buf, sched, t, 0.2)  # second segment starts at 0.5 s
    assert [target for _, target, _ in ex.started] == ["celebration"]
    assert ex.started[0][0] == pytest.approx(0.5, abs=0.03)  # when segment 2's first sample played
    await play(buf, sched, t, 0.5)
    sched.on_drained(1)  # end of the last segment
    await asyncio.sleep(0)
    assert [target for _, target, _ in ex.started] == ["celebration", "watching"]
    await sched.aclose()


async def test_interrupted_generation_gestures_never_run_later():
    t = [0.0]
    ex = MockGestureExecutor(clock=lambda: t[0])
    sched = ChoreographyScheduler(ex)
    buf = PlaybackBuffer(24000, prebuffer_s=0.0, device_latency_s=0.0, clock=lambda: t[0])
    buf.add_marker_listener(lambda m: sched.on_marker(m.tag))
    segs = sched.prepare(7, ChoreographyPlan.model_validate(PLAN))
    for seg in segs:
        buf.mark(seg.tag)
        buf.write(b"\x01\x00" * int(24000 * 0.5))
    await play(buf, sched, t, 0.2)
    sched.cancel_generation(7)  # barge-in
    buf.flush("barge_in")
    sched.on_marker(SegmentTag(7, 1))  # a late marker for the cancelled turn
    sched.on_drained(7)
    await play(buf, sched, t, 1.0)
    await asyncio.sleep(0.01)
    assert ex.started == [] and sched.fired == []
    await sched.aclose()


async def test_new_gesture_supersedes_a_running_one_and_cancel_stops_it():
    ex = MockGestureExecutor(duration_s=0.5)
    sched = ChoreographyScheduler(ex)
    plan = ChoreographyPlan.model_validate(
        {
            "actions": [
                {"type": "behavior", "behavior_id": "wave", "anchor": "immediate"},
                {"type": "speak", "text": "Hi!"},
                {"type": "pose", "pose_id": "watching"},
            ]
        }
    )
    sched.prepare(1, plan)
    sched.on_marker(SegmentTag(1, 0))
    await asyncio.sleep(0.05)
    sched.on_drained(1)
    await asyncio.sleep(0.05)
    assert [k for _, _, k in ex.started] == ["gesture-1-0", "gesture-1-2"]
    assert ex.cancelled == ["gesture-1-0"]  # superseded
    sched.cancel_generation(1)
    await asyncio.sleep(0.05)
    assert ex.cancelled == ["gesture-1-0", "gesture-1-2"] and ex.finished == []
    await sched.aclose()


def test_scheduler_is_not_connected_to_the_robot():
    import so101_voice.choreography.scheduler as mod

    src = open(mod.__file__).read()
    assert "RobotAPIClient" not in src and "httpx" not in src

"""M2 Behavior Engine, pure layer (ADR-013): format, validation, library, trajectory, preview.

Acceptance coverage: M2-A03, A04, A06, A07, A08, A09, A10, A11, A12, A13, A14 (pure part), A21.
"""

from __future__ import annotations

import itertools
import json
import math
from pathlib import Path

import pytest

from so101_companion.behaviors.model import Origin, PlaybackSettings, Sample
from so101_companion.behaviors.plan import (
    MIN_JERK_PEAK,
    MIN_JERK_PEAK_ACCEL,
    CompatibilityError,
    SmoothTransition,
    compile_plan,
    pose_deviation,
)
from so101_companion.behaviors.preview import PreviewSession
from so101_companion.behaviors.recorder import BehaviorRecorder
from so101_companion.behaviors.repository import BehaviorRepository, new_behavior
from so101_companion.behaviors.validate import BehaviorFormatError, behavior_from_json, quality_report
from so101_companion.control.timeline import StallAwareClock
from so101_companion.domain.joints import ARM_JOINTS, JOINT_NAMES
from so101_companion.safety import policy

ZERO = dict.fromkeys(JOINT_NAMES, 0.0)
FP = "sha256:" + "ab" * 32
SCHEMA = Path(__file__).resolve().parents[2] / "planning/so101-milestone2-handoff/milestone-2/contracts"


def pose(**v: float) -> dict[str, float]:
    return {**ZERO, **v}


def real_samples(times: list[float], f=lambda t: pose(shoulder_pan=10 * t)) -> tuple[Sample, ...]:  # type: ignore[no-untyped-def]
    return tuple(Sample(t, actual=f(t)) for t in times)


def ramp(n: int = 41, dt: float = 0.05, origin: Origin = Origin.REAL_HAND_GUIDED):  # type: ignore[no-untyped-def]
    """2 s ramp: shoulder_pan 0 -> 20 deg, gripper 0 -> 40."""
    times = [round(i * dt, 6) for i in range(n)]
    poses = [pose(shoulder_pan=10.0 * t, gripper=20.0 * t) for t in times]
    if origin is Origin.REAL_HAND_GUIDED:
        samples = tuple(Sample(t, actual=p) for t, p in zip(times, poses, strict=True))
        return new_behavior("ramp", origin, samples, calibration_fingerprint=FP, calibration_robot_id="arm")
    samples = tuple(Sample(t, requested=p) for t, p in zip(times, poses, strict=True))
    return new_behavior("ramp", origin, samples)


# ------------------------------------------------------------------ validation (A03, A04)
@pytest.mark.parametrize(("gap_ms", "ok"), [(249, True), (250, True), (251, False), (350, False)])
def test_gap_threshold_is_250ms_inclusive(gap_ms: int, ok: bool) -> None:
    times = [0.0, 0.05, 0.05 + gap_ms / 1000.0, 0.10 + gap_ms / 1000.0]
    report = quality_report(real_samples(times), Origin.REAL_HAND_GUIDED)
    assert report.valid is ok
    if not ok:
        (gap,) = report.gaps
        assert gap.index == 2 and gap.t_start == pytest.approx(0.05)
        assert f"{gap_ms} ms gap" in report.problems[0]
        with pytest.raises(BehaviorFormatError, match="exceeds 250 ms"):
            new_behavior("x", Origin.REAL_HAND_GUIDED, real_samples(times), calibration_fingerprint=FP)


def test_invalid_samples_and_ordering_are_rejected() -> None:
    bad = (Sample(0.0, actual=pose()), Sample(0.05, actual=None), Sample(0.1, actual=pose()))
    assert any("missing 'actual'" in p for p in quality_report(bad, Origin.REAL_HAND_GUIDED).problems)
    back = real_samples([0.0, 0.1, 0.1])
    assert any("strictly increasing" in p for p in quality_report(back, Origin.REAL_HAND_GUIDED).problems)
    nan = (Sample(0.0, actual=pose()), Sample(0.05, actual=pose(elbow_flex=math.nan)))
    assert any("finite" in p for p in quality_report(nan, Origin.REAL_HAND_GUIDED).problems)
    seam = (Sample(0.0, actual=pose(wrist_roll=170.0)), Sample(0.05, actual=pose(wrist_roll=-170.0)))
    assert any("seam" in p for p in quality_report(seam, Origin.REAL_HAND_GUIDED).problems)


def test_hand_guided_samples_carry_no_invented_commands() -> None:
    b = ramp()
    assert all(s.actual is not None and s.requested is None and s.sent is None for s in b.samples)
    doc = b.to_json()
    assert all(s["requested"] is None and s["sent"] is None for s in doc["samples"])
    doc["samples"][3]["requested"] = dict(ZERO)
    with pytest.raises(BehaviorFormatError, match="must not contain requested/sent"):
        behavior_from_json(doc)


def test_keyframes_are_exempt_from_the_gap_rule_only() -> None:
    kf = tuple(Sample(t, requested=pose(elbow_flex=t)) for t in (0.0, 1.5, 4.0))
    assert quality_report(kf, Origin.SIM_KEYFRAMES).valid
    assert not quality_report(kf, Origin.SIM_SLIDERS).valid  # slider takes are recordings


def test_files_match_the_machine_readable_schema_shape() -> None:
    """Structural check against contracts/behavior-v1.schema.json (required keys, enums, units)."""
    schema = json.loads((SCHEMA / "behavior-v1.schema.json").read_text())
    doc = ramp().to_json()
    assert set(schema["required"]) <= set(doc)
    assert doc["origin"] in schema["properties"]["origin"]["enum"]
    units = {k: v["const"] for k, v in schema["properties"]["joint_units"]["properties"].items()}
    assert doc["joint_units"] == units
    sample_keys = set(schema["properties"]["samples"]["items"]["properties"])
    assert all(set(s) <= sample_keys for s in doc["samples"])


# ------------------------------------------------------------------ library (A06, A07)
def test_repository_round_trip_rename_keeps_id_and_history(tmp_path: Path) -> None:
    repo = BehaviorRepository(tmp_path / "behaviors")
    b = repo.create(ramp())
    assert (tmp_path / "behaviors" / f"{b.id}.json").is_file()
    assert repo.load(b.id) == b
    r = repo.rename(b, "wave", "hello gesture")
    assert r.id == b.id and r.revision == 2 and r.created_at_utc == b.created_at_utc
    assert r.updated_at_utc >= b.updated_at_utc and r.edits[-1].kind == "renamed"
    assert r.samples == b.samples  # source untouched
    with pytest.raises(BehaviorFormatError, match="changed on disk"):
        repo.rename(b, "stale edit", "")  # based on revision 1: refused, no lost update
    summaries, problems = repo.list()
    assert [(s.id, s.name, s.revision) for s in summaries] == [(b.id, "wave", 2)] and problems == []
    assert not list((tmp_path / "behaviors").glob(".*tmp"))  # atomic write leaves no temp file


def test_corrupt_unknown_and_mismatched_files_fail_with_actionable_errors(tmp_path: Path) -> None:
    repo = BehaviorRepository(tmp_path)
    good = repo.create(ramp())
    (tmp_path / "11111111-1111-4111-8111-111111111111.json").write_text("{broken")
    future = good.to_json() | {"schema_version": 2, "id": "22222222-2222-4222-8222-222222222222"}
    (tmp_path / f"{future['id']}.json").write_text(json.dumps(future))
    summaries, problems = repo.list()
    assert [s.id for s in summaries] == [good.id]
    text = " ".join(p.error for p in problems)
    assert "corrupt JSON" in text and "unsupported schema_version 2" in text
    with pytest.raises(CompatibilityError, match="another calibration"):
        compile_plan(good, good.settings, ZERO, bounds=None, calibration_fingerprint="sha256:other")
    doc = good.to_json() | {"calibration_fingerprint": None}
    with pytest.raises(BehaviorFormatError, match="calibration_fingerprint"):
        behavior_from_json(doc)


# ------------------------------------------------------------------ trajectory (A08–A13, A21)
def test_trim_interpolates_boundaries_and_keeps_source(tmp_path: Path) -> None:
    b = ramp()
    s = PlaybackSettings(trim_start_s=0.525, trim_end_s=1.5)
    plan = compile_plan(b, s, ZERO, bounds=None, calibration_fingerprint=None)
    assert plan.content.times[0] == 0.0 and plan.content.duration == pytest.approx(0.975)
    assert plan.content.poses[0]["shoulder_pan"] == pytest.approx(5.25)
    assert plan.content.poses[-1]["shoulder_pan"] == pytest.approx(15.0)
    repo = BehaviorRepository(tmp_path)
    saved = repo.set_settings(repo.create(b), s)
    assert saved.samples == b.samples and saved.settings == s
    with pytest.raises(BehaviorFormatError, match="trim"):
        repo.set_settings(saved, PlaybackSettings(trim_start_s=1.9, trim_end_s=1.8))


def test_linear_interpolation_at_and_between_samples() -> None:
    samples = (
        Sample(0.0, actual=pose(elbow_flex=0.0)),
        Sample(0.1, actual=pose(elbow_flex=10.0)),
        Sample(0.25, actual=pose(elbow_flex=-5.0)),
    )
    b = new_behavior("v", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=FP)
    c = compile_plan(b, b.settings, ZERO, bounds=None, calibration_fingerprint=None).content
    assert c.at(0.1)["elbow_flex"] == pytest.approx(10.0)
    assert c.at(0.05)["elbow_flex"] == pytest.approx(5.0)
    assert c.at(0.175)["elbow_flex"] == pytest.approx(2.5)
    assert c.at(0.25)["elbow_flex"] == pytest.approx(-5.0)


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_speed_scales_duration_not_the_source(speed: float) -> None:
    b = ramp()
    plan = compile_plan(b, PlaybackSettings(speed=speed), ZERO, bounds=None, calibration_fingerprint=None)
    assert plan.content.duration == pytest.approx(2.0 / speed)
    assert plan.content.at(1.0 / speed)["shoulder_pan"] == pytest.approx(10.0)
    assert b.samples[-1].t == 2.0
    with pytest.raises(BehaviorFormatError, match="speed"):
        compile_plan(b, PlaybackSettings(speed=2.5), ZERO, bounds=None, calibration_fingerprint=None)


def limit_duration(dist: dict[str, float]) -> float:
    """Expected D8 duration: the slowest joint's speed- or acceleration-bound min-jerk time."""
    need = max(
        max(
            MIN_JERK_PEAK * abs(d) / policy.TRANSITION_MAX_SPEED[n],
            math.sqrt(MIN_JERK_PEAK_ACCEL * abs(d) / policy.TRANSITION_MAX_ACCEL[n]),
        )
        for n, d in dist.items()
    )
    return max(policy.TRANSITION_MIN_DURATION_S, need)


def assert_within_transition_limits(seg: SmoothTransition, hz: float = 1000.0) -> None:
    """Numerically sampled speed and acceleration of a transition never exceed the per-joint limits."""
    dt = 1.0 / hz
    pts = [seg.at(k * dt) for k in range(int(seg.duration * hz) + 2)]
    for n in JOINT_NAMES:
        v = [(b[n] - a[n]) / dt for a, b in itertools.pairwise(pts)]
        acc = [(v2 - v1) / dt for v1, v2 in itertools.pairwise(v)]
        assert max(map(abs, v)) <= policy.TRANSITION_MAX_SPEED[n] * 1.001 + 1e-6, n
        assert max(map(abs, acc)) <= policy.TRANSITION_MAX_ACCEL[n] * 1.01 + 1e-3, n
        assert abs(v[0]) < 0.05 * policy.TRANSITION_MAX_SPEED[n] + 1e-6  # eased in
        assert abs(v[-1]) < 0.05 * policy.TRANSITION_MAX_SPEED[n] + 1e-6  # eased out


def test_finite_and_infinite_loops_with_smooth_bridges() -> None:
    b = ramp()
    plan = compile_plan(b, PlaybackSettings(repeat_count=3), ZERO, bounds=None, calibration_fingerprint=None)
    e, c, br = plan.entry.duration, plan.content.duration, plan.bridge.duration
    assert br == pytest.approx(limit_duration({"shoulder_pan": 20.0, "gripper": 40.0}))  # short, D8
    assert br < 0.6  # was 2.5 s at the old 30 °/s cap
    assert_within_transition_limits(plan.bridge)
    assert plan.total_duration == pytest.approx(e + 3 * c + 2 * br)
    seen = [plan.at(t / 100.0) for t in range(int(plan.total_duration * 100) + 5)]
    assert max(p.repetition for p in seen) == 3 and seen[-1].finished
    assert e == 0.0  # the arm (ZERO) already is at the first pose: no entry move (D8)
    assert [k for k, _ in itertools.groupby(p.segment for p in seen)] == [
        "content", "bridge", "content", "bridge", "content", "done",
    ]  # fmt: skip
    for a, b2 in itertools.pairwise(seen):  # continuous at 100 Hz: no step beyond a joint's limit
        for n in JOINT_NAMES:
            assert abs(b2.pose[n] - a.pose[n]) <= policy.TRANSITION_MAX_SPEED[n] * 0.01 + 1e-6
    forever = compile_plan(
        b, PlaybackSettings(repeat_count=None), ZERO, bounds=None, calibration_fingerprint=None
    )
    assert forever.total_duration is None and not forever.at(1e4).finished
    assert forever.at(e + 50 * (c + br) + 0.1).repetition == 51


def test_transitions_are_short_and_within_per_joint_speed_and_accel_limits() -> None:
    """D8: the shortest min-jerk move that respects every joint's speed AND acceleration limit."""
    tiny = SmoothTransition.plan(ZERO, pose(elbow_flex=1.5))
    assert tiny.duration == policy.TRANSITION_MIN_DURATION_S  # still a ramp, never a step
    far = SmoothTransition.plan(ZERO, pose(elbow_flex=90.0, gripper=40.0))
    assert far.duration == pytest.approx(limit_duration({"elbow_flex": 90.0, "gripper": 40.0}))
    assert far.duration < 2.0  # was 5.6 s at the old 30 °/s cap
    assert far.peak_speeds()["elbow_flex"] <= policy.TRANSITION_MAX_SPEED["elbow_flex"] + 1e-9
    assert_within_transition_limits(far)
    short = SmoothTransition.plan(ZERO, pose(wrist_flex=10.0))  # short moves are acceleration-bound
    assert short.peak_accels()["wrist_flex"] == pytest.approx(policy.TRANSITION_MAX_ACCEL["wrist_flex"])
    assert short.peak_speeds()["wrist_flex"] < policy.TRANSITION_MAX_SPEED["wrist_flex"]


@pytest.mark.parametrize(
    "goal",
    [
        # every joint end to end across its calibrated span (worst case), and mixed directions
        {"shoulder_pan": 104.0, "shoulder_lift": 99.0, "elbow_flex": 92.0, "wrist_flex": 97.0,
         "wrist_roll": 163.0, "gripper": 100.0},
        {"shoulder_pan": -104.0, "shoulder_lift": -99.0, "elbow_flex": 92.0, "wrist_flex": -97.0,
         "wrist_roll": -163.0, "gripper": 0.0},
    ],
)  # fmt: skip
def test_large_pose_differences_stay_within_limits(goal: dict[str, float]) -> None:
    start = {n: -v for n, v in goal.items()} | {"gripper": 100.0 - goal["gripper"]}
    seg = SmoothTransition.plan(start, goal)
    assert seg.duration == pytest.approx(limit_duration({n: goal[n] - start[n] for n in JOINT_NAMES}))
    assert seg.duration < 4.5  # whole-range move in a few seconds, not tens of seconds
    assert_within_transition_limits(seg)
    # monotonic per joint (no overshoot beyond the goal, so no new bound or seam can be reached)
    path = [seg.at(k * seg.duration / 400) for k in range(401)]
    for n in JOINT_NAMES:
        lo, hi = sorted((start[n], goal[n]))
        assert all(lo - 1e-9 <= p[n] <= hi + 1e-9 for p in path), n


def test_matching_end_and_start_loop_immediately_without_a_bridge() -> None:
    """D8: if the gesture ends where it starts (within 1° / 2 units), repetitions follow back to back."""
    times = [round(i * 0.05, 6) for i in range(41)]
    wave = [pose(elbow_flex=30.0 * math.sin(math.pi * t), gripper=0.8 * t) for t in times]  # 0 -> 30 -> 0
    samples = tuple(Sample(t, actual=p) for t, p in zip(times, wave, strict=True))
    b = new_behavior("wave", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=FP)
    plan = compile_plan(b, PlaybackSettings(repeat_count=3), ZERO, bounds=None, calibration_fingerprint=None)
    assert plan.bridge.duration == 0.0 and plan.summary()["seamless_loop"] is True
    assert plan.entry.duration == 0.0  # already at the first pose: no entry move either
    assert plan.total_duration == pytest.approx(3 * plan.content.duration)
    seen = [plan.at(k * 0.05) for k in range(int(plan.total_duration / 0.05) + 2)]
    assert "bridge" not in {p.segment for p in seen}
    content_step = max(abs(math.pi * 30.0 * 0.05), 0.04)
    for a, c in itertools.pairwise(seen):  # the seam is no bigger than what the gesture does per tick
        assert abs(c.pose["elbow_flex"] - a.pose["elbow_flex"]) <= content_step + 1e-6
        assert abs(c.pose["gripper"] - a.pose["gripper"]) <= policy.LOOP_SEAMLESS_TOL_GRIPPER + 1e-6
    almost = compile_plan(  # just outside the tolerance -> a short bridge again
        new_behavior(
            "almost",
            Origin.REAL_HAND_GUIDED,
            tuple(Sample(round(i * 0.05, 6), actual=pose(elbow_flex=0.075 * i)) for i in range(21)),
            calibration_fingerprint=FP,
        ),
        PlaybackSettings(repeat_count=2),
        ZERO,
        bounds=None,
        calibration_fingerprint=None,
    )
    assert almost.bridge.duration == policy.TRANSITION_MIN_DURATION_S


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_faster_transitions_never_change_the_recorded_timing(speed: float) -> None:
    b = ramp()
    plan = compile_plan(b, PlaybackSettings(speed=speed, repeat_count=4), pose(elbow_flex=60.0),
                        bounds=None, calibration_fingerprint=None)  # fmt: skip
    assert plan.content.duration == pytest.approx(2.0 / speed)
    e = plan.entry.duration
    for k in range(4):  # every repetition plays the recorded series at its recorded time ÷ speed
        t0 = e + k * (plan.content.duration + plan.bridge.duration)
        assert plan.at(t0 + 1.0 / speed).pose["shoulder_pan"] == pytest.approx(10.0)


def test_recorded_content_is_not_speed_limited() -> None:
    fast = (Sample(0.0, actual=pose(elbow_flex=0.0)), Sample(0.05, actual=pose(elbow_flex=20.0)))
    b = new_behavior("flick", Origin.REAL_HAND_GUIDED, fast, calibration_fingerprint=FP)
    plan = compile_plan(b, PlaybackSettings(speed=2.0), ZERO, bounds=None, calibration_fingerprint=None)
    assert plan.content.duration == pytest.approx(0.025)  # 800°/s requested: NOT limited (D2)


def test_clamp_creates_audited_effective_copy_only() -> None:
    samples = real_samples([0.0, 0.05, 0.1], lambda t: pose(wrist_flex=-80.0 - 400.0 * t))
    b = new_behavior("deep", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=FP)
    bounds = {n: (-97.0, 97.0) for n in ARM_JOINTS} | {"gripper": (0.0, 100.0)}
    plan = compile_plan(b, b.settings, ZERO, bounds=bounds, calibration_fingerprint=FP)
    assert [p["wrist_flex"] for p in plan.content.poses] == [-80.0, -97.0, -97.0]
    assert [(c.index, c.before, c.after) for c in plan.audit.changes] == [
        (1, -100.0, -97.0),
        (2, -120.0, -97.0),
    ]
    assert "wrist_flex: 2 samples clamped" in plan.audit.describe()
    assert b.samples[2].actual["wrist_flex"] == -120.0  # raw capture unchanged
    provisional = compile_plan(b, b.settings, ZERO, bounds=None, calibration_fingerprint=None)
    assert not provisional.audit.checked and provisional.content.poses[2]["wrist_flex"] == -120.0


def test_wrist_never_takes_the_seam_or_a_360_path() -> None:
    """A21: logical-frame interpolation; −150 -> +150 goes through 0 (the real continuous arc)."""
    t = SmoothTransition.plan(pose(wrist_roll=-150.0), pose(wrist_roll=150.0))
    path = [t.at(k * t.duration / 200)["wrist_roll"] for k in range(201)]
    assert all(-150.0 - 1e-9 <= w <= 150.0 + 1e-9 for w in path)
    assert any(abs(w) < 2.0 for w in path)
    samples = real_samples([0.0, 0.05], lambda tt: pose(wrist_roll=160.0 - 40.0 * tt))
    b = new_behavior("w", Origin.REAL_HAND_GUIDED, samples, calibration_fingerprint=FP)
    plan = compile_plan(b, b.settings, pose(wrist_roll=-160.0), bounds=None, calibration_fingerprint=None)
    assert plan.entry.duration == pytest.approx(limit_duration({"wrist_roll": 320.0}))  # long way, no wrap
    path = [plan.entry.at(k * plan.entry.duration / 200)["wrist_roll"] for k in range(201)]
    assert all(-160.0 - 1e-9 <= w <= 160.0 + 1e-9 for w in path) and any(abs(w) < 3.0 for w in path)


def test_preview_token_tracks_every_input() -> None:
    b = ramp()
    base = compile_plan(b, b.settings, ZERO, bounds=None, calibration_fingerprint=FP)
    assert (
        base.content_token
        == compile_plan(
            b, b.settings, pose(elbow_flex=1), bounds=None, calibration_fingerprint=FP
        ).content_token
    )
    for other in (
        compile_plan(b, PlaybackSettings(speed=1.5), ZERO, bounds=None, calibration_fingerprint=FP),
        compile_plan(b.with_changes(revision=2), b.settings, ZERO, bounds=None, calibration_fingerprint=FP),
        compile_plan(
            b, b.settings, ZERO, bounds={n: (-90.0, 90.0) for n in JOINT_NAMES}, calibration_fingerprint=FP
        ),
    ):
        assert other.content_token != base.content_token
    assert pose_deviation(ZERO, pose(elbow_flex=3.0)) == {}
    assert pose_deviation(ZERO, pose(elbow_flex=3.1, gripper=5.1)) == {"elbow_flex": 3.1, "gripper": 5.1}


def test_preview_session_shows_bridge_and_rest_and_authorizes_only_with_hardware() -> None:
    b = ramp()
    bounds = {n: (-100.0, 100.0) for n in ARM_JOINTS} | {"gripper": (0.0, 100.0)}
    plan = compile_plan(  # start away from the first pose so there is an entry transition to show
        b,
        PlaybackSettings(repeat_count=None),
        pose(elbow_flex=20.0),
        bounds=bounds,
        calibration_fingerprint=FP,
    )
    session = PreviewSession(plan, rest_goal=pose(shoulder_lift=-90.0), hardware_context=True)
    labels = {session.pose_at(k / 20)[1].split(" (")[0] for k in range(int(session.duration * 20) + 1)}
    assert {"entry transition", "behavior", "loop bridge after repetition 1", "return to rest"} <= labels
    assert session.approval() is None  # not finished yet
    session.step(session.duration + 0.01)
    approval = session.approval()
    assert approval is not None and approval.content_token == plan.content_token and approval.bounds == bounds
    assert session.approval().nonce != approval.nonce  # type: ignore[union-attr]
    sim = PreviewSession(
        compile_plan(b, b.settings, ZERO, bounds=None, calibration_fingerprint=None), None, False
    )
    sim.step(sim.duration + 1)
    assert sim.completed and sim.approval() is None  # disconnected preview never authorizes


# ------------------------------------------------------------------ recorder + clock
def test_recorder_uses_receipt_timestamps_and_ignores_duplicates() -> None:
    r = BehaviorRecorder()
    r.start(Origin.REAL_HAND_GUIDED, 100.0, FP, "arm")
    r.add(pose(), 100.02, seq=1)
    r.add(pose(elbow_flex=1), 100.02, seq=1)  # same frame again: ignored
    r.add(pose(elbow_flex=2), 100.07, seq=2)
    r.add(pose(elbow_flex=3), 100.42, seq=3)  # 350 ms bus stall
    rec = r.finish()
    assert [s.t for s in rec.samples] == pytest.approx([0.0, 0.05, 0.40])
    assert not rec.valid and "350 ms gap" in rec.problems()[0]
    assert all(s.requested is None and s.sent is None for s in rec.samples)


def test_stall_aware_clock_never_jumps() -> None:
    c = StallAwareClock(0.05, started_at=10.0)
    assert c.advance(10.05) == pytest.approx(0.05)
    assert c.advance(10.45) == pytest.approx(0.10)  # 400 ms stall: one period of progress only
    assert c.delay_s == pytest.approx(0.35)
    assert c.advance(10.47) == pytest.approx(0.12)  # short ticks count fully
    assert c.advance(10.47) == pytest.approx(0.12)  # no time, no progress

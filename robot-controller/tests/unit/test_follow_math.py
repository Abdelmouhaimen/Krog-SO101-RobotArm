"""ADR-019 pure math: circular statistics and classification, the pan solver (reachability, residuals,
hysteresis), the pan tracker (continuity, limits, latest value), the handoff and the Talk generator."""

from __future__ import annotations

import itertools
import math
import statistics

import pytest

from so101_companion.behaviors.direction import (
    MIXED,
    NORMAL,
    REVERSED,
    UNDEFINED,
    circular_median,
    classify,
    time_weights,
    wrap180,
)
from so101_companion.behaviors.plan import MIN_JERK_PEAK, MIN_JERK_PEAK_ACCEL
from so101_companion.control.overlays import (
    TALK_MAX_RHYTHM_HZ,
    TALK_PRESETS,
    Handoff,
    PanTracker,
    TalkGenerator,
    min_jerk_duration,
    solve_pan,
)
from so101_companion.safety import policy

LO, HI = -104.9, 104.9  # the owner's calibrated pan travel


# ------------------------------------------------------------------ circular statistics
def test_circular_median_handles_wraparound_where_arithmetic_fails() -> None:
    angles = [179.9, -179.9, 179.8, -179.7, 179.95]
    assert abs(wrap180(circular_median(angles) - 180.0)) < 0.2
    assert abs(statistics.mean(angles)) < 80  # an arithmetic mean would call this "forward"
    assert circular_median([10.0, 20.0, 350.0 - 360.0]) == pytest.approx(10.0)


def test_weights_follow_sample_duration() -> None:
    # 2 long samples at 30° outweigh 5 tightly packed samples at -30°
    times = [0.0, 1.0, 2.0, 2.01, 2.02, 2.03, 2.04, 2.05]
    angles = [30.0, 30.0, 30.0, -30.0, -30.0, -30.0, -30.0, -30.0]
    w = time_weights(times)
    assert circular_median(angles, w) == pytest.approx(30.0)
    assert circular_median(angles) == pytest.approx(-30.0)  # unweighted would pick the dense burst


def test_classification_normal_reversed_mixed_undefined() -> None:
    n = 100
    w = [1.0] * n
    rev = classify([179.5 + 0.5 * (i % 3) for i in range(n)], [0.25] * n, w)
    assert rev.classification == REVERSED and rev.compatible and abs(wrap180(rev.psi_deg - 180)) < 1
    norm = classify([0.3] * n, [0.3] * n, w)
    assert norm.classification == NORMAL and norm.psi_deg == pytest.approx(0.3)
    crossing = classify([180.0] * 80 + [0.0] * 20, [0.2] * n, w)  # 20% on the other side
    assert crossing.classification == MIXED and not crossing.compatible and "crosses" in crossing.reason
    slight = classify([180.0] * 97 + [0.0] * 3, [0.2] * n, w)  # 3% ≤ 5%: still one side
    assert slight.classification == REVERSED
    near_axis = classify([0.0] * n, [0.03] * 50 + [0.2] * 50, w)  # half the time within 60 mm
    assert near_axis.classification == UNDEFINED and not near_axis.compatible
    assert classify([], [], []).classification == UNDEFINED


# ------------------------------------------------------------------ solver
@pytest.mark.parametrize(
    ("theta", "psi", "pan", "reach"),
    [
        (0.0, 0.0, 0.0, "REACHABLE"),
        (23.0, 0.0, 23.0, "REACHABLE"),
        (90.0, 0.0, 90.0, "REACHABLE"),
        (180.0, 180.0, 0.0, "REACHABLE"),
        (120.0, 180.0, -60.0, "REACHABLE"),
        (0.0, 180.0, HI, "UNREACHABLE"),  # review example: no equivalent pan, residual 75°
    ],
)
def test_review_examples(theta: float, psi: float, pan: float, reach: str) -> None:
    sol = solve_pan(theta, psi, 0.0, LO, HI)
    assert sol.reach == reach
    if reach == "UNREACHABLE":  # an exact tie between the two bounds: either one, same 75.1° residual
        assert abs(sol.pan_target) == pytest.approx(HI) and abs(sol.residual_deg) == pytest.approx(
            75.1, abs=0.1
        )
    else:
        assert sol.pan_target == pytest.approx(pan)


def test_at_most_one_candidate_and_residual_rules() -> None:
    for theta, psi, front in itertools.product(range(-180, 181, 7), (0.0, 180.0, 3.0), (0.0, 105.0, -40.0)):
        sol = solve_pan(float(theta), psi, front, LO, HI)
        achieved = wrap180(sol.pan_target + psi - front)
        assert abs(wrap180(theta - achieved - sol.residual_deg)) < 1e-6
        assert LO - 1e-9 <= sol.pan_target <= HI + 1e-9
        if sol.reach == "REACHABLE":
            assert abs(sol.residual_deg) < 1e-6
        else:
            assert abs(sol.residual_deg) > 0  # never reported as tracking when it cannot face the target
            assert (sol.reach == "LIMITED") == (abs(sol.residual_deg) <= 20.0)


def test_front_reference_shifts_the_reachable_sector() -> None:
    # the owner's setup: working direction 105° clockwise of pan center, reversed content (review §2)
    sol = solve_pan(0.0, 180.0, 105.0, LO, HI)
    assert sol.reach == "REACHABLE" and sol.pan_target == pytest.approx(-75.0)
    lo_theta, hi_theta = sol.reachable_theta
    assert lo_theta == pytest.approx(-29.9) and hi_theta == pytest.approx(179.9)


def test_bound_hysteresis_prevents_swinging_between_bounds() -> None:
    # target opposite the forbidden sector: both bounds are almost equally bad
    a = solve_pan(-1.0, 180.0, 0.0, LO, HI)
    b = solve_pan(1.0, 180.0, 0.0, LO, HI, current_bound=a.bound)
    assert b.bound == a.bound  # no 210° swing for a 2° change
    far = solve_pan(60.0, 180.0, 0.0, LO, HI, current_bound="high")
    assert far.bound == "low" and far.reach == "LIMITED"  # a clearly better bound (15° vs 135° off) is taken


# ------------------------------------------------------------------ tracker / handoff
def test_tracker_is_continuous_bounded_and_settles_without_oscillation() -> None:
    v_max, a_max, dt = 72.0, 225.0, 0.05
    t = PanTracker(v_max, a_max)
    q, v = [t.q], [t.v]
    for _ in range(120):
        t.step(90.0, dt)
        q.append(t.q)
        v.append(t.v)
    assert q[-1] == pytest.approx(90.0)
    assert max(q) <= 90.0 + 0.5 * a_max * dt * dt  # discretization: at most a·dt²/2 past, then settles
    assert sum(1 for a, b in itertools.pairwise(v) if a * b < 0) <= 1  # no oscillation
    assert all(abs(b - a) <= a_max * dt + 1e-9 for a, b in itertools.pairwise(v))
    assert max(abs(x) for x in v) <= v_max + 1e-9
    # latest target: a new target mid-flight bends the path without a position jump
    t2 = PanTracker(v_max, a_max)
    for _ in range(10):
        t2.step(90.0, dt)
    before = t2.q
    t2.step(-30.0, dt)
    assert abs(t2.q - before) <= v_max * dt + 1e-9


def test_handoff_returns_smoothly_to_a_moving_base() -> None:
    h = Handoff.plan(40.0, 72.0, 225.0)
    base = [10.0 + 0.2 * i for i in range(200)]
    out = [h.step(b, 0.05) for b in base]
    assert out[0] == pytest.approx(base[0] + 40.0, abs=0.5) and out[-1] == pytest.approx(base[-1])
    assert all(abs(b - a) < 72.0 * 0.05 * 1.2 for a, b in itertools.pairwise(out))


# ------------------------------------------------------------------ Talk generator
def _talk_run(name: str, seed: int, seconds: float = 30.0, dt: float = 0.005) -> list[float]:
    g = TalkGenerator(TALK_PRESETS[name], 3.0, 0.0, 100.0, seed=seed)
    return [g.step(dt) for _ in range(int(seconds / dt))]


def test_talk_is_reproducible_with_a_seed() -> None:
    assert _talk_run("normal", 7, 5.0) == _talk_run("normal", 7, 5.0)
    assert _talk_run("normal", 7, 5.0) != _talk_run("normal", 8, 5.0)


@pytest.mark.parametrize("name", ["gentle", "normal", "excited"])
def test_talk_stays_inside_the_verified_gripper_envelope(name: str) -> None:
    p = TALK_PRESETS[name]
    dt = 0.005
    g = _talk_run(name, 3, 40.0, dt)
    v = [(b - a) / dt for a, b in itertools.pairwise(g)]
    acc = [(b - a) / dt for a, b in itertools.pairwise(v)]
    assert min(g) >= 0.0 and max(g) <= 3.0 + p.amplitude + 1e-6
    assert max(abs(x) for x in v) <= p.v_max * 1.02
    assert max(abs(x) for x in acc) <= p.a_max * 1.05
    assert (
        p.v_max <= policy.TRANSITION_MAX_SPEED["gripper"]
        and p.a_max <= policy.TRANSITION_MAX_ACCEL["gripper"]
    )
    # rhythm: opening peaks per second never above ~1.5 Hz (owner decision D9)
    peaks = sum(1 for a, b, c in zip(g, g[1:], g[2:], strict=False) if b > a and b >= c and b > 3.0 + 1.0)
    assert peaks / 40.0 <= TALK_MAX_RHYTHM_HZ + 0.05
    # varied, not identical oscillations; mostly small openings
    assert statistics.pstdev(g) > 0.5


def test_talk_segments_never_change_per_tick() -> None:
    g = TalkGenerator(TALK_PRESETS["excited"], 3.0, 0.0, 100.0, seed=1)
    for _ in range(400):
        g.step(0.05)
    assert g.segments < 400 * 0.05 / TALK_PRESETS["excited"].segment_min_s + 2  # ≥ 0.34 s per segment


def test_min_jerk_duration_matches_the_existing_transition_math() -> None:
    d = 20.0
    t = min_jerk_duration(d, 100.0, 500.0)
    assert MIN_JERK_PEAK * d / t <= 100.0 + 1e-9 and MIN_JERK_PEAK_ACCEL * d / t**2 <= 500.0 + 1e-6
    assert math.isclose(max(MIN_JERK_PEAK * d / 100.0, math.sqrt(MIN_JERK_PEAK_ACCEL * d / 500.0)), t)


def test_unreachable_prefers_the_nearer_bound_on_a_near_tie() -> None:
    """Real-arm finding (2026-11-07): reversed rest pose, target 0, pan at -72.8: both bounds are ~75° off;
    the solver must take the NEAR bound (-104.9, a 32° turn), not swing 178° to +104.9."""
    psi = 180.08
    sol = solve_pan(0.0, psi, 0.0, LO, HI, current_pan=-72.8)
    assert sol.bound == "low" and sol.pan_target == pytest.approx(LO) and sol.reach == "UNREACHABLE"
    sol = solve_pan(0.0, psi, 0.0, LO, HI, current_pan=72.8)
    assert sol.bound == "high"
    clear = solve_pan(60.0, 180.0, 0.0, LO, HI, current_pan=80.0)  # the far bound is MUCH better: take it
    assert clear.bound == "low"

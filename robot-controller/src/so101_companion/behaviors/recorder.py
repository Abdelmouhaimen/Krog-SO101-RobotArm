"""Behavior recorder (M2-REQ-02..05, ADR-013). Pure: the controller feeds it; it never commands anything.

* ``real_hand_guided``: fed with every fresh MEASURED frame (its own monotonic receipt timestamp) while
  the arm is in TORQUE_OFF with torque confirmed OFF. Only ``actual`` is stored; ``requested``/``sent``
  stay null (nothing is commanded during hand guidance, so nothing is invented).
* ``sim_sliders``: fed with the authored slider target (``requested``) at each SIM_ONLY tick.
* A take is aborted (and can never be saved) if its preconditions break mid-way; it is reported
  invalid if any adjacent gap exceeds 250 ms. Re-recording is always possible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from so101_companion.behaviors.model import Origin, Sample
from so101_companion.behaviors.validate import QualityReport, quality_report
from so101_companion.domain.joints import JOINT_NAMES
from so101_companion.safety import policy


@dataclass(frozen=True)
class Recording:
    origin: Origin
    samples: tuple[Sample, ...]
    report: QualityReport
    aborted: str | None  # reason, if the take was aborted
    calibration_fingerprint: str | None
    calibration_robot_id: str | None
    note: str = ""

    @property
    def valid(self) -> bool:
        return self.aborted is None and self.report.valid

    def problems(self) -> list[str]:
        return ([f"aborted: {self.aborted}"] if self.aborted else []) + list(self.report.problems)


@dataclass
class BehaviorRecorder:
    origin: Origin | None = None
    started_at: float | None = None
    fingerprint: str | None = None
    robot_id: str | None = None
    _samples: list[Sample] = field(default_factory=list)
    _t0: float | None = None
    _last_seq: int | None = None
    _max_gap: float = 0.0
    _first_bad_gap: str | None = None

    @property
    def active(self) -> bool:
        return self.origin is not None

    def start(
        self, origin: Origin, now: float, fingerprint: str | None = None, robot_id: str | None = None
    ) -> None:
        if self.active:
            raise RuntimeError("a recording is already active")
        self.origin, self.started_at = origin, now
        self.fingerprint, self.robot_id = fingerprint, robot_id
        self._samples, self._t0, self._last_seq = [], None, None
        self._max_gap, self._first_bad_gap = 0.0, None

    def add(self, values: Mapping[str, float], t_mono: float, seq: int | None = None) -> None:
        """One observation (real: measured frame; sim: authored target). Duplicate frames are ignored."""
        if self.origin is None:
            return
        if seq is not None and seq == self._last_seq:
            return
        self._last_seq = seq
        if self._t0 is None:
            self._t0 = t_mono
        t = t_mono - self._t0
        if self._samples:
            gap = t - self._samples[-1].t
            if gap <= 0:
                return  # not newer than the previous sample: never re-ordered or invented
            self._max_gap = max(self._max_gap, gap)
            if gap > policy.RECORDING_MAX_GAP_S and self._first_bad_gap is None:
                self._first_bad_gap = f"{gap * 1000:.0f} ms gap at t={self._samples[-1].t:.2f}s"
        pose = {n: float(values[n]) for n in JOINT_NAMES}
        if self.origin is Origin.REAL_HAND_GUIDED:
            self._samples.append(Sample(t, actual=pose))
        else:
            self._samples.append(Sample(t, requested=pose))

    def elapsed(self, now: float) -> float:
        return 0.0 if self.started_at is None else max(0.0, now - self.started_at)

    def status(self, now: float) -> dict[str, Any]:
        return {
            "state": "recording",
            "origin": None if self.origin is None else self.origin.value,
            "elapsed_s": round(self.elapsed(now), 2),
            "samples": len(self._samples),
            "max_gap_ms": round(self._max_gap * 1000.0, 1),
            "invalid_so_far": self._first_bad_gap,
        }

    def finish(self, aborted: str | None = None, note: str = "") -> Recording:
        assert self.origin is not None
        samples = tuple(self._samples)
        rec = Recording(
            self.origin,
            samples,
            quality_report(samples, self.origin),
            aborted,
            self.fingerprint,
            self.robot_id,
            note,
        )
        self.origin = None
        self.started_at = None
        self._samples = []
        return rec

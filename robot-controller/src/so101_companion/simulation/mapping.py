"""Logical joint units <-> official MuJoCo SO-101 joint coordinates (pure functions, no MuJoCo import).

Arm joints (ADR-006, PROVISIONAL until visually checked on hardware):
    model_angle_deg = sign * logical_deg + offset_deg ;  model_rad = radians(model_angle_deg)
Default sign = +1, offset = 0 for every joint. Justification: LeRobot DEGREES puts 0 at the middle of
each joint's calibrated range ((raw - mid) * 360/4095) and the SO101 ``so101_new_calib.xml`` model
puts each joint's virtual zero at the middle of its range (SO-ARM100 Simulation/SO101/README.md,
"New Calibration"). Rotation direction (sign) cannot be verified without the physical arm.

Gripper (PROVISIONAL): linear map of LeRobot RANGE_0_100 onto the official joint range. With
``logical_gripper_0_maps_to = "model_min"`` 0 -> jnt_range min (-10°, jaws closed in the official
model, verified by offscreen render) and 100 -> jnt_range max (+100°, wide open). LeRobot documents
0 = closed / 100 = open; the physical direction of a given calibration is a Gate B check.

Values outside the model range are clamped FOR DISPLAY ONLY and reported; the model files are never
modified. Exception (found during hardware commissioning): full-turn joints (wrist_roll) are shown at
their true wrapped angle even inside the ~40° gap of the official jnt_range, because the real joint
turns fully and jnt_range only acts in dynamics, which this kinematic preview never runs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from so101_companion.config import SimulationSection
from so101_companion.domain.joints import ARM_JOINTS, GRIPPER, JOINT_NAMES
from so101_companion.safety import policy
from so101_companion.safety.bounds import LABEL_SIM, JointBounds, build_bounds, restrict


@dataclass(frozen=True)
class JointMapping:
    model_range_rad: Mapping[str, tuple[float, float]]
    sign: Mapping[str, float]
    offset_deg: Mapping[str, float]
    gripper_0_maps_to: str = "model_min"
    # Full-turn joints (LeRobot calibrates wrist_roll over the whole 0..4095 encoder turn): the
    # displayed model angle is wrapped by ±360° into the official range when possible.
    full_turn: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if set(self.model_range_rad) != set(JOINT_NAMES):
            raise ValueError("model ranges must cover the six joints")
        for n in ARM_JOINTS:
            if self.sign[n] not in (1.0, -1.0):
                raise ValueError(f"sign[{n}] must be ±1")
        if self.gripper_0_maps_to not in ("model_min", "model_max"):
            raise ValueError("gripper_0_maps_to must be model_min or model_max")
        if not set(self.full_turn) <= set(ARM_JOINTS):
            raise ValueError("full_turn joints must be arm joints")

    def with_changes(self, **changes: object) -> JointMapping:
        """Copy with replaced fields (used by the live display-mapping panel)."""
        from dataclasses import replace

        return replace(self, **changes)  # type: ignore[arg-type]

    def _wrap_into_range(self, name: str, deg: float) -> float:
        lo, hi = (math.degrees(v) for v in self.model_range_rad[name])
        candidates = [deg + 360.0 * k for k in (-2, -1, 0, 1, 2)]
        inside = [c for c in candidates if lo <= c <= hi]
        if inside:
            return min(inside, key=lambda c: abs(c - deg))

        def gap(c: float) -> float:
            return lo - c if c < lo else c - hi

        return min(candidates, key=gap)

    # -- forward ---------------------------------------------------------------------------
    def logical_to_model_unclamped(self, name: str, value: float) -> float:
        if name == GRIPPER:
            lo, hi = self.model_range_rad[GRIPPER]
            frac = value / 100.0
            if self.gripper_0_maps_to == "model_max":
                frac = 1.0 - frac
            return lo + frac * (hi - lo)
        deg = self.sign[name] * value + self.offset_deg[name]
        if name in self.full_turn:
            deg = self._wrap_into_range(name, deg)
            # Still outside (the model's ~40° gap): show the true angle, wrapped to (-180, 180].
            lo, hi = (math.degrees(v) for v in self.model_range_rad[name])
            if not lo <= deg <= hi:
                deg = -((-deg + 180.0) % 360.0 - 180.0)
        return math.radians(deg)

    def logical_to_model(self, values: Mapping[str, float]) -> tuple[dict[str, float], list[str]]:
        """Return (model radians clamped into the official ranges, display_clamped reasons)."""
        out: dict[str, float] = {}
        reasons: list[str] = []
        for name in JOINT_NAMES:
            q = self.logical_to_model_unclamped(name, values[name])
            lo, hi = self.model_range_rad[name]
            if name in self.full_turn:
                out[name] = q  # full turn: displayed even inside the model's range gap (kinematic preview)
                continue
            if q < lo or q > hi:
                reasons.append(f"{name}:display_clamped")
                q = min(max(q, lo), hi)
            out[name] = q
        return out, reasons

    # -- inverse ---------------------------------------------------------------------------
    def model_to_logical(self, name: str, q: float) -> float:
        if name == GRIPPER:
            lo, hi = self.model_range_rad[GRIPPER]
            frac = (q - lo) / (hi - lo)
            if self.gripper_0_maps_to == "model_max":
                frac = 1.0 - frac
            return 100.0 * frac
        value = self.sign[name] * (math.degrees(q) - self.offset_deg[name])
        if name in self.full_turn:
            value = (value + 180.0) % 360.0 - 180.0
        return value

    def model_range_logical(self) -> dict[str, tuple[float, float]]:
        """The official model range expressed in logical units (sorted low, high)."""
        out = {}
        for name in JOINT_NAMES:
            if name in self.full_turn:
                out[name] = (-180.0, 180.0)  # a full turn; the model's small gap is display-clamped
                continue
            a, b = (self.model_to_logical(name, q) for q in self.model_range_rad[name])
            out[name] = (min(a, b), max(a, b))
        return out


def mapping_from_config(
    model_range_rad: Mapping[str, tuple[float, float]], sim_cfg: SimulationSection
) -> JointMapping:
    return JointMapping(
        model_range_rad=dict(model_range_rad),
        sign={n: float(sim_cfg.joint_sign[n]) for n in ARM_JOINTS},
        offset_deg={n: float(sim_cfg.joint_offset_deg[n]) for n in ARM_JOINTS},
        gripper_0_maps_to=sim_cfg.logical_gripper_0_maps_to,
        full_turn=frozenset(sim_cfg.full_turn_joints),
    )


def sim_provisional_bounds(mapping: JointMapping, margin_deg: float) -> JointBounds:
    """SIM_ONLY bounds: official model ranges in logical degrees with the 5° margin (calibration
    notes item 5). Explicitly labelled provisional; never used for hardware."""
    ranges = mapping.model_range_logical()
    arm = {n: ranges[n] for n in ARM_JOINTS}
    bounds = build_bounds(arm, margin_deg, "official MJCF jnt_range", LABEL_SIM)
    return restrict(bounds, policy.JOINT_RANGE_LIMITS, "owner-set usable range")

# M1 interface contracts (normative; illustrative Python signatures)

## Logical joint vector and units

Order: `shoulder_pan`, `shoulder_lift`, `elbow_flex`, `wrist_flex`, `wrist_roll`, `gripper`. All six required for commands; named dictionary preferred to positional arrays. First five use **degrees**. Sixth is normalized 0–100, **not degrees**. Example neutral-like values are placeholders only, not validated safe poses.

```python
@dataclass(frozen=True)
class JointFrame:
    values: Mapping[str, float]  # exactly all 6 keys; validated finite
    timestamp_mono_s: float
    seq: int


class RobotPort(Protocol):
    def initialize(self, calibration_path: Path, port: str) -> None: ...
    def observe(self) -> JointFrame: ...  # actual hardware positions
    def send(self, full_six_joint_targets: JointFrame) -> JointFrame: ...  # returned sent positions
    def torque_off(self) -> None: ...
    def disconnect(self) -> None: ...


class SimulatorPort(Protocol):
    def set_visual_joint_pose(self, logical: JointFrame) -> None: ...
    def read_visual_joint_pose(self) -> JointFrame: ...
    def render_rgb(self) -> numpy.ndarray: ...
```

`initialize` is never called from import/startup/CI; only after UI warning and explicit confirmation. The actual implementation can add an explicit `connect`/`configure` and authorization state, but **must not** erase this distinction. The real adapter maps logical keys to LeRobot action keys by appending `.pos` and conversely strips suffix on returned dict.

## Stages and identity

`requested`: latest slider target (may change >20 times/s).

`staged`: after range and per-tick 10-unit clamp, prior to LeRobot. One full six-value dict per controller tick.

`sent`: result **returned** by LeRobot `send_action()`; differs if measured-relative clamp changes it; unknown until real send.

`actual`: read from `get_observation()`, with a timestamp and stale flag; never copy from requested/staged/sent on error.

`displayed`: current MuJoCo visual joint pose (sim-only from staged; mirror/real execution from actual).

`last_success_read_at`, `last_success_send_at`, `tick_hz_actual`, `command_seq`, `fault` are separate status fields.

## LeRobot configuration example (for API verification, not executed in spec package)

```python
from lerobot.robots.so_follower.so_follower import SOFollower, SOFollowerRobotConfig

cfg = SOFollowerRobotConfig(
    port="/dev/serial/by-id/<operator-selected-port>",
    id="<existing-calibration-id>",
    use_degrees=True,
    max_relative_target={
        "shoulder_pan": 20.0,
        "shoulder_lift": 20.0,
        "elbow_flex": 20.0,
        "wrist_flex": 20.0,
        "wrist_roll": 20.0,
        "gripper": 100.0,
    },
)
robot = SOFollower(cfg)
# ONLY after user confirmation and validated calibration:
# robot.connect(calibrate=False)
# and after a separate enable-movement action:
# sent = robot.send_action({"shoulder_pan.pos":..., ..., "gripper.pos":...})
```

The **full six-key dictionary** is important because `ensure_safe_goal_position` expects exact cap keys matching the action. As the installed API may change, implementation must inspect and pin it before use.

## Status model

```text
mode: SIM_ONLY | REAL_TO_SIM | SIM_TO_REAL
connection: DISCONNECTED | CONNECTING | CONNECTED_READ_ONLY | ARMED | HOLDING | FAULT_LATCHED | TORQUE_OFF
hardware_authorized: boolean
calibration_valid: boolean
fault: { code, first_at, last_at, details, acknowledged } | null
motor_torque: ON | OFF | UNKNOWN
```

For `SIM_ONLY`, `motor_torque=UNKNOWN / N/A` and no serial access. `FAULT_LATCHED` never automatically clears from a good subsequent read. Separately log state-transition reasons.

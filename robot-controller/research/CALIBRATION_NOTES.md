# Calibration, normalization and margin warning

## Why this matters

The SO-101-VR-Control source builds a ~5° safety margin using `range_min`/`range_max` raw tick counts, relative to a midpoint, plus an intersection with its URDF joint limits. LeRobot transforms raw servo ticks through calibration to normalized joint units (degrees for arms, 0–100 gripper). A new application must **not** assume the raw midpoint formula produces the same axis origin/sign/range as the installed LeRobot version in every configuration.

## Required approach

1. Identify LeRobot version and per-joint normalization behavior in `feetech` bus/calibration implementation; record source revision and unit transforms.
2. Load existing calibration file only for hardware operation. Validate all five angular joints and gripper entries (IDs/ranges/validity) using the installed API or parsed schema where supported; reject missing, corrupt or inconsistent files. **Never call automatic calibration in M1**.
3. Derive physical allowed bounds **in the very same logical degree convention** in which `SOFollower.send_action()` accepts its targets. Intersect those bounds with the existing verified software/model limits when appropriate; subtract/add 5° from physical joint angular endpoints only, not 5 from gripper 0–100. Log the source of each bound and show clamp decisions.
4. If the normalized angular bounds cannot be derived or checked confidently without hardware, implement a pure-function converter with fixtures and mark real actuation unavailable until validated, rather than copying a suspect formula.
5. Sim-only needs no physical calibration. Use the official MJCF's allowed joint ranges for safe visual sliders, and a 5° conservative margin for five angular joints if consistent with range endpoints; gripper 0–100. **Label these as provisional simulation-only bounds, not as physical calibrated limits.**
6. No mandatory physical alignment wizard. If mapping is inconsistent when robot arrives, user will visually evaluate; adapter mapping may then be adjusted and manually tested, but leave official model files unchanged.

## Relevant sources

- [VR raw calibration margin conversion](https://github.com/kabilankb/SO-101-VR-Control/blob/main/so101_vr/core/robot_interface.py#L1909-L1956)
- [LeRobot follower config / `use_degrees`](https://github.com/huggingface/lerobot/blob/main/src/lerobot/robots/so_follower/config_so_follower.py)
- [TheRobotStudio model calibration conventions](https://github.com/TheRobotStudio/SO-ARM100/blob/main/Simulation/SO101/README.md)

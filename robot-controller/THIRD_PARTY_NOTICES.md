# Third-party notices

This repository does **not** redistribute upstream model files. `scripts/fetch_model_assets.py`
downloads them at a pinned commit into `vendor/` (gitignored) and verifies their sha256 hashes.

| Component | Source | Revision | License | How it is used |
|---|---|---|---|---|
| SO-101 MuJoCo model (`Simulation/SO101/scene.xml`, `so101_new_calib.xml`, 13 STL meshes) | https://github.com/TheRobotStudio/SO-ARM100 | `a758567c3978dfeefe282ede0500085a48fe8f78` | Apache-2.0 (`vendor/SO-ARM100/LICENSE`) | Loaded unmodified for pose preview |
| MuJoCo | https://github.com/google-deepmind/mujoco | 3.17.0 (PyPI) | Apache-2.0 | Model loading and offscreen rendering |
| Qt for Python (PySide6-Essentials) | https://pypi.org/project/PySide6/ | 6.12.0 | LGPL-3.0-only / GPL | GUI |
| numpy | https://numpy.org | 2.5.4 | BSD-3-Clause et al. | Arrays |
| SO-101-VR-Control | https://github.com/kabilankb/SO-101-VR-Control | `2270be2153149010b18d91cf77fd31d05e0afff2` | MIT, Copyright (c) 2025 Emil Rofors | Calibrated ±5° range formula (`devices/calibration.py`, attribution in the file). The per-tick step limiter adapted earlier was removed on 2026-11-07 (ADR-010) |
| LeRobot | https://github.com/huggingface/lerobot | `0.4.4` (`8fff0fde…`) | Apache-2.0 | Optional `hardware` extra; real-arm driver (`devices/lerobot_so101.py`), not redistributed |
| PyTorch (via LeRobot) | https://pytorch.org | `2.10.0+cpu` | BSD-3-Clause | Transitive, hardware extra only |

### MIT notice for the adapted SO-101-VR-Control arithmetic (verbatim from its LICENSE @ 2270be2)

```text
MIT License

Copyright (c) 2025 Emil Rofors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

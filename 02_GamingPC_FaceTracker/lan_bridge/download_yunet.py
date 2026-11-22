"""Explicit optional download of the MIT-licensed OpenCV Zoo face detector."""
from __future__ import annotations

import argparse
import pathlib
import urllib.request

URL = "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"


def main():
    parser = argparse.ArgumentParser(description="Download YuNet ONNX explicitly from OpenCV Zoo")
    parser.add_argument("--output", default="models/face_detection_yunet_2023mar.onnx")
    args = parser.parse_args()
    path = pathlib.Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {URL}")
    with urllib.request.urlopen(URL, timeout=30) as response:
        content = response.read(1_000_000)
    if not 100_000 < len(content) < 1_000_000:
        raise RuntimeError("Unexpected model size — refusing to save")
    path.write_bytes(content)
    print(f"Saved {len(content):,} bytes to {path}")


if __name__ == "__main__":
    main()

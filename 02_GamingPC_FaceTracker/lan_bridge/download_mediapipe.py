"""Download the official MediaPipe Face Landmarker model once; inference is local."""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import urllib.request
import zipfile

from .nose_detector import DEFAULT_MODEL

URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
POSE_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args()
    download(URL, args.output)
    download(POSE_URL, DEFAULT_MODEL.with_name("pose_landmarker_lite.task"))


def download(url, destination):
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read(20_000_001)
    if not 1_000_000 < len(content) <= 20_000_000 or not zipfile.is_zipfile(io.BytesIO(content)):
        raise RuntimeError("Unexpected MediaPipe model download; refusing to save")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".download")
    temporary.write_bytes(content)
    temporary.replace(destination)
    print(f"Saved MediaPipe model ({len(content):,} bytes)")


if __name__ == "__main__":
    main()

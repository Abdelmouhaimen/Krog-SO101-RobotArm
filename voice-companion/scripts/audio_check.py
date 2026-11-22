"""Check microphone and speaker through the same code path the app uses.

    python scripts/audio_check.py [--input-device X] [--output-device Y] [--seconds 3] [--tone]

Records from the microphone and prints the level every 0.25 s (speak to see it move). With --tone it
also plays a quiet 440 Hz beep through the speaker and reports when playback started and finished as
seen by the PlaybackBuffer (the same signal that drives Talk Mode).
"""

from __future__ import annotations

import argparse
import array
import math
import threading
import time

from so101_voice.audio.devices import MiniaudioIO
from so101_voice.audio.playback import PlaybackBuffer


def rms(pcm: bytes) -> float:
    a = array.array("h", pcm)
    return math.sqrt(sum(x * x for x in a) / max(1, len(a))) / 32768 if a else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-device")
    ap.add_argument("--output-device")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--tone", action="store_true", help="play a quiet 0.5 s beep")
    args = ap.parse_args()

    io = MiniaudioIO(
        input_rate=16000, output_rate=24000, input_device=args.input_device, output_device=args.output_device
    )
    buf = PlaybackBuffer(24000, device_latency_s=io.output_latency_s)
    events = []
    buf.add_listener(lambda e: events.append((time.monotonic(), e.state.value, e.reason)))
    lock = threading.Lock()
    captured = bytearray()
    chunks = [0]

    def on_audio(pcm: bytes) -> None:
        with lock:
            captured.extend(pcm)
            chunks[0] += 1

    io.start_output(buf)
    io.start_input(on_audio)
    t0 = time.monotonic()
    if args.tone:
        n = 12000
        tone = array.array("h", (int(3000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(n)))
        buf.write(tone.tobytes())
        buf.mark_end_of_stream()
    try:
        while time.monotonic() - t0 < args.seconds:
            time.sleep(0.25)
            with lock:
                last = bytes(captured[-8000:])
            level = rms(last)
            print(f"{time.monotonic() - t0:5.2f}s  mic level {level:6.4f} {'#' * int(level * 400)}")
    finally:
        io.close()
    secs = len(captured) / 2 / 16000
    print(f"captured {secs:.2f}s in {chunks[0]} callbacks (expected ≈{args.seconds:.2f}s)")
    for t, state, reason in events:
        print(f"playback {state:7} ({reason}) at {t - t0:.3f}s")
    ok = secs > 0.8 * args.seconds and (not args.tone or [e[1] for e in events] == ["playing", "idle"])
    print("OK" if ok else "PROBLEM")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

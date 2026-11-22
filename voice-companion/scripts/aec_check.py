"""Acoustic echo-cancellation check: real speakers, real microphone, no network, no paid services.

    python scripts/aec_check.py                 # automated scenarios (~45 s of test sounds)
    python scripts/aec_check.py --interactive   # also: YOU speak while the "assistant" talks
    python scripts/aec_check.py --level 0.2     # quieter test signals

Plays synthetic, speech-like signals through the speakers. Records the RAW microphone and the
ECHO-CANCELLED microphone at the same time, so the two can be compared, and measures:

* echo reduction (ERLE, dB) with the "assistant" talking and the user silent;
* false speech onsets (local energy VAD, the same detector as the ``vad`` input mode) during
  assistant-only audio: a local proxy for false barge-ins;
* double talk: how much of a simultaneous "user" voice survives, and its detection delay;
* game audio: is it cancelled in each topology;
* several playback levels;
* the speaker→microphone delay (for ``audio.output_latency_ms`` and Talk Mode).

WAV files are written to ``logs/aec/`` so the raw and cancelled recordings can be listened to.
The system's default devices and volumes are not changed.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import random
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from so101_voice.audio.aec import (
    SINK_NODE,
    SOURCE_NODE,
    AECSettings,
    AECUnavailable,
    PipeWireAEC,
    list_audio_nodes,
    probe,
)

RATE = 16000
FRAME = RATE // 50  # 20 ms
OUT = Path(__file__).resolve().parents[1] / "logs" / "aec"
VOWELS = [
    (730, 1090, 2440),
    (270, 2290, 3010),
    (530, 1840, 2480),
    (570, 840, 2410),
    (300, 870, 2240),
    (660, 1720, 2410),
]


# ------------------------------------------------------------------ test signals
def voice(seconds: float, f0: float, seed: int, level: float) -> array.array:
    """Formant-synthesized babble: voiced syllables with consonant noise (speech-like spectrum + rhythm)."""
    rnd = random.Random(seed)
    out = array.array("h")
    t = 0.0
    while t < seconds:
        syl = rnd.uniform(0.14, 0.26)
        formants = rnd.choice(VOWELS)
        pitch = f0 * rnd.uniform(0.85, 1.15)
        n = int(syl * RATE)
        for i in range(n):
            ts = i / RATE
            env = math.sin(math.pi * i / n) ** 2
            s = 0.0
            for k in range(1, int(3800 / pitch)):
                f = k * pitch
                a = sum(1.0 / (1 + ((f - F) / (60 + F * 0.08)) ** 2) for F in formants) / k**0.6
                s += a * math.sin(2 * math.pi * f * ts + k)
            if i < n * 0.15:  # consonant onset
                s += rnd.uniform(-1, 1) * 1.2
            out.append(int(max(-1, min(1, 0.12 * s * env)) * 32767 * level))
        t += syl
        if rnd.random() < 0.25:  # pauses between words
            gap = int(rnd.uniform(0.05, 0.25) * RATE)
            out.extend([0] * gap)
            t += gap / RATE
    return out[: int(seconds * RATE)]


def game(seconds: float, level: float, seed: int = 7) -> array.array:
    """Game-like audio: music chords + footsteps/gunshot noise bursts."""
    rnd = random.Random(seed)
    out = array.array("h")
    chords = [(220, 277, 330), (196, 247, 294), (175, 220, 262)]
    burst = 0
    for i in range(int(seconds * RATE)):
        ts = i / RATE
        ch = chords[int(ts / 0.8) % 3]
        s = sum(math.sin(2 * math.pi * f * ts) for f in ch) * 0.12
        if burst <= 0 and rnd.random() < 0.0004:
            burst = int(0.08 * RATE)
        if burst > 0:
            s += rnd.uniform(-1, 1) * 0.6 * (burst / (0.08 * RATE))
            burst -= 1
        out.append(int(max(-1, min(1, s)) * 32767 * level))
    return out


def click() -> array.array:
    a = array.array("h", [0] * int(0.3 * RATE))
    for i in range(80):
        a[i] = int(26000 * math.sin(2 * math.pi * 2000 * i / RATE))
    return a


def silence(seconds: float) -> array.array:
    return array.array("h", [0] * int(seconds * RATE))


def write_wav(path: Path, pcm: array.array) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> array.array:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == RATE and w.getsampwidth() == 2
        data = w.readframes(w.getnframes())
        ch = w.getnchannels()
    a = array.array("h")
    a.frombytes(data)
    return a if ch == 1 else array.array("h", a[::ch])


# ------------------------------------------------------------------ recording
def record_play(
    name: str, mic_node: str, plays: list[tuple[Path, str | None, float]], seconds: float
) -> tuple[array.array, array.array]:
    """Record raw mic + echo-cancelled mic while playing [(wav, target node or None=default, start s)]."""
    raw_p, ec_p = OUT / f"{name}-raw.wav", OUT / f"{name}-aec.wav"
    common = ["--rate", str(RATE), "--channels", "1", "--format", "s16"]
    recs = [
        subprocess.Popen(["pw-record", "--target", mic_node, *common, str(raw_p)]),
        subprocess.Popen(["pw-record", "--target", SOURCE_NODE, *common, str(ec_p)]),
    ]
    time.sleep(0.4)
    t0 = time.monotonic()
    players: list[subprocess.Popen[bytes]] = []
    pending = sorted(plays, key=lambda p: p[2])
    while pending or any(p.poll() is None for p in players) or time.monotonic() - t0 < seconds:
        while pending and time.monotonic() - t0 >= pending[0][2]:
            wav, target, _ = pending.pop(0)
            cmd = ["pw-play"] + (["--target", target] if target else []) + [str(wav)]
            players.append(subprocess.Popen(cmd))
        time.sleep(0.01)
    time.sleep(0.4)
    for r in recs:
        r.send_signal(signal.SIGINT)
    for r in recs:
        r.wait(5)
    return read_wav(raw_p), read_wav(ec_p)


# ------------------------------------------------------------------ analysis
def frames_db(pcm: array.array) -> list[float]:
    out = []
    for i in range(0, len(pcm) - FRAME, FRAME):
        seg = pcm[i : i + FRAME]
        rms = math.sqrt(sum(x * x for x in seg) / FRAME) / 32768
        out.append(20 * math.log10(rms) if rms > 1e-7 else -140.0)
    return out


def energy(pcm: array.array, a: float, b: float) -> float:
    seg = pcm[int((a + 0.4) * RATE) : int((b + 0.4) * RATE)]  # +0.4 s: recording started before playback
    return sum(x * x for x in seg) / max(1, len(seg)) + 1e-3


def db(x: float) -> float:
    return round(10 * math.log10(x), 1)


def onsets(
    pcm: array.array, a: float, b: float, threshold: float = -42.0, attack: int = 3, hang: int = 20
) -> list[float]:
    """Speech onsets (s, from playback start) by the same energy rule as the ``vad`` input mode."""
    fr = frames_db(pcm)
    lo, hi = int((a + 0.4) * 50), int((b + 0.4) * 50)
    found, run, quiet, active = [], 0, 0, False
    for i in range(lo, min(hi, len(fr))):
        loud = fr[i] >= threshold
        run = run + 1 if loud else 0
        quiet = 0 if loud else quiet + 1
        if not active and run >= attack:
            active = True
            found.append(round(i / 50 - 0.4 - (attack - 1) * 0.02, 2))
        elif active and quiet >= hang:
            active = False
    return found


def floor_db(pcm: array.array) -> float:
    fr = sorted(frames_db(pcm))
    return fr[len(fr) // 10]


def speech_db(pcm: array.array, a: float, b: float) -> float:
    """Mean level of the louder half of 20 ms frames in [a, b] (≈ the speech frames)."""
    fr = sorted(frames_db(pcm)[int((a + 0.4) * 50) : int((b + 0.4) * 50)])[::-1]
    top = fr[: max(1, len(fr) // 2)]
    return round(sum(top) / len(top), 1)


def adaptive_onsets(pcm: array.array, a: float, b: float, floor: float) -> list[float]:
    """Onsets 15 dB over the recording's own noise floor (a fixed threshold hides quiet echo)."""
    return onsets(pcm, a, b, threshold=floor + 15)


def envelope_corr(x: array.array, y: array.array, a: float, b: float) -> float:
    fx = frames_db(x)[int((a + 0.4) * 50) : int((b + 0.4) * 50)]
    fy = frames_db(y)[int((a + 0.4) * 50) : int((b + 0.4) * 50)]
    n = min(len(fx), len(fy))
    fx, fy = fx[:n], fy[:n]
    mx, my = sum(fx) / n, sum(fy) / n
    num = sum((p - mx) * (q - my) for p, q in zip(fx, fy, strict=True))
    den = math.sqrt(sum((p - mx) ** 2 for p in fx) * sum((q - my) ** 2 for q in fy)) or 1.0
    return round(num / den, 3)


def delay_s(ref: array.array, rec: array.array) -> float | None:
    """Delay of a click: first frame in the recording well above its noise floor."""
    fr = frames_db(rec)
    floor = sorted(fr)[len(fr) // 5]
    for i, v in enumerate(fr):
        if v > floor + 25:
            return round(i * 0.02 - 0.4, 3)
    return None


# ------------------------------------------------------------------ scenarios
def run(level: float, interactive: bool, mic: str | None, agc: bool) -> dict[str, object]:
    OUT.mkdir(parents=True, exist_ok=True)
    nodes = list_audio_nodes()
    sources = [n for n in nodes if n.media_class == "Audio/Source" and not n.name.startswith("so101")]
    mic_node = mic or next(
        (n.name for n in sources if "Digital Microphone" in n.description), sources[0].name
    )
    report: dict[str, object] = {
        "mic_node": mic_node,
        "level": level,
        "agc": agc,
        "method": "dBFS levels; onsets = 60 ms above (noise floor + 15 dB), a local proxy for Flux",
    }
    files = {
        "assistant": voice(6.0, 210, 1, level),
        "assistant_long": voice(10.0, 210, 2, level),
        "user": voice(2.5, 115, 3, level),
        "game": game(8.0, level * 0.8),
        "click": click(),
    }
    for k, v in files.items():
        write_wav(OUT / f"src-{k}.wav", v)
    src = {k: OUT / f"src-{k}.wav" for k in files}

    for topology in ("monitor", "sink"):
        aec = PipeWireAEC(AECSettings(topology=topology, gain_control=agc))  # type: ignore[arg-type]
        aec.start()
        tts_target = SINK_NODE if topology == "sink" else None  # where the assistant's audio goes
        try:
            res: dict[str, object] = {}
            # 0. silence: noise floor, sanity
            raw, ec = record_play(f"{topology}-silence", mic_node, [], 2.0)
            fl_raw, fl_ec = floor_db(raw), floor_db(ec)
            res["noise_floor_dbfs"] = {"raw": round(fl_raw, 1), "aec": round(fl_ec, 1)}
            # 1. assistant talking, user silent (after a 4 s warm-up so the filter converges)
            raw, ec = record_play(
                f"{topology}-echo", mic_node, [(src["assistant_long"], tts_target, 0.0)], 10.0
            )
            res["echo_only"] = {
                "erle_db_first_2s": db(energy(raw, 0.3, 2.0) / energy(ec, 0.3, 2.0)),
                "erle_db_converged": db(energy(raw, 4.0, 9.7) / energy(ec, 4.0, 9.7)),
                "echo_speech_level_dbfs": {"raw": speech_db(raw, 4.0, 9.7), "aec": speech_db(ec, 4.0, 9.7)},
                "false_onsets_raw": len(adaptive_onsets(raw, 0.0, 10.0, fl_raw)),
                "false_onsets_aec": len(adaptive_onsets(ec, 0.0, 10.0, fl_ec)),
            }
            # 2. game audio on the speakers (not sent through the echo-cancel sink)
            raw, ec = record_play(f"{topology}-game", mic_node, [(src["game"], None, 0.0)], 8.0)
            res["game_audio"] = {
                "reduction_db": db(energy(raw, 2.0, 7.8) / energy(ec, 2.0, 7.8)),
                "level_dbfs": {"raw": speech_db(raw, 2.0, 7.8), "aec": speech_db(ec, 2.0, 7.8)},
                "false_onsets_raw": len(adaptive_onsets(raw, 0.0, 8.0, fl_raw)),
                "false_onsets_aec": len(adaptive_onsets(ec, 0.0, 8.0, fl_ec)),
            }
            # 3. speaker→microphone delay (click), for Talk Mode / output latency
            raw, ec = record_play(f"{topology}-click", mic_node, [(src["click"], tts_target, 0.0)], 0.6)
            res["speaker_to_mic_delay_s"] = delay_s(files["click"], raw)
            # 4. playback levels: fresh canceller per level (no state carried over), stream level only
            aec.stop()
            levels = {}
            for lv in (0.15, 0.4, 0.8):
                p = OUT / f"src-assistant-{lv}.wav"
                write_wav(p, voice(6.0, 210, 5, lv))
                aec.start()
                try:
                    raw, ec = record_play(f"{topology}-level-{lv}", mic_node, [(p, tts_target, 0.0)], 6.0)
                finally:
                    aec.stop()
                levels[str(lv)] = {
                    "level_dbfs": {"raw": speech_db(raw, 2.5, 5.8), "aec": speech_db(ec, 2.5, 5.8)},
                    "false_onsets_raw": len(adaptive_onsets(raw, 1.0, 6.0, fl_raw)),
                    "false_onsets_aec": len(adaptive_onsets(ec, 1.0, 6.0, fl_ec)),
                }
            res["levels"] = levels
            aec.start()
            if topology == "sink":
                # 5a. the "user" alone (outside the reference): is speech preserved without echo?
                raw, ec = record_play("sink-user-alone", mic_node, [(src["user"], None, 0.0)], 2.6)
                user_alone = {"raw": speech_db(raw, 0.1, 2.4), "aec": speech_db(ec, 0.1, 2.4)}
                res["user_alone"] = {
                    "level_dbfs": user_alone,
                    "level_change_db": round(user_alone["aec"] - user_alone["raw"], 1),
                    "envelope_correlation": envelope_corr(raw, ec, 0.1, 2.4),
                }
                # 5b. double talk, last (a speaker-played "user" can mis-train the filter)
                raw, ec = record_play(
                    "sink-doubletalk",
                    mic_node,
                    [(src["assistant_long"], tts_target, 0.0), (src["user"], None, 5.0)],
                    10.0,
                )
                on_raw = adaptive_onsets(raw, 4.6, 8.0, fl_raw)
                on_ec = adaptive_onsets(ec, 4.6, 8.0, fl_ec)
                res["double_talk"] = {
                    "aec_level_echo_only_dbfs": speech_db(ec, 2.5, 4.5),
                    "aec_level_with_user_dbfs": speech_db(ec, 5.2, 7.3),
                    "user_attenuation_vs_alone_db": round(user_alone["aec"] - speech_db(ec, 5.2, 7.3), 1),
                    "user_onset_raw_s": on_raw[0] if on_raw else None,
                    "user_onset_aec_s": on_ec[0] if on_ec else None,
                    "detection_delay_vs_raw_s": round(on_ec[0] - on_raw[0], 2) if on_raw and on_ec else None,
                }
            if interactive:
                print(f"\n[{topology}] INTERACTIVE: the assistant will talk for 10 s. Speak a short command")
                print("    (e.g. 'stop, what time is it') about 3 s after it starts. Press Enter to begin.")
                input()
                raw, ec = record_play(
                    f"{topology}-you", mic_node, [(src["assistant_long"], tts_target, 0.0)], 10.0
                )
                res["interactive"] = {
                    "onsets_raw": adaptive_onsets(raw, 0, 10, fl_raw),
                    "onsets_aec": adaptive_onsets(ec, 0, 10, fl_ec),
                }
                print(f"    saved {OUT}/{topology}-you-raw.wav and -aec.wav: listen to compare")
            report[topology] = res
        finally:
            aec.stop()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--level", type=float, default=0.5, help="test signal level 0..1 (system volume untouched)"
    )
    ap.add_argument("--interactive", action="store_true", help="also record you speaking over the assistant")
    ap.add_argument("--mic", help="microphone node.name (default: the built-in Digital Microphone)")
    ap.add_argument(
        "--agc", action="store_true", help="enable WebRTC gain control (default off, as in the app)"
    )
    args = ap.parse_args()
    problems = probe()
    if problems:
        print("AEC unavailable:\n  - " + "\n  - ".join(problems))
        return 2
    try:
        report = run(args.level, args.interactive, args.mic, args.agc)
    except AECUnavailable as e:
        print(f"AEC unavailable: {e}")
        return 2
    path = OUT.parent / "aec-report.json"
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nreport: {path}\nrecordings: {OUT}/ (*-raw.wav vs *-aec.wav)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

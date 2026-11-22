"""End-to-end acoustic check of the REAL app audio path, with local fake providers (nothing billable).

    python scripts/aec_pipeline_check.py            # compares audio modes aec vs raw
    python scripts/aec_pipeline_check.py --game     # with game-like audio playing on the speakers

The real application objects are used (``build_companion``):
* real miniaudio microphone and speaker;
* PipeWire echo cancellation (mode ``aec``);
* the real Flux client, PlaybackBuffer and Talk Mode coordinator.

The fake Flux TTS sends synthetic speech, which plays out loud through the speakers. The fake Flux
server records **exactly the bytes the app streams for speech recognition**, so the check measures what
Deepgram would hear. The robot is the fake SO-101 API: Talk Mode timing is checked against the real
playback, and the real arm is never involved.

Per mode it reports:
* the level of the assistant's voice inside the STT stream (echo reaching Flux);
* speech-like onsets in that stream (local proxy for false barge-ins);
* Talk Mode start/stop versus playback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from aec_check import game, voice, write_wav  # noqa: E402

from so101_voice.app import build_companion  # noqa: E402
from so101_voice.config import Secrets, load_config  # noqa: E402
from so101_voice.conversation import Phase  # noqa: E402
from tests.fake_providers import DG_KEY, OA_KEY, FakeFlux, FakeFluxTTS, FakeOpenAI, say  # noqa: E402
from tests.fakes import TOKEN, FakeSO101API  # noqa: E402

ANSWER = "Once upon a time a little robot learned to dance. " * 3


def level_db(chunks: list[bytes]) -> float:
    import array

    a = array.array("h")
    for c in chunks:
        a.frombytes(c)
    if not a:
        return -140.0
    rms = math.sqrt(sum(x * x for x in a) / len(a)) / 32768
    return round(20 * math.log10(rms), 1) if rms > 1e-7 else -140.0


def frame_onsets(chunks: list[bytes], floor_db: float) -> int:
    """Speech-like segments in the STT stream: 3+ consecutive 80 ms chunks 15 dB over the floor."""
    run, n, active = 0, 0, False
    for c in chunks:
        loud = level_db([c]) >= floor_db + 15
        run = run + 1 if loud else 0
        if run >= 3 and not active:
            n, active = n + 1, True
        if not loud:
            active = False
    return n


async def run_mode(mode: str, level: float, with_game: bool, agc: bool = True) -> dict[str, object]:
    speech = voice(9.0, 210, 11, level).tobytes()

    def synth(job_no: int, text: str) -> bytes:
        # 24 kHz output: upsample the 16 kHz synthetic voice (sample-and-hold, fine for a test signal)
        import array

        src = array.array("h")
        src.frombytes(speech)
        out = array.array("h")
        n = int(len(text) / len(ANSWER) * len(src))
        for i in range(int(n * 1.5)):
            out.append(src[min(int(i / 1.5), len(src) - 1)])
        return out.tobytes()

    async with FakeFlux(keep_audio=True) as flux, FakeFluxTTS(synth=synth, frame_delay_s=0.0) as aura:
        cfg = load_config()
        cfg.audio.mode = mode  # type: ignore[assignment]
        cfg.audio.aec.gain_control = agc
        cfg.stt.url, cfg.tts.url = flux.url, aura.url
        cfg.llm.base_url, cfg.llm.greeting = "http://openai.test/v1", None
        cfg.cost.ledger_file = None
        cfg.log_file = None
        api = FakeSO101API()
        oai = FakeOpenAI([say(ANSWER)])
        events: list[object] = []
        comp = build_companion(
            cfg,
            Secrets(deepgram_api_key=DG_KEY, openai_api_key=OA_KEY, so101_api_token=TOKEN),
            on_event=events.append,
            robot=api.client(),
            llm_transport=oai.transport(),
        )
        conv = comp.conversation
        pb_events: list[tuple[float, str]] = []
        conv.playback.add_listener(lambda e: pb_events.append((time.monotonic(), e.state.value)))
        talk: list[tuple[float, bool]] = []
        game_proc = None
        try:
            await comp.startup()
            assert await conv.start(), conv.last_error
            await flux.wait_connections(1)
            await asyncio.sleep(2.0)  # baseline: room noise only
            t_base = asyncio.get_running_loop().time()
            if with_game:
                write_wav(ROOT / "logs/aec/src-game-long.wav", game(14.0, level * 0.8))
                game_proc = subprocess.Popen(["pw-play", str(ROOT / "logs/aec/src-game-long.wav")])  # noqa: ASYNC220
                await asyncio.sleep(2.0)
            t_ask = asyncio.get_running_loop().time()
            await flux.utter("tell me a story")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                talk.append((time.monotonic(), api.talk_active()))
                if conv.generations_started and conv._gen is None and conv.phase is Phase.LISTENING:
                    break
                await asyncio.sleep(0.02)
            for _ in range(25):
                talk.append((time.monotonic(), api.talk_active()))
                await asyncio.sleep(0.02)
            loop_now = asyncio.get_running_loop().time()
            mono_offset = time.monotonic() - loop_now
            play = [t for t, s in pb_events if s == "playing"]
            idle = [t for t, s in pb_events if s == "idle"]
            assert play and idle, "assistant audio never played"
            p0, p1 = play[0] - mono_offset, idle[-1] - mono_offset
            base = [c for t, c in flux.received if t_base - 1.8 <= t < t_base]
            during = [c for t, c in flux.received if p0 + 0.3 <= t < p1]
            game_only = [c for t, c in flux.received if t_base + 0.5 <= t < t_ask] if with_game else []
            floor = level_db(base)
            on = next((t for t, a in talk if a), None)
            off = next((t for t, a in talk if not a and on and t > on), None)
            return {
                "mode": conv.audio_mode,
                "assistant_played_s": round(p1 - p0, 2),
                "stt_stream_noise_floor_dbfs": floor,
                "stt_stream_level_while_assistant_speaks_dbfs": level_db(during),
                "echo_above_floor_db": round(level_db(during) - floor, 1),
                "agc": agc,
                "per_second_dbfs": [
                    level_db([c for t, c in flux.received if p0 + k <= t < p0 + k + 1])
                    for k in range(int(p1 - p0))
                ],
                "speechlike_onsets_while_assistant_speaks": frame_onsets(during, floor),
                "game_only_level_dbfs": level_db(game_only) if with_game else None,
                "talk_on_after_playback_start_s": None if on is None else round(on - play[0], 3),
                "talk_off_after_playback_end_s": None if off is None else round(off - idle[-1], 3),
                "interruptions": conv.interruptions,
                "llm_requests": len(oai.requests),
            }
        finally:
            if game_proc is not None:
                game_proc.terminate()
            await comp.shutdown()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level", type=float, default=0.5)
    ap.add_argument("--game", action="store_true", help="play game-like audio on the speakers too")
    ap.add_argument("--modes", default="aec,raw")
    ap.add_argument("--no-agc", action="store_true", help="disable WebRTC gain control in aec mode")
    args = ap.parse_args()
    report = {}
    for mode in args.modes.split(","):
        report[mode] = await run_mode(mode, args.level, args.game, agc=not args.no_agc)
        print(json.dumps(report[mode], indent=1), flush=True)
    out = ROOT / "logs" / ("aec-pipeline-report-game.json" if args.game else "aec-pipeline-report.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

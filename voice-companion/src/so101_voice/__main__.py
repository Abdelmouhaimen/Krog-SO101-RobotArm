"""so101-voice — talk to the SO-101 desk companion (Deepgram Flux → OpenAI → Deepgram Flux TTS)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from so101_voice.config import AppConfig, ConfigError, Secrets, load_config, load_secrets


def _setup_logging(log_file: str | None, verbose: bool, to_stderr: bool) -> None:
    handlers: list[logging.Handler] = []
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    if to_stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
    )
    for noisy in ("websockets", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="so101-voice", description=__doc__)
    ap.add_argument("--config", help="extra TOML config file (after config/default.toml and local.toml)")
    ap.add_argument("--list-devices", action="store_true", help="list microphones and speakers, then exit")
    ap.add_argument("--input-device", help="microphone: index or name substring")
    ap.add_argument("--output-device", help="speaker: index or name substring")
    ap.add_argument(
        "--input-mode", choices=["continuous", "push_to_talk", "vad"], help="override [input].mode"
    )
    ap.add_argument("--audio-mode", choices=["aec", "raw", "half_duplex"], help="override [audio].mode")
    ap.add_argument(
        "--audio-diagnostics", action="store_true", help="check PipeWire echo cancellation (silent)"
    )
    ap.add_argument("--no-robot", action="store_true", help="do not contact the SO-101 API")
    ap.add_argument(
        "--no-physical-robot",
        action="store_true",
        help="this session: refuse arm behaviors on a REAL arm (voice + gripper Talk Mode only)",
    )
    ap.add_argument("--plain", action="store_true", help="console mode: start at once, print events")
    ap.add_argument("--duration", type=float, help="--plain: stop after N seconds")
    ap.add_argument("--print-request", action="store_true", help="print an example LLM request body, exit")
    ap.add_argument("--check-model", action="store_true", help="ask OpenAI whether llm.model is available")
    ap.add_argument("--cost-report", action="store_true", help="print the cumulative cost ledger, exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.list_devices:
        from so101_voice.audio.devices import list_devices

        for d in list_devices():
            print(f"{d.kind:6} {d.index:2}  {d.name}")
        return 0

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.input_device is not None:
        cfg.audio.input_device = args.input_device
    if args.output_device is not None:
        cfg.audio.output_device = args.output_device
    if args.input_mode is not None:
        cfg.input.mode = args.input_mode
    if args.audio_mode is not None:
        cfg.audio.mode = args.audio_mode
    if args.no_physical_robot:
        cfg.robot_tools.allow_physical = False
        cfg.follow.allow_physical = False
    if args.audio_diagnostics:
        return _audio_diagnostics(cfg)

    if args.print_request:
        print(json.dumps(_example_request(cfg), indent=2))
        return 0
    if args.cost_report:
        return _cost_report(cfg)

    secrets = load_secrets()
    _setup_logging(cfg.log_file, args.verbose, to_stderr=args.plain)
    if args.check_model:
        return asyncio.run(_check_model(cfg, secrets))
    for name, value in (
        ("DEEPGRAM_API_KEY", secrets.deepgram_api_key),
        ("OPENAI_API_KEY", secrets.openai_api_key),
    ):
        if not value:  # fail fast, before the UI takes the terminal
            print(f"error: {name} is not set (put it in .env or the environment)", file=sys.stderr)
            return 2
    if args.plain:
        return asyncio.run(_plain(cfg, secrets, args))

    from so101_voice.app import build_companion
    from so101_voice.ui.tui import CompanionTUI

    CompanionTUI(
        lambda on_event, on_talk: build_companion(
            cfg, secrets, on_event=on_event, on_talk_status=on_talk, use_robot=not args.no_robot
        )
    ).run()
    return 0


def _example_request(cfg: AppConfig) -> dict[str, object]:
    from so101_voice.character.persona import build_instructions
    from so101_voice.llm.reply import instructions, response_format
    from so101_voice.llm.responses import build_request
    from so101_voice.tools.builtin import register_builtin_tools
    from so101_voice.tools.registry import ToolRegistry

    reg = ToolRegistry()
    register_builtin_tools(reg)
    return build_request(
        model=cfg.llm.model,
        instructions=(cfg.llm.prompt or build_instructions(cfg.character.name, None))
        + instructions(reg.action_catalog()),
        input_items=[{"role": "user", "content": "Hi! What time is it?"}],
        tools=[],
        response_format=response_format(reg.action_catalog()),
        reasoning_effort=cfg.llm.reasoning_effort,
        verbosity=cfg.llm.verbosity,
        temperature=cfg.llm.temperature,
        max_output_tokens=cfg.llm.max_output_tokens,
        prompt_cache_key="so101-voice",
    )


def _audio_diagnostics(cfg: AppConfig) -> int:
    """Silent check: are the PipeWire AEC pieces present, and do the echo-cancel nodes come up?"""
    from so101_voice.audio.aec import AECSettings, AECUnavailable, PipeWireAEC, list_audio_nodes, probe

    print(f"audio mode: {cfg.audio.mode}; AEC topology: {cfg.audio.aec.topology}")
    for n in list_audio_nodes():
        if n.media_class in ("Audio/Source", "Audio/Sink"):
            print(f"  {n.media_class:12} {n.description}  [{n.name}]")
    problems = probe()
    if problems:
        print("AEC NOT available:\n  - " + "\n  - ".join(problems))
        return 1
    x = cfg.audio.aec
    aec = PipeWireAEC(
        AECSettings(x.topology, noise_suppression=x.noise_suppression, gain_control=x.gain_control)
    )
    try:
        started = aec.start(x.start_timeout_s)
        sink = started.sink_description or "none (reference = speaker monitor)"
        print(f"AEC OK: virtual mic '{started.source_description}', virtual sink: {sink}")
    except AECUnavailable as e:
        print(f"AEC failed to start: {e}")
        return 1
    finally:
        aec.stop()
    print("Acoustic test (plays sounds, compares raw vs echo-cancelled mic): python scripts/aec_check.py")
    return 0


def _cost_report(cfg: AppConfig) -> int:
    from so101_voice.config import REPO_ROOT
    from so101_voice.cost import CostLedger

    path = None if cfg.cost.ledger_file is None else REPO_ROOT / cfg.cost.ledger_file
    ledger = CostLedger(cfg.cost.prices, ledger_file=path)
    print(f"cumulative estimated cost: ${ledger.previous_usd:.4f} (ledger: {path}; estimates, not invoices)")
    print(f"prices verified on {cfg.cost.prices.verified_on}: {cfg.cost.prices.model_dump_json(indent=1)}")
    return 0


async def _check_model(cfg: AppConfig, secrets: Secrets) -> int:
    from so101_voice.llm.responses import LLMError, ResponsesClient

    if not secrets.openai_api_key:
        print("error: OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    client = ResponsesClient(api_key=secrets.openai_api_key, base_url=cfg.llm.base_url)
    try:
        info = await client.retrieve_model(cfg.llm.model)
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    if info is None:
        print(f"{cfg.llm.model}: NOT available to this API key (no other model is substituted)")
        return 1
    print(f"{cfg.llm.model}: available ({json.dumps(info)})")
    return 0


async def _plain(cfg: AppConfig, secrets: Secrets, args: argparse.Namespace) -> int:
    from so101_voice.app import build_companion, run_plain
    from so101_voice.conversation import (
        CostUpdate,
        Notice,
        PhaseChanged,
        ToolActivity,
        Transcript,
        TurnLatency,
        UIEvent,
    )
    from so101_voice.motion.speech_motion import TalkStatus
    from so101_voice.ui.tui import format_cost

    def on_event(ev: UIEvent) -> None:
        match ev:
            case Transcript(role=r, text=t, final=True):
                print(f"{'YOU' if r == 'user' else 'KROG'}: {t}", flush=True)
            case Transcript(role="assistant", text=t):
                print(f"KROG: {t}", flush=True)
            case PhaseChanged(phase=ph):
                print(f"  [{ph.value}]", flush=True)
            case Notice(level=lv, text=t):
                print(f"  ({lv}) {t}", flush=True)
            case TurnLatency() as lat:
                print(f"  (latency {lat})", flush=True)
            case ToolActivity(name=n, status=s):
                print(f"  (tool {n}: {s})", flush=True)
            case CostUpdate(snapshot=s):
                print(f"  (cost {format_cost(s)})", flush=True)

    def on_talk(st: TalkStatus) -> None:
        print(f"  (robot {st.link.value}, talk {'ON' if st.talk_on else 'OFF'}) {st.detail}", flush=True)

    try:
        companion = build_companion(
            cfg, secrets, on_event=on_event, on_talk_status=on_talk, use_robot=not args.no_robot
        )
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    task = asyncio.create_task(run_plain(companion, duration_s=args.duration))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        await companion.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())

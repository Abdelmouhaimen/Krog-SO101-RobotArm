"""Read-only LAN diagnostics: no audio, robot access, LLM or provider credentials needed."""

import argparse
import asyncio
import hashlib
import json
import time

from so101_voice.config import load_config, load_secrets
from so101_voice.perception.lan import PerceptionClient


async def check(duration: float) -> None:
    cfg = load_config()
    token = load_secrets().bridge_token
    if not token:
        raise SystemExit("BRIDGE_TOKEN is missing")
    client = PerceptionClient(cfg.perception.receiver_url, token, timeout_s=cfg.perception.timeout_s)
    started = time.monotonic()
    seen = set()
    try:
        while True:
            st = await client.poll()
            if st.face.found:
                seen.add((st.face.session, st.face.seq))
            if time.monotonic() - started >= duration:
                break
            await asyncio.sleep(0.05)
        image = await client.screen()
        print(
            json.dumps(
                {
                    "connected": client.connected,
                    "error": client.error,
                    "face": st.face.model_dump(),
                    "screen": st.screen.model_dump(),
                    "distinct_face_packets_observed": len(seen),
                    "measured_s": round(time.monotonic() - started, 3),
                    "jpeg_sha256": hashlib.sha256(image.jpeg).hexdigest() if image else None,
                },
                indent=2,
            )
        )
    finally:
        await client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=0)
    asyncio.run(check(max(0, parser.parse_args().duration)))

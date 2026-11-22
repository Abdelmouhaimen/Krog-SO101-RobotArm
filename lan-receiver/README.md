# LAN receiver (robot PC)

Copy **this entire folder** to the robot PC (Linux). It receives two independent streams from the gaming PC: a JPEG monitor frame every ~2s and a webcam-derived angle up to 20Hz. No robot API calls, motors, or paid providers are involved.

## Physical network

Use a direct Ethernet cable or an isolated LAN. Example static interface addresses:

- Robot PC: `192.168.50.1/24`
- Gaming PC: `192.168.50.2/24`

Assign these addresses on the **Ethernet interfaces only**, and keep your usual internet/Wi-Fi default gateway intact. Check ping and allow inbound TCP **8766** only on the private Ethernet network. If your actual addresses differ, use them instead.

This receiver uses **unencrypted HTTP with a shared secret**. Do not expose its port to the internet.

## Install / run (Linux)

```bash
./install_linux.sh
./start_receiver.sh
```

The launcher asks for a token in the terminal without displaying it, unless `BRIDGE_TOKEN` is already set. Enter the **same token** in both gaming-PC UIs. The default bind address is **192.168.50.1**; to change it:

```bash
BRIDGE_HOST=192.168.50.10 ./start_receiver.sh
```

Or manually:

```bash
export BRIDGE_TOKEN='a-long-random-secret'
.venv/bin/python -m lan_bridge.receiver --host 192.168.50.1 --port 8766
```

If the receiver fails to bind, verify that the Ethernet adapter actually has the specified IP. Avoid binding `0.0.0.0` because that listens on all network adapters.

## Data endpoints

All data endpoints require the header `X-Bridge-Token: <shared-token>`. `/health` is accessible without a token and does not contain private data.

```bash
curl http://192.168.50.1:8766/health
curl -H 'X-Bridge-Token: a-long-random-secret' http://192.168.50.1:8766/v1/status
curl -H 'X-Bridge-Token: a-long-random-secret' http://192.168.50.1:8766/v1/frame --output latest.jpg
```

- `GET /v1/status` returns the freshest screen and face status. Missing/expired face data gives `found=false, angle_deg=null`.
- `GET /v1/frame` returns the latest JPEG, or HTTP 404 when none is fresh.
- Upload routes are `POST /v1/frame` and `POST /v1/angle`; do not need to call them manually.
- Screen expires after **5 seconds**; face expires after **0.5 seconds**.
- Latest observations exist **in RAM only**, not saved to disk.

## Tests (optional)

```bash
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

## Important scope

The receiver does not analyze screenshots with AI, does not feed screenshots into GPT, and **does not forward bearings to the SO-101 Follow Me API**. These integrations are future work. It does not require ManySight/MCP.

## M3C companion integration (2026-11-11)

The separate voice companion now consumes this receiver; the receiver still has no robot or LLM code.
`start_receiver.sh` restricts peers to the two configured Ethernet addresses (override
`BRIDGE_ALLOWED_PEERS` only for a reviewed private-link configuration). An ignored local `.env` can
supply `BRIDGE_TOKEN`; do not commit or log it. `/v1/frame` now includes atomic `X-Session-Id`,
`X-Sequence` and `X-Age-S` headers; `/v1/status` also includes stream session IDs.

See `../voice-companion/planning/M3C_LAN_PERCEPTION.md` for detected interfaces, startup,
Windows setup, perception tools, calibration and explicit pending real-PC validation.

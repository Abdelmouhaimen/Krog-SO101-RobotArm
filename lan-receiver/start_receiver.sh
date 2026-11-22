#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then echo "Run ./install_linux.sh first" >&2; exit 1; fi
if [[ -z "${BRIDGE_TOKEN:-}" && ! -f .env ]]; then
  read -rs -p "Shared receiver token (must match both Windows sender apps): " BRIDGE_TOKEN
  echo
  export BRIDGE_TOKEN
fi
if [[ -z "${BRIDGE_TOKEN:-}" && ! -f .env ]]; then echo "Token must not be empty" >&2; exit 1; fi
export BRIDGE_ALLOWED_PEERS="${BRIDGE_ALLOWED_PEERS:-192.168.50.1,192.168.50.2,127.0.0.1}"
exec .venv/bin/python -m lan_bridge.receiver --host "${BRIDGE_HOST:-192.168.50.1}" --port "${BRIDGE_PORT:-8766}"

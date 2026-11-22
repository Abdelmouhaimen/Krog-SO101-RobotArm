#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Launch with normal terminal colors even when invoked from an agent with NO_COLOR set.
exec env -u NO_COLOR .venv/bin/so101-voice "$@"

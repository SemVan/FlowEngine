#!/usr/bin/env bash
# Launch FlowEngine against real hardware.
# Reads .env if present; CLI flags override.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a; . ./.env; set +a
fi

exec python -m flowengine "$@"

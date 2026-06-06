#!/usr/bin/env bash
# Launch FlowEngine in mock mode (no hardware required).
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m flowengine --mock "$@"

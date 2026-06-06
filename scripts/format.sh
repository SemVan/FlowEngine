#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ruff format flowengine tests
ruff check --fix flowengine tests

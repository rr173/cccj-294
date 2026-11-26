#!/usr/bin/env bash
# Repeatable spectator admission / offline permit example.
#
#   ./demo/admission_demo.sh
#
# The Python script uses FastAPI TestClient for HTTP calls and a durable local
# SQLite gate state.  A separate Python GateTerminal instance on the same state
# file demonstrates survival across a gate restart.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
if [[ -d "$ROOT/.pydeps" ]]; then
  export PYTHONPATH="$ROOT/.pydeps:$PYTHONPATH"
fi

PYTHON_BIN="${PYTHON:-python3}"
exec "$PYTHON_BIN" "$ROOT/demo/admission_demo.py"

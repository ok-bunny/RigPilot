#!/usr/bin/env sh
# Set up an isolated Mac development environment; no services are started.
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ "$(uname -s)" != Darwin ]; then
  echo "This helper is for macOS. See README.md for other platforms." >&2
  exit 1
fi

if [ ! -e .venv ]; then
  if [ -n "${RIGPILOT_PYTHON:-}" ]; then
    PYTHON_BIN=$RIGPILOT_PYTHON
  elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN=python3.12
  elif python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PYTHON_BIN=python3
  else
    echo "Python 3.10+ is required. Set RIGPILOT_PYTHON to its executable path." >&2
    exit 1
  fi
  "$PYTHON_BIN" -c 'import sys; sys.exit("Python 3.10+ is required") if sys.version_info < (3, 10) else None'
  "$PYTHON_BIN" -m venv .venv
fi

if [ ! -x .venv/bin/python ]; then
  echo "Existing .venv is not a usable Mac environment. Preserve it and recreate it with Python 3.10+." >&2
  exit 1
fi
.venv/bin/python -c 'import sys; sys.exit("Python 3.10+ is required") if sys.version_info < (3, 10) else None'
.venv/bin/python -m pip install --no-cache-dir -r requirements-dev.txt
.venv/bin/python -m pip check
echo "Ready. Start the local controller with: sh scripts/run-server-mac.sh"

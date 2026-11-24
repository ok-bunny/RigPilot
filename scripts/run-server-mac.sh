#!/usr/bin/env sh
# Local development only: a dedicated database and a loopback listener.
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ "$(uname -s)" != Darwin ]; then
  echo "This helper is for macOS. See README.md for other platforms." >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Run sh scripts/setup-mac.sh first." >&2
  exit 1
fi

umask 077
unset PYTHONPATH PYTHONHOME
echo "Local data: $ROOT_DIR/data/mac-dev"
echo "Open http://127.0.0.1:${RIGPILOT_DEV_PORT:-8080}; press Ctrl+C to stop."
echo "The first startup prints the generated admin password once."
exec .venv/bin/python -c '
import os, sys
env = {key: value for key, value in os.environ.items()
       if not key.startswith(("RIGPILOT_", "UVICORN_"))
       and key not in {"WEB_CONCURRENCY", "GUNICORN_WORKERS"}}
env.update(RIGPILOT_DATA_DIR=sys.argv[1], RIGPILOT_DATABASE=sys.argv[1] + "/rigpilot.db")
os.execve(sys.executable, [sys.executable, "-m", "uvicorn", "server.main:app",
                         "--host", "127.0.0.1", "--port", sys.argv[2], "--workers", "1"], env)
' "$ROOT_DIR/data/mac-dev" "${RIGPILOT_DEV_PORT:-8080}"

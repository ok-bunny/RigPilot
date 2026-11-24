#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
python -m pip install -r server/requirements.txt

if [ -z "${RIGPILOT_ADMIN_PASSWORD:-}" ] && [ ! -f "${RIGPILOT_DATA_DIR:-data}/rigpilot.db" ]; then
  echo "RIGPILOT_ADMIN_PASSWORD is not set. A one-time password will be printed by the server."
fi

exec python -m uvicorn server.main:app --host "${RIGPILOT_HOST:-127.0.0.1}" --port "${RIGPILOT_PORT:-8080}"


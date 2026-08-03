#!/usr/bin/env bash
# Start Polista using the settings scripts/setup.py wrote to polista.env.
# Any extra arguments are passed through to uvicorn.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${POLISTA_ENV:-$ROOT/polista.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "No $ENV_FILE — run: python3 scripts/setup.py" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

BIND="${HTTP_BIND_ADDR:-127.0.0.1:8000}"
HOST="${BIND%:*}"
PORT="${BIND##*:}"

cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
    exec "$ROOT/.venv/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" "$@"
fi
exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"

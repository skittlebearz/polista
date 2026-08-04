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

# The SDE's bfrt_grpc is not on PyPI and is not installable into the venv, so
# TOFINO_BACKEND=bfrt needs the SDE's site-packages on PYTHONPATH.
if [[ -n "${SDE_PYTHONPATH:-}" ]]; then
    export PYTHONPATH="${SDE_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}"
fi

cd "$ROOT"

# POLISTA_PYTHON is set by setup.py when only the SDE's interpreter can import
# bfrt_grpc; the venv would fail on the pinned protobuf.
if [[ -n "${POLISTA_PYTHON:-}" ]]; then
    exec "$POLISTA_PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
fi
if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
    exec "$ROOT/.venv/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" "$@"
fi
exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"

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

# bfrt_grpc is vendored under vendor/ and app/tofino/bfrt.py adds it to sys.path,
# so TOFINO_BACKEND=bfrt needs no SDE paths. SDE_PYTHONPATH stays supported as an
# override for running inside the SDE container against its own bfrt_grpc.
if [[ -n "${SDE_PYTHONPATH:-}" ]]; then
    export PYTHONPATH="${SDE_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}"
fi

cd "$ROOT"

# POLISTA_PYTHON overrides the interpreter, for the same in-container case.
if [[ -n "${POLISTA_PYTHON:-}" ]]; then
    exec "$POLISTA_PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
fi
if [[ -x "$ROOT/.venv/bin/uvicorn" ]]; then
    exec "$ROOT/.venv/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" "$@"
fi
exec python3 -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"

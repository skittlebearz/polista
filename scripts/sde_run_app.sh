#!/usr/bin/env bash
# Portofino "final product" playground: run the web UI against the live SDE
# emulator, INSIDE the SDE container. Publishes uvicorn on 0.0.0.0:8000 (publish
# it to the host with: docker compose run --publish <host>:8000 ...).
#
# Layout (all under the /work bind mount, so persistent across runs):
#   /work/portofino        this repo (rsynced)
#   /work/portofino-out    compiled P4 artifacts
#   /work/portofino-data   port_map/mappings/auth JSON
#   /work/pydeps           pip --target with fastapi/uvicorn/... + scapy
#
# Port map: UI port u <-> device port (u-1). Device port N rides veth pair
# (2N, 2N+1) per /work/myproj/ports.json — inject/sniff on the ODD veth.
set -uo pipefail
P4SRC=/work/portofino/p4/portofino.p4
CONF=/work/portofino/p4/portofino.conf
OUT=/work/portofino-out
DATA=/work/portofino-data
SDE_PYPATH="$SDE_INSTALL/lib/python3.10/site-packages/tofino:$SDE_INSTALL/lib/python3.10/site-packages"

echo "### 1. compile (skipped if artifacts exist)"
if [[ ! -f "$OUT/portofino.tofino/pipe/tofino.bin" ]]; then
    mkdir -p "$OUT"
    bf-p4c --target tofino --arch tna -o "$OUT/portofino.tofino" "$P4SRC" 2>&1 | tail -2
fi
[[ -f "$OUT/portofino.tofino/bfrt.json" ]] || { echo "COMPILE: FAILED"; exit 1; }
echo "COMPILE: OK"

echo "### 2. data files"
mkdir -p "$DATA"
if [[ ! -f "$DATA/port_map.json" ]]; then
    python3 - <<'PY'
import json
json.dump({str(u): u - 1 for u in range(1, 9)}, open("/work/portofino-data/port_map.json", "w"))
PY
fi

echo "### 3. model + switchd"
"$SDE/run_tofino_model.sh" -p portofino -c "$CONF" -f /work/myproj/ports.json \
    >"$OUT/model.log" 2>&1 &
MODEL_PID=$!
"$SDE/run_switchd.sh" -c "$CONF" >"$OUT/switchd.log" 2>&1 &
SWITCHD_PID=$!
trap 'kill $SWITCHD_PID $MODEL_PID 2>/dev/null' EXIT

echo "### 4. wait for switchd (port 7777)"
READY=0
for i in $(seq 1 120); do
    (echo >/dev/tcp/127.0.0.1/7777) 2>/dev/null && { echo "READY after ${i}s"; READY=1; break; }
    kill -0 $SWITCHD_PID 2>/dev/null || { echo "switchd DIED"; tail -20 "$OUT/switchd.log"; break; }
    sleep 1
done
[[ $READY -eq 1 ]] || exit 1

echo "### 5. Portofino UI on 0.0.0.0:8888 (backend=bfrt)"
cd /work/portofino
PYTHONPATH="/work/pydeps:$SDE_PYPATH${PYTHONPATH:+:$PYTHONPATH}" \
PORT_COUNT=8 \
MAPPINGS_FILE="$DATA/mappings.json" \
PORT_MAP_FILE="$DATA/port_map.json" \
AUTH_FILE="$DATA/auth.json" \
SESSION_SECRET="${SESSION_SECRET:-sde-playground}" \
BOOTSTRAP_USERNAME="${BOOTSTRAP_USERNAME:-admin}" \
BOOTSTRAP_PASSWORD="${BOOTSTRAP_PASSWORD:-portofino-demo}" \
TOFINO_BACKEND=bfrt \
TOFINO_GRPC_TARGET=localhost:50052 \
TOFINO_DEVICE_ID=0 \
TOFINO_PROGRAM_NAME=portofino \
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8888

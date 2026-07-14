#!/usr/bin/env bash
# Checkpoint 5 verification, run INSIDE the SDE container (bind mount: /work).
# Expects the portofino repo staged at /work/portofino.
# Pattern follows /work/myproj/run.sh (out-of-tree program, -c with absolute paths).
set -uo pipefail
P4SRC=/work/portofino/p4/portofino.p4
CONF=/work/portofino/p4/portofino.conf
OUT=/work/portofino-out
LOGS=/work/portofino-out

echo "### 1. compile"
mkdir -p "$OUT"
bf-p4c --target tofino --arch tna -o "$OUT/portofino.tofino" "$P4SRC" 2>&1 | tail -2
[[ -f "$OUT/portofino.tofino/pipe/tofino.bin" ]] || { echo "COMPILE: FAILED"; exit 1; }
[[ -f "$OUT/portofino.tofino/bfrt.json" ]] || { echo "COMPILE: no bfrt.json"; exit 1; }
echo "COMPILE: OK"

echo "### 2. start model + switchd"
# model insists on -p even with -c (prints usage and exits otherwise)
"$SDE/run_tofino_model.sh" -p portofino -c "$CONF" -f /work/myproj/ports.json \
    >"$LOGS/model.log" 2>&1 &
MODEL_PID=$!
"$SDE/run_switchd.sh" -c "$CONF" >"$LOGS/switchd.log" 2>&1 &
SWITCHD_PID=$!

echo "### 3. wait for switchd status server (port 7777, NOT the 9999 log line)"
READY=0
for i in $(seq 1 120); do
    (echo >/dev/tcp/127.0.0.1/7777) 2>/dev/null && { echo "READY after ${i}s"; READY=1; break; }
    kill -0 $SWITCHD_PID 2>/dev/null || { echo "switchd DIED"; tail -20 "$LOGS/switchd.log"; break; }
    sleep 1
done
[[ $READY -eq 1 ]] || { kill $SWITCHD_PID $MODEL_PID 2>/dev/null; exit 1; }

echo "### 4. BFRT acceptance (coordinator-authored)"
PYTHONPATH="$SDE_INSTALL/lib/python3.10/site-packages/tofino:/work/portofino${PYTHONPATH:+:$PYTHONPATH}" \
    python3 /work/portofino/scripts/bfrt_verify.py
RC=$?
echo "verify exit code: $RC"

kill $SWITCHD_PID $MODEL_PID 2>/dev/null
wait 2>/dev/null
exit $RC

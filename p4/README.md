# Portofino P4 dataplane (Tofino 1 / TNA)

`portofino.p4` is a minimal TNA program, adopted from the verified skeleton in the
SDE environment (open-p4studio **9.13.4**, `bf-p4c` **1.2.5.10**): one table keyed
on the ingress port whose action sets the egress port. Unmapped ports hit the
`drop` default action, so an unconnected port is simply dark.

Control-plane names (from the generated `bfrt.json` — these are what
`app/tofino/bfrt.py` uses):

| | |
|---|---|
| table | `pipe.Ingress.port_map` |
| key | `ig_intr_md.ingress_port` (exact) |
| action | `Ingress.send`, data field `port` |
| default | `Ingress.drop` |

The transport is **BFRT** (`bfrt_grpc.client`, gRPC :50052), not P4Runtime — the
SDE image ships no `p4.v1` protos or Python P4Runtime client (gate decision,
2026-07-14; see `docker/SDE-HANDOFF.md` in the open-p4studio repo).

## Build

Inside the SDE container:

```bash
bf-p4c --target tofino --arch tna -o /work/portofino-out/portofino.tofino \
       /work/portofino/p4/portofino.p4
```

Note: `bf-p4c` emits `bfrt.json`; stock SDE `.conf` files reference `bf-rt.json`
(the SDE's install step renames it). `portofino.conf` here uses the name the
compiler actually produces.

## Run against the emulator (tofino-model)

Host prerequisite once per boot: `sudo sysctl -w vm.nr_hugepages=196`.

From the open-p4studio repo, with this repo staged at `work/portofino`:

```bash
docker compose -f docker/compose.yaml run --rm sde \
    bash /work/portofino/scripts/sde_verify.sh
```

That script compiles, boots model + switchd (out-of-tree `-c` conf, absolute
paths), waits for the status server on **port 7777** (the reliable ready signal —
not the "server started" log line), and runs `scripts/bfrt_verify.py`: the full
backend + Controller acceptance against the live device.

To serve the web UI against the emulator, run uvicorn **inside** the container
(the `bfrt_grpc` client and its pinned protobuf live there):

```bash
PYTHONPATH=$SDE_INSTALL/lib/python3.10/site-packages/tofino \
TOFINO_BACKEND=bfrt TOFINO_GRPC_TARGET=localhost:50052 TOFINO_PROGRAM_NAME=portofino \
    uvicorn app.main:app --host 0.0.0.0 --port 8000
```

(FastAPI deps must be installed in the container's Python 3.10; with
`TOFINO_BACKEND=fake` none of this is needed.)

"""Checkpoint 5 acceptance — authored by the coordinator, not Codex.

Runs INSIDE the SDE container against a live tofino-model + bf_switchd with the
portofino P4 program loaded. Exercises BFRTBackend directly, then the full
Controller stack on top of it (reconcile / canonical conflict / disconnect /
refresh). Requires only stdlib + bfrt_grpc — no FastAPI.

Usage (inside container, from /work):
    PYTHONPATH=$SDE_INSTALL/lib/python3.10/site-packages/tofino:/work/portofino \
        python3 /work/portofino/scripts/bfrt_verify.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.controller import Controller
from app.port_map import PortMap
from app.store import Store
from app.tofino.bfrt import BFRTBackend

PORT_COUNT = 8
UI_TO_DEV = {u: 100 + u * 3 for u in range(1, PORT_COUNT + 1)}
dev = UI_TO_DEV.__getitem__

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def main():
    backend = BFRTBackend("localhost:50052", 0, "portofino")
    try:
        # --- raw backend contract (spec section 5) ---------------------------
        check("status() True against live switchd", backend.status() is True)
        backend.clear_all()
        check("clear_all -> empty", backend.read_all() == [])

        backend.write_entry(101, 202)
        backend.write_entry(101, 205)  # upsert same key
        backend.write_entry(107, 305)
        got = dict(backend.read_all())
        check("write_entry upserts on ingress key", got == {101: 205, 107: 305}, str(got))

        backend.delete_entry(107)
        check("delete_entry by ingress key", dict(backend.read_all()) == {101: 205})
        backend.clear_all()
        check("clear_all again", backend.read_all() == [])

        # --- controller stack on the real device (spec 9 / 12) ---------------
        tmp = Path(tempfile.mkdtemp(prefix="pf-bfrt-"))
        store = Store(tmp / "mappings.json")
        store.save_state({1: 2, 3: 8}, {1: "Cam"})
        ctrl = Controller(backend, PortMap(dict(UI_TO_DEV), PORT_COUNT), store, PORT_COUNT)

        asyncio.run(ctrl.reconcile())
        check("reconcile -> healthy/in_sync", ctrl.health == "healthy" and ctrl.sync == "in_sync",
              f"{ctrl.health}/{ctrl.sync}")
        got = dict(backend.read_all())
        check("reconcile replayed JSON onto device",
              got == {dev(1): dev(2), dev(3): dev(8)}, str(got))

        res = asyncio.run(ctrl.connect(7, 5))
        check("connect 7->5 ok", res["status"] == "ok")

        res = asyncio.run(ctrl.connect(1, 5, force=False))
        would = {(r["ingress"], r["egress"]) for r in res.get("would_remove", [])}
        check("canonical conflict preview (1->2, 7->5)",
              res["status"] == "conflict" and would == {(1, 2), (7, 5)}, str(res))

        res = asyncio.run(ctrl.connect(1, 5, force=True))
        got = dict(backend.read_all())
        check("force connect applied on device",
              res["status"] == "ok" and got == {dev(1): dev(5), dev(3): dev(8)}, str(got))

        res = asyncio.run(ctrl.disconnect(1, 5))
        got = dict(backend.read_all())
        check("disconnect removed device entry", got == {dev(3): dev(8)}, str(got))

        backend.write_entry(dev(6), dev(2))  # out-of-band device write
        asyncio.run(ctrl.refresh())
        check("refresh reverse-translates device state",
              ctrl.mappings == {3: 8, 6: 2} and ctrl.labels.get(1) == "Cam", str(ctrl.mappings))

        backend.clear_all()
    finally:
        backend.close()
        backend.close()  # idempotent

    # lease trap: a SECOND client must be able to connect after close()
    b2 = BFRTBackend("localhost:50052", 0, "portofino")
    check("re-connect after close (client_id lease released)", b2.status() is True)
    b2.close()

    print(f"\n{'OK' if not failures else 'FAILED'}: {len(failures)} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

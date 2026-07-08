"""Checkpoint 1 acceptance tests — authored by the coordinator, not Codex.

These pin the module interfaces for Checkpoint 1 (spec sections 3, 5, 9, 12):

  app.port_map:
    class PortMapError(Exception)
    class PortMap:
        __init__(ui_to_dev: dict[int, int], port_count: int)
            -> raises PortMapError if non-bijective or any UI port 1..port_count missing
        to_dev(ui: int) -> int
        to_ui(dev: int) -> int
    load_port_map(path, port_count) -> PortMap   # raises PortMapError on parse/validate failure

  app.store:
    class Store:
        __init__(mappings_file: str | Path)
        load_state() -> tuple[dict[int, int], dict[int, str]]   # (mappings ui->ui, labels)
                                                                 # missing file -> ({}, {})
        save_state(mappings, labels) -> None                     # atomic: temp file + os.replace
    load_auth(path) -> dict | None                               # None if file absent

  app.tofino.fake:
    class FakeBackend:  # implements TofinoBackend protocol (spec section 5), device ports only
        status() / read_all() / write_entry(i, e) / delete_entry(i) / clear_all()

  app.controller:
    class Controller:
        __init__(backend, port_map: PortMap, store: Store, port_count: int)
        health: str   # "healthy" | "unhealthy"
        sync: str     # "in_sync" | "out_of_sync" | "partial_sync"
        mappings: dict[int, int]; labels: dict[int, str]
        async connect(ingress, egress, force=False) -> dict
            # conflict & not force -> {"status":"conflict","would_remove":[{"ingress":..,"egress":..},...]}
            # applied -> {"status":"ok","removed":[...],"added":{"ingress":..,"egress":..},"sync_state":...}
            # invalid port / no port-map entry -> raises ValueError
        async disconnect(ingress, egress) -> dict   # {"status":"ok","sync_state":...}; ValueError if
                                                    # mappings.get(ingress) != egress
        async refresh() -> dict                     # {"status":"ok","source":"tofino"}
        async reconcile() -> None                   # spec 12.1: clear_all + replay from store
        async set_label(port, label) -> dict
"""

import asyncio
import json

import pytest

from app.controller import Controller
from app.port_map import PortMap, PortMapError, load_port_map
from app.store import Store
from app.tofino.fake import FakeBackend

PORT_COUNT = 8
# Non-trivial bijection so UI/device confusion shows up in assertions.
UI_TO_DEV = {u: 100 + u * 3 for u in range(1, PORT_COUNT + 1)}


def dev(ui: int) -> int:
    return UI_TO_DEV[ui]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "mappings.json")


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def controller(backend, store):
    return Controller(backend, PortMap(dict(UI_TO_DEV), PORT_COUNT), store, PORT_COUNT)


# --- 3.2 / 9.1: canonical 1:1 conflict -------------------------------------


async def test_canonical_conflict_preview_then_force(controller, backend):
    assert (await controller.connect(1, 2))["status"] == "ok"
    assert (await controller.connect(7, 5))["status"] == "ok"

    res = await controller.connect(1, 5, force=False)
    assert res["status"] == "conflict"
    removals = {(r["ingress"], r["egress"]) for r in res["would_remove"]}
    assert removals == {(1, 2), (7, 5)}
    # No device writes on a refused conflict:
    assert dict(backend.read_all()) == {dev(1): dev(2), dev(7): dev(5)}

    res = await controller.connect(1, 5, force=True)
    assert res["status"] == "ok"
    assert {(r["ingress"], r["egress"]) for r in res["removed"]} == {(1, 2), (7, 5)}
    assert res["added"] == {"ingress": 1, "egress": 5}
    assert controller.mappings == {1: 5}
    # Device state: entry for ingress 7 explicitly deleted, ingress 1 upserted.
    assert dict(backend.read_all()) == {dev(1): dev(5)}


async def test_self_connect_allowed(controller, backend):
    res = await controller.connect(3, 3)
    assert res["status"] == "ok"
    assert controller.mappings == {3: 3}
    assert dict(backend.read_all()) == {dev(3): dev(3)}


async def test_duplicate_identical_mapping_is_noop(controller, backend):
    await controller.connect(1, 2)
    res = await controller.connect(1, 2)
    assert res["status"] == "ok"
    assert controller.mappings == {1: 2}
    assert dict(backend.read_all()) == {dev(1): dev(2)}


async def test_invalid_ports_rejected(controller):
    with pytest.raises(ValueError):
        await controller.connect(0, 1)
    with pytest.raises(ValueError):
        await controller.connect(1, PORT_COUNT + 1)


# --- 3.4: bijection ---------------------------------------------------------


def test_port_map_rejects_non_injective():
    bad = {u: 100 for u in range(1, PORT_COUNT + 1)}  # all UI ports -> device 100
    with pytest.raises(PortMapError):
        PortMap(bad, PORT_COUNT)


def test_port_map_rejects_missing_ui_port():
    partial = {u: 100 + u for u in range(1, PORT_COUNT)}  # port 8 missing
    with pytest.raises(PortMapError):
        PortMap(partial, PORT_COUNT)


def test_load_port_map_file(tmp_path):
    p = tmp_path / "port_map.json"
    p.write_text(json.dumps({str(u): d for u, d in UI_TO_DEV.items()}))
    pm = load_port_map(p, PORT_COUNT)
    assert pm.to_dev(1) == dev(1)
    assert pm.to_ui(dev(7)) == 7
    p.write_text(json.dumps({"1": 5, "2": 5}))
    with pytest.raises(PortMapError):
        load_port_map(p, 2)


# --- 3.3 / 9.2: disconnect requires the exact pair --------------------------


async def test_disconnect_requires_exact_pair(controller, backend):
    await controller.connect(1, 2)
    with pytest.raises(ValueError):
        await controller.disconnect(1, 3)
    with pytest.raises(ValueError):
        await controller.disconnect(2, 2)
    assert dict(backend.read_all()) == {dev(1): dev(2)}

    res = await controller.disconnect(1, 2)
    assert res["status"] == "ok"
    assert controller.mappings == {}
    assert backend.read_all() == []


# --- 9.1 / 9.3: persist failure degrades, never rolls back device -----------


class FailingStore(Store):
    def save_state(self, mappings, labels):
        raise OSError("disk full")


async def test_persist_failure_is_success_with_out_of_sync(backend, tmp_path):
    ctrl = Controller(
        backend, PortMap(dict(UI_TO_DEV), PORT_COUNT), FailingStore(tmp_path / "m.json"), PORT_COUNT
    )
    res = await ctrl.connect(1, 2)
    assert res["status"] == "ok"
    assert ctrl.sync == "out_of_sync"
    assert res["sync_state"] == "out_of_sync"
    # Device write happened and was NOT rolled back:
    assert dict(backend.read_all()) == {dev(1): dev(2)}
    assert ctrl.mappings == {1: 2}


# --- persistence: atomic write + round-trip ---------------------------------


async def test_state_persisted_as_ui_ports_and_round_trips(controller, store, tmp_path):
    await controller.connect(1, 2)
    await controller.set_label(1, "Camera A")
    raw = json.loads((store.path if hasattr(store, "path") else tmp_path / "mappings.json").read_text())
    assert {"ingress": 1, "egress": 2} in raw["mappings"]  # UI numbers, not device
    mappings, labels = store.load_state()
    assert mappings == {1: 2}
    assert labels[1] == "Camera A"


# --- 12.1: startup reconcile ------------------------------------------------


async def test_reconcile_clears_and_replays(backend, store):
    store.save_state({1: 2, 3: 8}, {1: "Cam"})
    backend.write_entry(999, 998)  # stale junk on the device

    ctrl = Controller(backend, PortMap(dict(UI_TO_DEV), PORT_COUNT), store, PORT_COUNT)
    await ctrl.reconcile()

    assert ctrl.health == "healthy"
    assert ctrl.sync == "in_sync"
    assert ctrl.mappings == {1: 2, 3: 8}
    assert ctrl.labels == {1: "Cam"}
    assert dict(backend.read_all()) == {dev(1): dev(2), dev(3): dev(8)}


# --- 12.2: refresh reads device, preserves labels ---------------------------


async def test_refresh_reverse_translates_and_preserves_labels(controller, backend):
    await controller.set_label(4, "Feed B")
    backend.write_entry(dev(4), dev(6))  # out-of-band device change

    res = await controller.refresh()
    assert res["status"] == "ok"
    assert controller.mappings == {4: 6}
    assert controller.labels[4] == "Feed B"


# --- lock: concurrent connects serialize ------------------------------------


async def test_concurrent_mutations_keep_1to1_invariant(controller, backend):
    await asyncio.gather(*(controller.connect(i, i, force=True) for i in range(1, PORT_COUNT + 1)))
    # Everything landed; now hammer one egress from many ingresses concurrently.
    await asyncio.gather(*(controller.connect(i, 1, force=True) for i in range(1, PORT_COUNT + 1)))
    egresses = list(controller.mappings.values())
    assert egresses.count(1) == 1  # strict 1:1 held under concurrency
    device = dict(backend.read_all())
    assert sorted(device.values()).count(dev(1)) == 1

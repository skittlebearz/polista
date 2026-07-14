"""Checkpoint 6 robustness acceptance — authored by the coordinator, not Codex.

Spec section 13 health/sync states, exercised end to end:
  - unreachable backend at startup -> unhealthy (surfaced in /health)
  - partial replay at startup      -> partial_sync
  - non-bijective port map through the real lifespan -> unhealthy, app still serves
  - JSON persist failure           -> out_of_sync surfaced in /health AND the UI
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.controller import Controller
from app.port_map import PortMap
from app.store import Store
from app.tofino.fake import FakeBackend

PORT_COUNT = 8
UI_TO_DEV = {u: 100 + u for u in range(1, PORT_COUNT + 1)}
USER, PASSWORD = "admin", "hunter2secret"
BASIC = (USER, PASSWORD)


class DeadBackend(FakeBackend):
    def status(self):
        return False


class FlakyBackend(FakeBackend):
    """write_entry fails for one specific ingress key; channel stays up."""

    def __init__(self, poison_ingress_dev):
        super().__init__()
        self.poison = poison_ingress_dev

    def write_entry(self, ingress_dev, egress_dev):
        if ingress_dev == self.poison:
            raise RuntimeError("device write failed")
        super().write_entry(ingress_dev, egress_dev)


class FailingStore(Store):
    def save_state(self, mappings, labels):
        raise OSError("disk full")


def make_controller(backend, store):
    return Controller(backend, PortMap(dict(UI_TO_DEV), PORT_COUNT), store, PORT_COUNT)


# --- controller-level state transitions --------------------------------------


async def test_unreachable_backend_reconcile_unhealthy(tmp_path):
    ctrl = make_controller(DeadBackend(), Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    assert ctrl.health == "unhealthy"


async def test_partial_replay_is_partial_sync(tmp_path):
    store = Store(tmp_path / "m.json")
    store.save_state({1: 2, 3: 8}, {})
    backend = FlakyBackend(poison_ingress_dev=UI_TO_DEV[3])
    ctrl = make_controller(backend, store)
    await ctrl.reconcile()
    assert ctrl.sync == "partial_sync"
    assert ctrl.health == "healthy"  # channel is still up


async def test_out_of_sync_recovers_on_next_successful_persist(tmp_path):
    class OnceFailingStore(Store):
        def __init__(self, path):
            super().__init__(path)
            self.fail_next = True

        def save_state(self, mappings, labels):
            if self.fail_next:
                self.fail_next = False
                raise OSError("disk full")
            super().save_state(mappings, labels)

    store = OnceFailingStore(tmp_path / "m.json")
    ctrl = make_controller(FakeBackend(), store)
    res = await ctrl.connect(1, 2)
    assert res["sync_state"] == "out_of_sync"
    res = await ctrl.connect(3, 4)
    assert res["sync_state"] == "in_sync"


# --- app-level surfacing ------------------------------------------------------


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    (tmp_path / "port_map.json").write_text(
        json.dumps({str(u): d for u, d in UI_TO_DEV.items()})
    )
    monkeypatch.setenv("PORT_COUNT", str(PORT_COUNT))
    monkeypatch.setenv("MAPPINGS_FILE", str(tmp_path / "mappings.json"))
    monkeypatch.setenv("PORT_MAP_FILE", str(tmp_path / "port_map.json"))
    monkeypatch.setenv("AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BOOTSTRAP_USERNAME", USER)
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", PASSWORD)
    monkeypatch.setenv("TOFINO_BACKEND", "fake")
    return tmp_path


def test_non_bijective_port_map_unhealthy_but_serving(app_env):
    (app_env / "port_map.json").write_text(json.dumps({"1": 5, "2": 5}))
    from app.main import create_app

    with TestClient(create_app()) as c:
        body = c.get("/health", auth=BASIC).json()
        assert body["status"] == "unhealthy"
        assert body["tofino_connected"] is False
        # app still serves the UI (degraded, not dead):
        r = c.post(
            "/ui/login",
            data={"username": USER, "password": PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code in (302, 303)
        assert "unhealthy" in c.get("/ui").text


def test_missing_port_map_file_unhealthy(app_env):
    (app_env / "port_map.json").unlink()
    from app.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/health", auth=BASIC).json()["status"] == "unhealthy"


def test_out_of_sync_surfaced_in_health_and_ui(app_env):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        # make persistence fail from now on
        app.state.controller.store = FailingStore(app_env / "mappings.json")
        r = c.post("/mappings", json={"ingress": 1, "egress": 2}, auth=BASIC)
        assert r.status_code == 200
        assert r.json()["sync_state"] == "out_of_sync"
        assert c.get("/health", auth=BASIC).json()["sync_state"] == "out_of_sync"

        c.post("/ui/login", data={"username": USER, "password": PASSWORD})
        assert "out_of_sync" in c.get("/ui").text


def test_corrupt_mappings_json_unhealthy_not_crash(app_env):
    (app_env / "mappings.json").write_text("{ not json")
    from app.main import create_app

    with TestClient(create_app()) as c:
        body = c.get("/health", auth=BASIC).json()
        assert body["status"] == "unhealthy"


# --- fan-out review findings (CP6): pinned regressions ------------------------


async def test_partial_sync_not_promoted_by_later_persist(tmp_path):
    """Finding 1: a successful set_label persist must not clear partial_sync."""
    store = Store(tmp_path / "m.json")
    store.save_state({1: 2, 3: 8}, {})
    ctrl = make_controller(FlakyBackend(poison_ingress_dev=UI_TO_DEV[3]), store)
    await ctrl.reconcile()
    assert ctrl.sync == "partial_sync"
    await ctrl.set_label(1, "Cam")
    assert ctrl.sync == "partial_sync"  # device is still missing 3->8


async def test_refresh_clears_partial_sync(tmp_path):
    """After refresh, mappings mirror the device exactly -> in_sync is honest."""
    store = Store(tmp_path / "m.json")
    store.save_state({1: 2, 3: 8}, {})
    ctrl = make_controller(FlakyBackend(poison_ingress_dev=UI_TO_DEV[3]), store)
    await ctrl.reconcile()
    assert ctrl.sync == "partial_sync"
    await ctrl.refresh()
    assert ctrl.sync == "in_sync"
    assert ctrl.mappings == {1: 2}  # the entry that actually made it to the device


async def test_partial_connect_failure_keeps_state_honest(tmp_path):
    """Finding 2: delete(other) applied, write fails -> JSON must follow the device."""

    class WriteFailsBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.fail_writes = False

        def write_entry(self, ingress_dev, egress_dev):
            if self.fail_writes:
                raise RuntimeError("channel dropped")
            super().write_entry(ingress_dev, egress_dev)

    backend = WriteFailsBackend()
    store = Store(tmp_path / "m.json")
    ctrl = make_controller(backend, store)
    await ctrl.connect(1, 2)
    await ctrl.connect(3, 4)
    backend.fail_writes = True
    with pytest.raises(Exception):
        await ctrl.connect(1, 4, force=True)  # deletes 3->4 on device, then write fails
    # device lost 3->4; in-memory/JSON state must reflect that, not resurrect it
    assert 3 not in ctrl.mappings
    mappings, _ = store.load_state()
    assert 3 not in mappings


async def test_reconcile_validates_before_touching_device(tmp_path):
    """Finding 3: invalid persisted entry -> unhealthy WITHOUT clearing the device."""
    store = Store(tmp_path / "m.json")
    store.save_state({0: 2}, {})  # port 0 invalid
    backend = FakeBackend()
    backend.write_entry(999, 998)  # pre-existing live device state
    ctrl = make_controller(backend, store)
    await ctrl.reconcile()
    assert ctrl.health == "unhealthy"
    assert dict(backend.read_all()) == {999: 998}  # untouched


async def test_unhealthy_controller_refuses_device_mutations(app_env):
    """Finding 4: identity-map fallback must not program the device."""
    (app_env / "port_map.json").write_text(json.dumps({"1": 5, "2": 5}))
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        r = c.post("/mappings", json={"ingress": 1, "egress": 2}, auth=BASIC)
        assert r.status_code in (400, 503)
        assert c.get("/mappings", auth=BASIC).json() == {"mappings": []}
        assert app.state.controller.backend.read_all() == []  # device untouched
        r = c.post("/refresh", auth=BASIC)
        assert r.status_code in (400, 503)

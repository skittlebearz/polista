"""Drift detection and the two recovery directions.

The scenario throughout: something changed the Tofino table without going
through Polista — an out-of-band `bfshell` edit, or a bf_switchd restart that
came up empty. Before this, the controller had no way to notice; the UI kept
drawing mappings that no longer existed and /health kept saying in_sync.

  - check_drift  -> report-only comparison, sets sync="drifted"
  - push         -> desired state wins (clear + replay onto the device)
  - refresh      -> device wins (already existed; retested against drift)
  - clear        -> remove everything, both sides
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.controller import BackendError, Controller
from app.port_map import PortMap
from app.store import Store
from app.tofino.fake import FakeBackend

PORT_COUNT = 8
UI_TO_DEV = {u: 100 + u for u in range(1, PORT_COUNT + 1)}
USER, PASSWORD = "admin", "hunter2secret"
BASIC = (USER, PASSWORD)


def make_controller(backend, store):
    return Controller(backend, PortMap(dict(UI_TO_DEV), PORT_COUNT), store, PORT_COUNT)


class DeadBackend(FakeBackend):
    def status(self):
        return False


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
    # The background loop would race the assertions; these tests drive
    # check_drift explicitly. Its own wiring is covered separately.
    monkeypatch.setenv("DRIFT_CHECK_INTERVAL", "0")
    return tmp_path


# --- drift detection ---------------------------------------------------------


async def test_no_drift_when_device_agrees(tmp_path):
    ctrl = make_controller(FakeBackend(), Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    assert await ctrl.check_drift() is None
    assert ctrl.sync == "in_sync"


async def test_detects_entry_deleted_out_of_band(tmp_path):
    """The bfshell-delete case: we think 1->2 exists, the table disagrees."""
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)

    backend.delete_entry(UI_TO_DEV[1])  # behind Polista's back

    drift = await ctrl.check_drift()
    assert drift["missing_on_device"] == [{"ingress": 1, "egress": 2}]
    assert ctrl.sync == "drifted"
    assert "1 missing from the switch" in ctrl.drift_summary()


async def test_detects_switchd_restart_losing_whole_table(tmp_path):
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    await ctrl.connect(3, 4)

    backend.clear_all()  # switchd came back with an empty table

    drift = await ctrl.check_drift()
    assert len(drift["missing_on_device"]) == 2
    assert ctrl.sync == "drifted"


async def test_detects_extra_and_mismatched_entries(tmp_path):
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)

    backend.write_entry(UI_TO_DEV[1], UI_TO_DEV[7])  # repointed
    backend.write_entry(UI_TO_DEV[5], UI_TO_DEV[6])  # nobody asked for this

    drift = await ctrl.check_drift()
    assert drift["mismatched"] == [{"ingress": 1, "expected": 2, "actual": 7}]
    assert drift["extra_on_device"] == [{"ingress": 5, "egress": 6}]


async def test_entry_outside_port_map_reported_not_crashed(tmp_path):
    """A foreign device port must not blow up the background loop."""
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    backend.write_entry(999, 998)

    drift = await ctrl.check_drift()
    assert drift["unmapped_on_device"] == [{"ingress_dev": 999, "egress_dev": 998}]
    assert ctrl.sync == "drifted"


async def test_drift_check_never_writes_to_the_device(tmp_path):
    """Report-only: the operator picks a direction, not the poller."""
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    backend.clear_all()

    await ctrl.check_drift()
    assert backend.read_all() == []  # still empty; nothing self-healed
    assert ctrl.mappings == {1: 2}  # and our desired state is untouched


async def test_drift_clears_once_device_agrees_again(tmp_path):
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    backend.delete_entry(UI_TO_DEV[1])
    await ctrl.check_drift()
    assert ctrl.sync == "drifted"

    backend.write_entry(UI_TO_DEV[1], UI_TO_DEV[2])  # someone put it back
    assert await ctrl.check_drift() is None
    assert ctrl.sync == "in_sync"
    assert ctrl.drift is None


async def test_drift_does_not_overwrite_out_of_sync(tmp_path):
    """out_of_sync is about the state file, which a device read cannot clear."""
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    ctrl.sync = "out_of_sync"
    backend.clear_all()

    await ctrl.check_drift()
    assert ctrl.sync == "out_of_sync"


# --- push: desired state wins ------------------------------------------------


async def test_push_repairs_a_wiped_table(tmp_path):
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    await ctrl.connect(3, 4)
    backend.clear_all()

    result = await ctrl.push()
    assert result["written"] == 2
    assert dict(backend.read_all()) == {
        UI_TO_DEV[1]: UI_TO_DEV[2],
        UI_TO_DEV[3]: UI_TO_DEV[4],
    }
    assert ctrl.sync == "in_sync"
    assert ctrl.drift is None


async def test_push_removes_foreign_entries(tmp_path):
    """Push is clear-then-replay, so the table ends up exactly ours."""
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    backend.write_entry(UI_TO_DEV[5], UI_TO_DEV[6])

    await ctrl.push()
    assert dict(backend.read_all()) == {UI_TO_DEV[1]: UI_TO_DEV[2]}


async def test_push_recovers_from_unreachable_at_startup(tmp_path):
    """A switch that was down at boot is the main reason to click Push."""
    store = Store(tmp_path / "m.json")
    store.save_state({1: 2}, {})

    class Revivable(FakeBackend):
        alive = False

        def status(self):
            return self.alive

    backend = Revivable()
    ctrl = make_controller(backend, store)
    await ctrl.reconcile()
    assert ctrl.health == "unhealthy"

    backend.alive = True  # switchd came back
    await ctrl.push()
    assert ctrl.health == "healthy"
    assert dict(backend.read_all()) == {UI_TO_DEV[1]: UI_TO_DEV[2]}


async def test_push_still_refused_when_port_map_is_wrong(tmp_path):
    """An unrecoverable reason must not be clickable-past into a bad program."""
    ctrl = make_controller(FakeBackend(), Store(tmp_path / "m.json"))
    ctrl.mark_unhealthy("port map is unusable", recoverable=False)
    with pytest.raises(ValueError, match="cannot recover automatically"):
        await ctrl.push()


async def test_push_reports_backend_failure(tmp_path):
    ctrl = make_controller(DeadBackend(), Store(tmp_path / "m.json"))
    with pytest.raises(BackendError):
        await ctrl.push()


# --- clear -------------------------------------------------------------------


async def test_clear_empties_device_and_desired_state(tmp_path):
    backend = FakeBackend()
    store = Store(tmp_path / "m.json")
    ctrl = make_controller(backend, store)
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    await ctrl.connect(3, 4)

    result = await ctrl.clear()
    assert result["removed"] == 2
    assert backend.read_all() == []
    assert ctrl.mappings == {}
    assert store.load_state()[0] == {}


async def test_clear_keeps_labels(tmp_path):
    """Clearing cross-connects must not wipe the operator's port names."""
    ctrl = make_controller(FakeBackend(), Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.set_label(1, "Camera A")
    await ctrl.connect(1, 2)

    await ctrl.clear()
    assert ctrl.labels == {1: "Camera A"}


async def test_clear_refused_when_unhealthy(tmp_path):
    ctrl = make_controller(FakeBackend(), Store(tmp_path / "m.json"))
    ctrl.mark_unhealthy("switch is gone")
    with pytest.raises(ValueError):
        await ctrl.clear()


# --- refresh against drift ---------------------------------------------------


async def test_refresh_adopts_the_device_and_clears_drift(tmp_path):
    backend = FakeBackend()
    ctrl = make_controller(backend, Store(tmp_path / "m.json"))
    await ctrl.reconcile()
    await ctrl.connect(1, 2)
    backend.write_entry(UI_TO_DEV[5], UI_TO_DEV[6])
    await ctrl.check_drift()
    assert ctrl.sync == "drifted"

    await ctrl.refresh()
    assert ctrl.mappings == {1: 2, 5: 6}
    assert ctrl.sync == "in_sync"
    assert ctrl.drift is None


# --- HTTP surface ------------------------------------------------------------


def test_health_and_drift_endpoints_expose_the_delta(app_env):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/mappings", json={"ingress": 1, "egress": 2}, auth=BASIC)
        assert c.get("/health", auth=BASIC).json().get("drift") is None

        app.state.controller.backend.clear_all()

        body = c.get("/drift", auth=BASIC).json()
        assert body["drift"]["missing_on_device"] == [{"ingress": 1, "egress": 2}]
        assert body["sync_state"] == "drifted"

        health = c.get("/health", auth=BASIC).json()
        assert health["sync_state"] == "drifted"
        assert health["drift"]["missing_on_device"] == [{"ingress": 1, "egress": 2}]


def test_push_endpoint_repairs_the_table(app_env):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/mappings", json={"ingress": 1, "egress": 2}, auth=BASIC)
        backend = app.state.controller.backend
        backend.clear_all()

        r = c.post("/push", auth=BASIC)
        assert r.status_code == 200
        assert r.json()["written"] == 1
        assert dict(backend.read_all()) == {UI_TO_DEV[1]: UI_TO_DEV[2]}
        assert c.get("/health", auth=BASIC).json()["sync_state"] == "in_sync"


def test_clear_endpoint(app_env):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post("/mappings", json={"ingress": 1, "egress": 2}, auth=BASIC)
        r = c.post("/clear", auth=BASIC)
        assert r.status_code == 200 and r.json()["removed"] == 1
        assert c.get("/mappings", auth=BASIC).json()["mappings"] == []


def test_new_endpoints_require_auth(app_env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        for method, path in (("post", "/push"), ("post", "/clear"), ("get", "/drift")):
            assert getattr(c, method)(path).status_code == 401


# --- UI ----------------------------------------------------------------------


def _login(client):
    client.post("/ui/login", data={"username": USER, "password": PASSWORD})


def test_menu_replaces_the_bare_refresh_button(app_env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        _login(c)
        page = c.get("/ui").text
        assert 'class="menu-trigger"' in page
        assert "Push to switch" in page
        assert "Pull from switch" in page
        assert "Clear all cross-connects" in page


def test_clear_requires_confirmation_first(app_env):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        _login(c)
        c.post("/ui/mappings", data={"ingress": 1, "egress": 2, "force": "false"})

        confirm = c.post("/ui/clear/confirm")
        assert "Clear all cross-connects?" in confirm.text
        assert "cannot be undone" in confirm.text
        # Asking for the dialog must not have touched anything.
        assert app.state.controller.mappings == {1: 2}

        c.post("/ui/clear")
        assert app.state.controller.mappings == {}


def test_ui_push_and_status_poll(app_env):
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        _login(c)
        c.post("/ui/mappings", data={"ingress": 1, "egress": 2, "force": "false"})
        app.state.controller.backend.clear_all()

        # The poll endpoint runs a drift check and renders the warning.
        status = c.get("/ui/status").text
        assert "drifted" in status
        assert "missing from the switch" in status

        c.post("/ui/push")
        assert dict(app.state.controller.backend.read_all()) == {
            UI_TO_DEV[1]: UI_TO_DEV[2]
        }
        assert "in_sync" in c.get("/ui/status").text


def test_dialog_container_survives_a_full_panel_swap(app_env):
    """#dialog must live outside #panel.

    Push/Pull/Clear all swap #panel wholesale. When #dialog was nested inside
    it, the first such action deleted the container and every later
    confirmation died with htmx:targetError — including the Clear warning.
    """
    from app.main import create_app

    with TestClient(create_app()) as c:
        _login(c)
        page = c.get("/ui").text
        # The container sits after the panel closes, so a #panel swap cannot
        # take it along.
        assert page.index('<div id="dialog"></div>') > page.index('<div id="panel">')

        # A panel-swapping response replaces the panel and must not ship its own
        # dialog element: it may only empty the existing one out of band.
        panel_swap = c.post("/ui/push").text
        assert 'id="panel"' in panel_swap
        assert '<div id="dialog"></div>' not in panel_swap

        # The confirmation still renders after a panel swap, which is exactly
        # what broke before.
        assert "Clear all cross-connects?" in c.post("/ui/clear/confirm").text


def test_ui_actions_require_a_session(app_env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        for path in ("/ui/push", "/ui/clear", "/ui/clear/confirm"):
            assert c.post(path).status_code == 401
        assert c.get("/ui/status").status_code == 401

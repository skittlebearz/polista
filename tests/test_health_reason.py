"""Unhealthy states must explain themselves.

A bare "unhealthy" makes a missing SDE module, an unreachable switchd, a bad
port map, and an unreadable state file look identical -- which is exactly the
debugging dead end this covers. Every path that sets unhealthy must leave a
reason on the controller, surface it on /health, and repeat it in the 400 the
UI gets back.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.controller import Controller
from app.port_map import PortMap
from app.store import Store
from app.tofino.fake import FakeBackend

PORT_COUNT = 4
UI_TO_DEV = {u: u - 1 for u in range(1, PORT_COUNT + 1)}
USER, PASSWORD = "admin", "hunter2secret"
BASIC = (USER, PASSWORD)


class UnreachableBackend(FakeBackend):
    """Reports down without raising, the way BFRTBackend.status() does."""

    def __init__(self, last_error=None, grpc_target=None):
        super().__init__()
        self.last_error = last_error
        if grpc_target:
            self.grpc_target = grpc_target

    def status(self) -> bool:
        return False


def make_controller(tmp_path, backend):
    store = Store(str(tmp_path / "mappings.json"))
    return Controller(backend, PortMap(UI_TO_DEV, PORT_COUNT), store, PORT_COUNT)


async def test_missing_bfrt_grpc_names_the_sde_and_pythonpath(tmp_path):
    """The failure every first-time bfrt user hits must name the actual fix."""
    backend = UnreachableBackend(
        last_error=ModuleNotFoundError("No module named 'bfrt_grpc'"),
        grpc_target="127.0.0.1:50052",
    )
    controller = make_controller(tmp_path, backend)
    await controller.reconcile()

    assert controller.health == "unhealthy"
    reason = controller.health_reason
    assert "bfrt_grpc" in reason
    assert "PYTHONPATH" in reason


async def test_unreachable_switchd_names_the_target(tmp_path):
    backend = UnreachableBackend(grpc_target="10.0.0.9:50052")
    controller = make_controller(tmp_path, backend)
    await controller.reconcile()

    assert controller.health == "unhealthy"
    assert "10.0.0.9:50052" in controller.health_reason
    assert "bf_switchd" in controller.health_reason


async def test_a_raised_status_error_is_reported_verbatim(tmp_path):
    class ExplodingBackend(FakeBackend):
        grpc_target = "127.0.0.1:50052"

        def status(self):
            raise RuntimeError("grpc handshake failed")

    controller = make_controller(tmp_path, ExplodingBackend())
    await controller.reconcile()

    assert controller.health == "unhealthy"
    assert "grpc handshake failed" in controller.health_reason


async def test_saved_mappings_outside_the_port_map_are_explained(tmp_path):
    state = tmp_path / "mappings.json"
    state.write_text(
        json.dumps({"mappings": [{"ingress": 99, "egress": 1}], "labels": {}})
    )
    controller = make_controller(tmp_path, FakeBackend())
    await controller.reconcile()

    assert controller.health == "unhealthy"
    assert "port map" in controller.health_reason


async def test_recovery_clears_the_reason(tmp_path):
    controller = make_controller(tmp_path, UnreachableBackend())
    await controller.reconcile()
    assert controller.health_reason is not None

    controller.backend = FakeBackend()
    await controller.reconcile()
    assert controller.health == "healthy"
    assert controller.health_reason is None


async def test_mutations_repeat_the_reason_not_just_unhealthy(tmp_path):
    controller = make_controller(tmp_path, UnreachableBackend(grpc_target="127.0.0.1:50052"))
    await controller.reconcile()

    with pytest.raises(ValueError, match="unreachable"):
        await controller.refresh()
    with pytest.raises(ValueError, match="unreachable"):
        await controller.connect(1, 2)


# ------------------------------------------------------------------- through HTTP


@pytest.fixture
def unhealthy_client(tmp_path, monkeypatch):
    """A real app whose port map cannot be loaded -- unhealthy via lifespan."""
    (tmp_path / "port_map.json").write_text(json.dumps({"1": 0, "2": 0}))  # not injective
    monkeypatch.setenv("PORT_COUNT", "2")
    monkeypatch.setenv("MAPPINGS_FILE", str(tmp_path / "mappings.json"))
    monkeypatch.setenv("PORT_MAP_FILE", str(tmp_path / "port_map.json"))
    monkeypatch.setenv("AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BOOTSTRAP_USERNAME", USER)
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", PASSWORD)
    monkeypatch.setenv("TOFINO_BACKEND", "fake")

    import app.main as main

    with TestClient(main.create_app()) as client:
        yield client


def test_health_endpoint_exposes_the_reason(unhealthy_client):
    body = unhealthy_client.get("/health", auth=BASIC).json()
    assert body["status"] == "unhealthy"
    assert body["tofino_connected"] is False
    assert "port map" in body["reason"]


def test_healthy_response_has_no_reason_key(tmp_path, monkeypatch):
    (tmp_path / "port_map.json").write_text(json.dumps({"1": 0, "2": 1}))
    monkeypatch.setenv("PORT_COUNT", "2")
    monkeypatch.setenv("MAPPINGS_FILE", str(tmp_path / "mappings.json"))
    monkeypatch.setenv("PORT_MAP_FILE", str(tmp_path / "port_map.json"))
    monkeypatch.setenv("AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BOOTSTRAP_USERNAME", USER)
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", PASSWORD)
    monkeypatch.setenv("TOFINO_BACKEND", "fake")

    import app.main as main

    with TestClient(main.create_app()) as client:
        body = client.get("/health", auth=BASIC).json()
        assert body["status"] == "healthy"
        assert "reason" not in body


def test_refresh_400_carries_the_reason(unhealthy_client):
    response = unhealthy_client.post("/refresh", auth=BASIC)
    assert response.status_code == 400
    assert "port map" in response.json()["detail"]


def test_ui_status_dot_shows_the_reason(unhealthy_client):
    unhealthy_client.post("/login", data={"username": USER, "password": PASSWORD})
    page = unhealthy_client.get("/ui")
    assert page.status_code == 200
    assert "port map" in page.text

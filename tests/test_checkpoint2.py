"""Checkpoint 2 acceptance tests — authored by the coordinator, not Codex.

Pinned interfaces for Checkpoint 2 (spec sections 2, 10.3, 11):

  app.auth:
    ensure_auth_file(config) -> None      # bootstrap AUTH_FILE from BOOTSTRAP_* if absent (argon2)
    verify_credentials(username, password, auth_path) -> bool
    require_user                          # FastAPI dependency: accepts session cookie OR HTTP Basic,
                                          # returns username, raises HTTPException(401) otherwise

  app.main:
    create_app() -> FastAPI               # reads env via load_config() at call time;
                                          # lifespan: ensure_auth_file + controller.reconcile();
                                          # SessionMiddleware; controller at app.state.controller

  Routes per spec Section 11 (JSON, auth required except /login):
    GET /health, GET /ports, GET /mappings, POST /mappings (409 on unforced conflict),
    DELETE /mappings, POST /refresh, GET /labels, PUT /labels/{port},
    POST /login, POST /logout (session), GET /session
"""

import json

import pytest
from fastapi.testclient import TestClient

PORT_COUNT = 8
USER = "admin"
PASSWORD = "hunter2secret"
BASIC = (USER, PASSWORD)


@pytest.fixture
def client(tmp_path, monkeypatch):
    port_map = {str(u): 100 + u for u in range(1, PORT_COUNT + 1)}
    (tmp_path / "port_map.json").write_text(json.dumps(port_map))
    monkeypatch.setenv("PORT_COUNT", str(PORT_COUNT))
    monkeypatch.setenv("MAPPINGS_FILE", str(tmp_path / "mappings.json"))
    monkeypatch.setenv("PORT_MAP_FILE", str(tmp_path / "port_map.json"))
    monkeypatch.setenv("AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BOOTSTRAP_USERNAME", USER)
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", PASSWORD)
    monkeypatch.setenv("TOFINO_BACKEND", "fake")

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.auth_file = tmp_path / "auth.json"
        yield c


# --- auth ---------------------------------------------------------------


def test_unauthenticated_is_401(client):
    for method, path in [("GET", "/mappings"), ("GET", "/ports"), ("POST", "/refresh")]:
        r = client.request(method, path)
        assert r.status_code == 401, (method, path, r.status_code)


def test_basic_auth_works(client):
    r = client.get("/health", auth=BASIC)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["tofino_connected"] is True
    assert body["sync_state"] == "in_sync"


def test_basic_auth_wrong_password_rejected(client):
    assert client.get("/mappings", auth=(USER, "wrong")).status_code == 401


def test_session_login_logout_cycle(client):
    assert client.post("/login", data={"username": USER, "password": "nope"}).status_code == 401

    r = client.post("/login", data={"username": USER, "password": PASSWORD})
    assert r.status_code in (200, 303)

    r = client.get("/session")
    assert r.status_code == 200
    assert USER in json.dumps(r.json())

    assert client.get("/mappings").status_code == 200  # session cookie, no Basic

    client.post("/logout")
    assert client.get("/mappings").status_code == 401


def test_auth_file_bootstrapped_argon2_never_plaintext(client):
    raw = client.auth_file.read_text()
    data = json.loads(raw)
    assert data["username"] == USER
    assert data["password_hash"].startswith("$argon2")
    assert PASSWORD not in raw


# --- REST semantics (spec 11) --------------------------------------------


def test_ports_shape(client):
    body = client.get("/ports", auth=BASIC).json()
    assert body["port_count"] == PORT_COUNT
    assert len(body["ports"]) == PORT_COUNT
    assert body["ports"][0] == {"port": 1, "label": ""} or body["ports"][0]["port"] == 1


def test_mapping_crud_and_conflict_409(client):
    assert client.post("/mappings", json={"ingress": 1, "egress": 2}, auth=BASIC).status_code == 200
    assert client.post("/mappings", json={"ingress": 7, "egress": 5}, auth=BASIC).status_code == 200

    r = client.post("/mappings", json={"ingress": 1, "egress": 5, "force": False}, auth=BASIC)
    assert r.status_code == 409
    body = r.json()
    assert body["conflict"] is True
    assert {(x["ingress"], x["egress"]) for x in body["would_remove"]} == {(1, 2), (7, 5)}

    r = client.post("/mappings", json={"ingress": 1, "egress": 5, "force": True}, auth=BASIC)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["added"] == {"ingress": 1, "egress": 5}
    assert body["sync_state"] == "in_sync"

    assert client.get("/mappings", auth=BASIC).json() == {"mappings": [{"ingress": 1, "egress": 5}]}

    r = client.request("DELETE", "/mappings", json={"ingress": 1, "egress": 5}, auth=BASIC)
    assert r.status_code == 200
    assert client.get("/mappings", auth=BASIC).json() == {"mappings": []}


def test_delete_wrong_pair_is_400(client):
    client.post("/mappings", json={"ingress": 1, "egress": 2}, auth=BASIC)
    r = client.request("DELETE", "/mappings", json={"ingress": 1, "egress": 3}, auth=BASIC)
    assert r.status_code == 400


def test_invalid_port_is_400(client):
    r = client.post("/mappings", json={"ingress": 0, "egress": 99}, auth=BASIC)
    assert r.status_code in (400, 422)


def test_labels_roundtrip(client):
    r = client.put("/labels/3", json={"label": "Camera A"}, auth=BASIC)
    assert r.status_code == 200
    assert client.get("/labels", auth=BASIC).json()["3"] == "Camera A"


def test_refresh(client):
    r = client.post("/refresh", auth=BASIC)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "source": "tofino"}


def test_startup_reconcile_replays_persisted_mappings(tmp_path, monkeypatch):
    port_map = {str(u): 100 + u for u in range(1, PORT_COUNT + 1)}
    (tmp_path / "port_map.json").write_text(json.dumps(port_map))
    (tmp_path / "mappings.json").write_text(
        json.dumps({"mappings": [{"ingress": 3, "egress": 4}], "labels": {"3": "Cam"}})
    )
    monkeypatch.setenv("PORT_COUNT", str(PORT_COUNT))
    monkeypatch.setenv("MAPPINGS_FILE", str(tmp_path / "mappings.json"))
    monkeypatch.setenv("PORT_MAP_FILE", str(tmp_path / "port_map.json"))
    monkeypatch.setenv("AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BOOTSTRAP_USERNAME", USER)
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", PASSWORD)
    monkeypatch.setenv("TOFINO_BACKEND", "fake")

    from app.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/mappings", auth=BASIC).json() == {"mappings": [{"ingress": 3, "egress": 4}]}
        assert c.get("/health", auth=BASIC).json()["sync_state"] == "in_sync"
        assert c.get("/labels", auth=BASIC).json() == {"3": "Cam"}

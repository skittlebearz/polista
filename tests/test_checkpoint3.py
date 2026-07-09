"""Checkpoint 3 acceptance tests (server side) — authored by the coordinator, not Codex.

Pinned UI contract (spec Section 8):

  Routes (app/routes/ui.py; session auth — unauthenticated GET /ui redirects to
  /ui/login, unauthenticated HTMX mutations get 401):
    GET  /ui                    -> full page: base.html wrapping panel.html
    GET  /ui/login              -> login.html
    POST /ui/login (form)       -> set session, 303 redirect to /ui (401 + page on bad creds)
    POST /ui/logout             -> clear session, redirect to /ui/login
    POST /ui/mappings (form ingress/egress/force) ->
         no conflict: _ports.html region (HTML containing all port elements)
         conflict:    _conflict.html dialog with hidden ingress/egress inputs and a
                      force=true confirm button; response carries HX-Retarget: #dialog
    POST /ui/mappings/delete (form ingress/egress) -> _ports.html region
    PUT  /ui/labels/{port} (form label)            -> _ports.html region
    POST /ui/refresh                               -> panel.html region

  Markup contract (consumed by lines.js):
    - ports region root has id="ports"; each ingress element: class contains "port",
      data-side="ingress", data-port="<n>", data-mapped-egress="<n or empty>";
      each egress element: data-side="egress", data-port="<n>".
    - panel has an <svg id="lines"> overlay and a health/sync indicator element.
    - base.html loads /static/htmx.min.js, /static/lines.js, /static/app.css.
    - mutation responses include an out-of-band-clearable dialog container id="dialog".
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

PORT_COUNT = 8
USER = "admin"
PASSWORD = "hunter2secret"


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

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


@pytest.fixture
def authed(client):
    r = client.post("/ui/login", data={"username": USER, "password": PASSWORD})
    assert r.status_code in (302, 303)
    return client


def ingress_attr(html: str, port: int) -> str:
    """data-mapped-egress value of ingress element <port>."""
    pat = re.compile(
        r'data-side="ingress"[^>]*data-port="%d"[^>]*data-mapped-egress="(\d*)"' % port
    )
    alt = re.compile(
        r'data-mapped-egress="(\d*)"[^>]*data-side="ingress"[^>]*data-port="%d"' % port
    )
    for tag in re.findall(r"<[^>]+data-side=\"ingress\"[^>]+>", html):
        if f'data-port="{port}"' in tag:
            m = re.search(r'data-mapped-egress="(\d*)"', tag)
            if m:
                return m.group(1)
    m = pat.search(html) or alt.search(html)
    assert m, f"ingress element for port {port} with data-mapped-egress not found"
    return m.group(1)


# --- auth / navigation -------------------------------------------------------


def test_ui_requires_session_redirects_to_login(client):
    r = client.get("/ui")
    assert r.status_code in (302, 303, 307)
    assert "/ui/login" in r.headers["location"]


def test_login_page_renders(client):
    r = client.get("/ui/login")
    assert r.status_code == 200
    assert "password" in r.text.lower()


def test_bad_login_rejected(client):
    r = client.post("/ui/login", data={"username": USER, "password": "nope"})
    assert r.status_code == 401


def test_htmx_mutation_unauthenticated_401(client):
    r = client.post("/ui/mappings", data={"ingress": 1, "egress": 2, "force": "false"})
    assert r.status_code == 401


# --- panel markup contract ---------------------------------------------------


def test_panel_markup(authed):
    r = authed.get("/ui")
    assert r.status_code == 200
    html = r.text
    assert 'id="ports"' in html
    assert 'id="lines"' in html
    assert 'id="dialog"' in html
    assert "/static/htmx.min.js" in html and "/static/lines.js" in html and "/static/app.css" in html
    for p in range(1, PORT_COUNT + 1):
        assert f'data-port="{p}"' in html
    assert html.count('data-side="ingress"') == PORT_COUNT
    assert html.count('data-side="egress"') == PORT_COUNT
    assert ingress_attr(html, 1) == ""
    assert "in_sync" in html  # health/sync indicator


# --- interaction flows (spec 8.2) ---------------------------------------------


def test_connect_no_conflict_returns_ports_region(authed):
    r = authed.post("/ui/mappings", data={"ingress": 1, "egress": 2, "force": "false"})
    assert r.status_code == 200
    assert 'data-side="ingress"' in r.text
    assert ingress_attr(r.text, 1) == "2"


def test_conflict_returns_dialog_with_force_confirm(authed):
    authed.post("/ui/mappings", data={"ingress": 1, "egress": 2, "force": "false"})
    authed.post("/ui/mappings", data={"ingress": 7, "egress": 5, "force": "false"})

    r = authed.post("/ui/mappings", data={"ingress": 1, "egress": 5, "force": "false"})
    assert r.status_code == 200
    html = r.text
    assert r.headers.get("hx-retarget", r.headers.get("HX-Retarget", "")) == "#dialog"
    # names what will be removed:
    assert "1" in html and "2" in html and "7" in html and "5" in html
    # hidden inputs + force confirm re-post:
    assert 'name="ingress"' in html and 'name="egress"' in html
    assert 'name="force"' in html and 'value="true"' in html
    assert "/ui/mappings" in html

    r = authed.post("/ui/mappings", data={"ingress": 1, "egress": 5, "force": "true"})
    assert r.status_code == 200
    assert ingress_attr(r.text, 1) == "5"
    assert ingress_attr(r.text, 7) == ""


def test_disconnect_pair(authed):
    authed.post("/ui/mappings", data={"ingress": 1, "egress": 2, "force": "false"})
    r = authed.post("/ui/mappings/delete", data={"ingress": 1, "egress": 2})
    assert r.status_code == 200
    assert ingress_attr(r.text, 1) == ""


def test_label_edit_roundtrip(authed):
    r = authed.put("/ui/labels/3", data={"label": "Camera A"})
    assert r.status_code == 200
    assert "Camera A" in r.text
    assert "Camera A" in authed.get("/ui").text


def test_refresh_returns_panel(authed):
    authed.post("/ui/mappings", data={"ingress": 4, "egress": 6, "force": "false"})
    r = authed.post("/ui/refresh")
    assert r.status_code == 200
    assert ingress_attr(r.text, 4) == "6"


def test_logout(authed):
    authed.post("/ui/logout")
    r = authed.get("/ui")
    assert r.status_code in (302, 303, 307)

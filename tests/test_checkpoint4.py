"""Checkpoint 4 acceptance tests — authored by the coordinator, not Codex.

Pinned client interface (spec Section 17 step 4): client/portofino_client.py,
stdlib-only (urllib), no direct gRPC, HTTP Basic auth.

  class PortofinoClient:
      __init__(base_url: str, username: str, password: str)
      get_health() -> dict
      get_ports() -> dict
      get_mappings() -> list[dict]          # the "mappings" list
      connect(ingress, egress, force=False) -> dict
          # 409 conflict with force=False raises ConflictError carrying .would_remove
      disconnect(ingress, egress) -> dict
      refresh() -> dict
      get_labels() -> dict
      set_label(port, label) -> dict
  class PortofinoError(Exception)           # non-2xx other than the 409 conflict case
  class ConflictError(PortofinoError)       # .would_remove: list[dict]

Runs against a real uvicorn subprocess on the fake backend.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PORT_COUNT = 8
USER = "admin"
PASSWORD = "clientpw123"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cp4")
    (tmp / "port_map.json").write_text(
        json.dumps({str(u): 100 + u for u in range(1, PORT_COUNT + 1)})
    )
    port = _free_port()
    env = dict(
        os.environ,
        PORT_COUNT=str(PORT_COUNT),
        MAPPINGS_FILE=str(tmp / "mappings.json"),
        PORT_MAP_FILE=str(tmp / "port_map.json"),
        AUTH_FILE=str(tmp / "auth.json"),
        SESSION_SECRET="cp4-secret",
        BOOTSTRAP_USERNAME=USER,
        BOOTSTRAP_PASSWORD=PASSWORD,
        TOFINO_BACKEND="fake",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    import urllib.request, urllib.error

    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/ui/login", timeout=1)
            break
        except urllib.error.HTTPError:
            break  # server is up, any HTTP status counts
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read().decode())
            time.sleep(0.2)
    else:
        proc.send_signal(signal.SIGTERM)
        raise RuntimeError("server did not start")
    yield base
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)


@pytest.fixture
def api(server):
    sys.path.insert(0, str(REPO / "client"))
    from portofino_client import PortofinoClient

    c = PortofinoClient(server, USER, PASSWORD)
    # clean slate per test
    for m in c.get_mappings():
        c.disconnect(m["ingress"], m["egress"])
    return c


def test_health_and_ports(api):
    h = api.get_health()
    assert h["status"] == "healthy" and h["sync_state"] == "in_sync"
    p = api.get_ports()
    assert p["port_count"] == PORT_COUNT and len(p["ports"]) == PORT_COUNT


def test_connect_disconnect_cycle(api):
    res = api.connect(1, 2)
    assert res["status"] == "ok"
    assert api.get_mappings() == [{"ingress": 1, "egress": 2}]
    api.disconnect(1, 2)
    assert api.get_mappings() == []


def test_conflict_raises_then_force_applies(api):
    from portofino_client import ConflictError

    api.connect(1, 2)
    api.connect(7, 5)
    with pytest.raises(ConflictError) as exc:
        api.connect(1, 5)
    assert {(x["ingress"], x["egress"]) for x in exc.value.would_remove} == {(1, 2), (7, 5)}

    res = api.connect(1, 5, force=True)
    assert res["status"] == "ok"
    assert api.get_mappings() == [{"ingress": 1, "egress": 5}]


def test_bad_credentials_raise(server):
    from portofino_client import PortofinoClient, PortofinoError

    bad = PortofinoClient(server, USER, "wrong")
    with pytest.raises(PortofinoError):
        bad.get_mappings()


def test_labels_and_refresh(api):
    api.set_label(3, "Feed B")
    assert api.get_labels()["3"] == "Feed B"
    assert api.refresh() == {"status": "ok", "source": "tofino"}


def test_invalid_port_raises(api):
    from portofino_client import PortofinoError

    with pytest.raises(PortofinoError):
        api.connect(0, 99)

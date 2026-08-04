"""Setup-wizard acceptance — scripts/setup.py must produce a config the app boots on.

The wizard is the first thing a new user runs, so the contract under test is:
  - answers -> a valid port map (bijective, covers 1..PORT_COUNT) and polista.env
  - the generated env actually starts the app healthy through the real lifespan
  - bad input is rejected rather than written out
  - re-running preserves existing cross-connects and refuses to silently
    replace a legacy auth file
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def load_setup():
    spec = importlib.util.spec_from_file_location("polista_setup", ROOT / "scripts" / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load_setup()


@pytest.fixture
def prompter():
    return setup.Prompter(assume_yes=True)


# --------------------------------------------------------------------- validators


@pytest.mark.parametrize("value", ["abc", "0", "-3", "", "4.5"])
def test_positive_int_rejects_bad_port_counts(value):
    with pytest.raises(ValueError):
        setup.positive_int(value)


def test_positive_int_accepts_a_count():
    assert setup.positive_int("8") == 8


@pytest.mark.parametrize("value", ["9999999", "127.0.0.1", "host:0", "host:70000", ":8000"])
def test_bind_addr_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        setup.bind_addr(value)


def test_bind_addr_accepts_host_port():
    assert setup.bind_addr("0.0.0.0:8888") == "0.0.0.0:8888"


def test_custom_port_map_rejects_duplicates():
    with pytest.raises(ValueError, match="unique"):
        setup.custom_port_map("5 5 5", 3)


def test_custom_port_map_rejects_wrong_arity():
    with pytest.raises(ValueError, match="expected 4"):
        setup.custom_port_map("1 2 3", 4)


def test_custom_port_map_accepts_commas_and_spaces():
    assert setup.custom_port_map("9, 8, 7", 3) == {"1": 9, "2": 8, "3": 7}


# ------------------------------------------------------------------------- files


def test_shell_quote_survives_a_password_with_a_quote():
    quoted = setup.shell_quote("it's a 'secret'")
    # `sh -c` must round-trip the value byte for byte.
    import subprocess

    out = subprocess.run(
        ["sh", "-c", f"printf %s {quoted}"], capture_output=True, text=True, check=True
    )
    assert out.stdout == "it's a 'secret'"


def test_backup_never_clobbers_an_earlier_backup(tmp_path):
    target = tmp_path / "auth.json"
    target.write_text("first")
    first = setup.backup(target)

    target.write_text("second")
    second = setup.backup(target)

    assert first != second
    assert first.read_text() == "first"
    assert second.read_text() == "second"


# ------------------------------------------------------------------------ wizard


def run_wizard(tmp_path, monkeypatch, answers, prompter):
    """Drive collect_answers/apply against tmp_path as the project root."""
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    monkeypatch.setattr(setup, "ENV_FILE", tmp_path / "polista.env")

    scripted = list(answers)

    def fake_choice(self, question, options, default_index=0):
        return scripted.pop(0)

    def fake_text(self, question, default, validate=None):
        value = scripted.pop(0)
        return validate(value) if validate else value

    def fake_yes_no(self, question, default):
        return default

    monkeypatch.setattr(setup.Prompter, "choice", fake_choice)
    monkeypatch.setattr(setup.Prompter, "text", fake_text)
    monkeypatch.setattr(setup.Prompter, "yes_no", fake_yes_no)

    collected = setup.collect_answers(prompter)
    setup.apply(collected, prompter)
    return collected


def parse_env(path):
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key] = raw.strip().strip("'")
    return values


def test_fake_backend_run_writes_a_complete_env_and_port_map(tmp_path, monkeypatch, prompter):
    # backend, port_count, bind, map scheme, username
    run_wizard(tmp_path, monkeypatch, ["fake", "4", "127.0.0.1:8000", "identity", "admin"], prompter)

    port_map = json.loads((tmp_path / "data" / "port_map.json").read_text())
    assert port_map == {"1": 1, "2": 2, "3": 3, "4": 4}

    env = parse_env(tmp_path / "polista.env")
    assert env["PORT_COUNT"] == "4"
    assert env["TOFINO_BACKEND"] == "fake"
    assert env["BOOTSTRAP_USERNAME"] == "admin"
    assert env["SESSION_SECRET"] not in ("", "dev-session-secret", "change-me")
    assert len(env["BOOTSTRAP_PASSWORD"]) >= 12
    # bfrt-only knobs stay out of a fake-backend env
    assert "TOFINO_GRPC_TARGET" not in env


def test_env_file_is_not_world_readable(tmp_path, monkeypatch, prompter):
    run_wizard(tmp_path, monkeypatch, ["fake", "8", "127.0.0.1:8000", "identity", "admin"], prompter)
    mode = (tmp_path / "polista.env").stat().st_mode & 0o777
    assert mode == 0o600


def test_bfrt_run_records_the_switchd_connection(tmp_path, monkeypatch, prompter):
    answers = [
        "bfrt", "8", "0.0.0.0:8888", "zero", "operator",
        "localhost:50052", "0", "polista",
    ]
    run_wizard(tmp_path, monkeypatch, answers, prompter)

    port_map = json.loads((tmp_path / "data" / "port_map.json").read_text())
    assert port_map["1"] == 0 and port_map["8"] == 7

    env = parse_env(tmp_path / "polista.env")
    assert env["TOFINO_BACKEND"] == "bfrt"
    assert env["TOFINO_GRPC_TARGET"] == "localhost:50052"
    assert env["TOFINO_PROGRAM_NAME"] == "polista"
    assert env["HTTP_BIND_ADDR"] == "0.0.0.0:8888"


# --------------------------------------------------------------------- vendored


def test_probe_bfrt_imports_the_vendored_client():
    """The whole point of vendoring: bfrt_grpc imports with no SDE on the box."""
    ok, detail = setup.probe_bfrt()
    assert ok, detail


def test_bfrt_env_has_no_sde_variables(tmp_path, monkeypatch, prompter):
    """bfrt no longer implies an SDE, so nothing SDE-shaped may reach the env."""
    answers = [
        "bfrt", "8", "127.0.0.1:8000", "zero", "admin",
        "127.0.0.1:50052", "0", "polista",
    ]
    run_wizard(tmp_path, monkeypatch, answers, prompter)

    env = parse_env(tmp_path / "polista.env")
    assert env["TOFINO_BACKEND"] == "bfrt"
    for removed in ("SDE_INSTALL", "SDE_PYTHONPATH", "POLISTA_PYTHON"):
        assert removed not in env


def test_fake_backend_env_has_no_sde_variables(tmp_path, monkeypatch, prompter):
    run_wizard(tmp_path, monkeypatch, ["fake", "8", "127.0.0.1:8000", "identity", "admin"], prompter)
    env = parse_env(tmp_path / "polista.env")
    assert "SDE_INSTALL" not in env
    assert "SDE_PYTHONPATH" not in env
    assert "POLISTA_PYTHON" not in env


def test_rerun_preserves_existing_cross_connects(tmp_path, monkeypatch, prompter):
    data = tmp_path / "data"
    data.mkdir()
    existing = {
        "mappings": [{"ingress": 1, "egress": 5}],
        "labels": {"1": "Camera A"},
        "last_sync_status": "ok",
    }
    (data / "mappings.json").write_text(json.dumps(existing))

    run_wizard(tmp_path, monkeypatch, ["fake", "8", "127.0.0.1:8000", "identity", "admin"], prompter)

    assert json.loads((data / "mappings.json").read_text()) == existing


def test_legacy_auth_file_is_backed_up_not_overwritten(tmp_path, monkeypatch, prompter):
    data = tmp_path / "data"
    data.mkdir()
    legacy = json.dumps({"username": "olduser", "password_hash": "$argon2id$v=19$m=1$abc$def"})
    (data / "auth.json").write_text(legacy)

    collected = run_wizard(
        tmp_path, monkeypatch, ["fake", "8", "127.0.0.1:8000", "identity", "newadmin"], prompter
    )

    assert collected["auth_action"] == "replace"
    assert not (data / "auth.json").exists()
    assert json.loads((data / "auth.json.bak").read_text()) == json.loads(legacy)


def test_existing_scrypt_auth_file_is_kept_and_env_says_so(tmp_path, monkeypatch, prompter):
    data = tmp_path / "data"
    data.mkdir()
    (data / "auth.json").write_text(
        json.dumps({"username": "keepme", "password_hash": "scrypt$16384$8$1$c2FsdA$a2V5"})
    )

    collected = run_wizard(
        tmp_path, monkeypatch, ["fake", "8", "127.0.0.1:8000", "identity", "admin"], prompter
    )

    assert collected["auth_action"] == "keep"
    assert collected["username"] == "keepme"
    assert (data / "auth.json").exists()
    assert "placeholder" in (tmp_path / "polista.env").read_text()


# ------------------------------------------------------------------- end to end


def test_generated_env_boots_the_app_healthy(tmp_path, monkeypatch, prompter):
    """The whole point: a fresh wizard run yields a config the real app starts on."""
    answers = ["fake", "4", "127.0.0.1:8000", "custom", "40 30 20 10", "admin"]
    run_wizard(tmp_path, monkeypatch, answers, prompter)

    env = parse_env(tmp_path / "polista.env")
    for key, value in env.items():
        # paths in the env are relative to the project root the wizard wrote into
        if key.endswith("_FILE"):
            value = str(tmp_path / value)
        monkeypatch.setenv(key, value)

    import app.main as main

    importlib.reload(main)
    with TestClient(main.create_app()) as client:
        auth = (env["BOOTSTRAP_USERNAME"], env["BOOTSTRAP_PASSWORD"])

        health = client.get("/health", auth=auth)
        assert health.status_code == 200
        assert health.json()["status"] == "healthy"
        assert health.json()["sync_state"] == "in_sync"

        ports = client.get("/ports", auth=auth)
        assert ports.json()["port_count"] == 4

        # the generated credentials are the ones that actually work
        assert client.get("/ports", auth=(auth[0], "wrong-password")).status_code == 401

        # and the custom port map is live: a mapping round-trips
        created = client.post("/mappings", json={"ingress": 1, "egress": 3}, auth=auth)
        assert created.status_code in (200, 201)
        assert {"ingress": 1, "egress": 3} in client.get("/mappings", auth=auth).json()["mappings"]

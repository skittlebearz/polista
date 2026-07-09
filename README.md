# Portofino

Portofino is a unidirectional, strictly-1:1 port cross-connect ("patch panel") controller for Tofino 1. A minimal P4 dataplane matches ingress port and sets egress port; a single all-Python FastAPI app owns domain logic, the gRPC connection to Tofino, and a server-rendered HTMX UI. Tofino is the live source of truth; JSON files hold desired state, labels, and auth.

Portofino is designed for offline operation with no build step: edit Python, HTML, CSS, or vendored JS, then restart uvicorn. There is no Node, bundler, or compile phase.

## Dev Quickstart

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp data/port_map.json.example data/port_map.json
cp data/mappings.json.example data/mappings.json
```

Create an environment for the fake backend:

```sh
export PORT_COUNT=8
export MAPPINGS_FILE=data/mappings.json
export PORT_MAP_FILE=data/port_map.json
export AUTH_FILE=data/auth.json
export HTTP_BIND_ADDR=127.0.0.1:8000
export SESSION_SECRET=dev-session-secret
export BOOTSTRAP_USERNAME=admin
export BOOTSTRAP_PASSWORD=admin
export TOFINO_BACKEND=fake
export TOFINO_GRPC_TARGET=127.0.0.1:50051
export TOFINO_DEVICE_ID=0
export TOFINO_PROGRAM_NAME=portofino
```

Run the app:

```sh
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

With `TOFINO_BACKEND=fake`, the `TOFINO_GRPC_*` values are ignored. The app creates `data/auth.json` on first start from `BOOTSTRAP_USERNAME` and `BOOTSTRAP_PASSWORD`.

## Data Files

`MAPPINGS_FILE` stores desired UI-port mappings and labels:

```json
{
  "mappings": [{"ingress": 1, "egress": 2}],
  "labels": {"1": "Camera A", "2": "Recorder A"},
  "last_sync_status": "ok"
}
```

`PORT_MAP_FILE` maps UI ports to device ports and must be a bijection over `1..PORT_COUNT`:

```json
{ "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8 }
```

`AUTH_FILE` stores one argon2-hashed user:

```json
{ "username": "admin", "password_hash": "<argon2 hash>" }
```

## Verification

```sh
.venv/bin/python -m pytest
.venv/bin/python scripts/ui_verify.py
```

`scripts/ui_verify.py` starts the real app against a temporary fake backend and drives the browser flow in headless Chromium.

## REST API

All JSON API routes require session auth or HTTP Basic auth.

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/health` | | `{"status":"healthy","tofino_connected":true,"sync_state":"in_sync"}` |
| `GET` | `/ports` | | `{"port_count":8,"ports":[{"port":1,"label":"Camera A"}]}` |
| `GET` | `/mappings` | | `{"mappings":[{"ingress":1,"egress":2}]}` |
| `POST` | `/mappings` | `{"ingress":1,"egress":5,"force":true}` | `{"status":"ok","removed":[],"added":{"ingress":1,"egress":5},"sync_state":"in_sync"}` |
| `DELETE` | `/mappings` | `{"ingress":1,"egress":5}` | `{"status":"ok","sync_state":"in_sync"}` |
| `POST` | `/refresh` | | `{"status":"ok","source":"tofino"}` |
| `GET` | `/labels` | | `{"1":"Camera A"}` |
| `PUT` | `/labels/{port}` | `{"label":"Camera A"}` | `{"status":"ok","sync_state":"in_sync"}` |
| `POST` | `/login` | form `username`, `password` | `{"status":"ok","user":"admin"}` |
| `POST` | `/logout` | | `{"status":"ok"}` |
| `GET` | `/session` | | `{"user":"admin"}` |

`POST /mappings` with `force:false` returns HTTP 409 on conflict:

```json
{ "conflict": true, "would_remove": [{"ingress": 1, "egress": 2}] }
```

## Real Tofino Backend

The real Tofino deliverables, including `p4/` and `P4RuntimeBackend`, land in a later gated phase: Checkpoint 5. Until then, develop and verify against `TOFINO_BACKEND=fake`.

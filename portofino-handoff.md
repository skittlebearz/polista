# Portofino — Tofino Port Cross-Connect Controller — Coding Agent Handoff

**Status:** Implementation-ready. Stack and architecture are locked (Section 2). Build against this directly; deviations from locked decisions need sign-off.

**One-line description:** A unidirectional, strictly-1:1 port cross-connect ("patch panel") controller for Tofino 1. A minimal P4 dataplane matches ingress port and sets egress port; a single all-Python FastAPI app owns domain logic, the gRPC connection to Tofino, and a server-rendered HTMX UI. Tofino is the live source of truth; JSON files hold desired state, labels, and auth.

**Primary design constraint:** the whole thing must be **buildable, modifiable, and runnable offline on a constrained box, with no build/compile step.** Editing the app = editing `.py` / `.html` / `.js` and restarting uvicorn. This constraint drove every stack choice below.

---

## 1. Reading Order

This supersedes any earlier Phoenix/Elixir version of the plan. The *semantics* (1:1 rules, reconciliation, persistence model, REST shapes, port-map bijection) are authoritative and reproduced/refined below. The framework, UI, auth, and gRPC-transport choices are **decided** here. Build order is Section 17 — start there after reading Sections 2–5.

---

## 2. Locked Decisions

| Area | Decision | Rationale |
|---|---|---|
| Backend | **Single all-Python FastAPI app** (one process, one uvicorn) | No language boundary → `grpcio` is just a module. No separate sidecar, no IPC seam. Fewest moving parts on a bare box. |
| Build step | **None.** No compiler, no bundler, no `mix release`, no npm | The app is interpreted end-to-end; frontend deps are vendored static files. This is the whole point — offline-modifiable. |
| UI | **Server-rendered Jinja2 + HTMX**, SVG lines via a small vanilla-JS helper | Server owns all authoritative state (mirrors the LiveView model we liked). One vendored ~14KB `htmx.min.js`, zero build. |
| Tofino / gRPC | **In-process Python backend module** (P4Runtime default), behind an interface with a **fake** implementation | `grpcio` + `p4runtime` live in the app. Fake backend lets the entire app + UI be built and tested with **zero** P4 Studio access. |
| Write serialization | **Single `asyncio.Lock`** around every mutation path | Serializes all writes by construction; protects the 1:1 invariant and JSON files. |
| Backend call style | Backend methods are **synchronous** (match `grpcio`/`bfrt_grpc` examples); the Controller calls them via `asyncio.to_thread` inside the lock | Keeps real-backend code looking exactly like Intel SDE examples; keeps the event loop unblocked. |
| Auth | **Session cookie for UI** (Starlette `SessionMiddleware`), **HTTP Basic for API**, both resolving to the same user check; **argon2** hashing | Built into FastAPI/Starlette; no extra auth system. |
| Port map | **Must be a bijection**, validated at startup | Refresh translates device→UI; ambiguous otherwise. See Section 3.4. |
| P4Runtime vs BFRT | **P4Runtime default**, isolated behind the backend interface | Trivial table (exact match, one action) → P4Runtime is enough and keeps the app in a clean pip venv instead of coupled to the SDE's Python. BFRT remains a drop-in alt backend if a table feature forces it. |

---

## 3. Core Semantics (authoritative)

### 3.1 Mapping model
- Left = ingress ports; right = egress ports.
- Mappings are **unidirectional**. `1 -> 2` does **not** imply `2 -> 1`. Both directions require two entries.
- **Self-connect allowed:** `1 -> 1` is valid.

### 3.2 Strict 1:1 enforcement
Each ingress maps to at most one egress; each egress is mapped from at most one ingress. On a conflicting new mapping, **remove both conflicts, then add**:
- Existing `1 -> 2`, `7 -> 5`; request `1 -> 5` → delete `1 -> 2`, delete `7 -> 5`, add `1 -> 5`.

Dataplane note: the P4 table key is the **ingress device port**, so remapping the *same* ingress (`1 -> 2` → `1 -> 5`) is an upsert on that key. The *egress* conflict (`7 -> 5`) must be an explicit delete of the other ingress's entry (Section 9).

### 3.3 Disconnect
- Removing a mapping requires selecting **both** endpoints of an existing pair (connected ingress, then its connected egress).
- Clicking a single endpoint only highlights it + its current counterpart/line. No mutation.

### 3.4 Port map bijection
The UI↔device port map (Section 10.2) **must be injective in both directions** over the ports it covers. Validated at startup; a non-bijective map marks the backend `unhealthy`. Reason: `/refresh` reads live device-port entries and reverse-translates to UI numbers, which is only well-defined for a bijection.

### 3.5 Unmapped packets
Ingress with no mapping → packet dropped (P4 default action).

---

## 4. Architecture

One process. One diagram.

```
   Browser ◄──HTML over HTTP (HTMX swaps)──►┐
   (Jinja2 + HTMX + lines.js/SVG)           │
                                            ▼
   Python client ──JSON + Basic Auth──►┌──────────────────────────────────────┐
                                       │  FastAPI app (single uvicorn process) │
                                       │                                       │
                                       │  • UI routes → Jinja2 HTML partials   │
                                       │  • JSON REST API + session/Basic auth │
                                       │  • Controller (asyncio.Lock serializes│
                                       │    ALL writes; in-mem state + health) │
                                       │  • Startup reconcile (lifespan)       │
                                       │  • JSON persistence (mappings/labels, │
                                       │    port map, auth)                    │
                                       │  • UI↔device translation              │
                                       │  • Tofino backend (in-process):       │
                                       │      Fake  ── or ──  P4Runtime/grpcio │
                                       └───────────────┬───────────────────────┘
                                                       │ gRPC (real backend only)
                                                       ▼
                                             Tofino 1 (P4 Studio emulator
                                             or hardware) — SOURCE OF TRUTH
```

**Mutation call path:** UI route or REST route → `Controller` method → `async with lock:` → backend op (via `to_thread`, device ports) → on success persist JSON → update state → return re-rendered partial (UI) or JSON (API).

**No PubSub/websocket.** Cross-client auto-sync is gone (acceptable for single-admin); the Refresh button reconciles a stale tab against Tofino.

---

## 5. Tofino Backend Interface

In-process, **device ports only**, knows nothing about UI numbering, labels, JSON, or 1:1 rules. Sync methods (match SDE examples); Controller wraps calls in `asyncio.to_thread`.

```python
class TofinoBackend(Protocol):
    def status(self) -> bool: ...                          # gRPC channel up?
    def read_all(self) -> list[tuple[int, int]]: ...       # [(ingress_dev, egress_dev), ...]
    def write_entry(self, ingress_dev: int, egress_dev: int) -> None: ...  # UPSERT on ingress key
    def delete_entry(self, ingress_dev: int) -> None: ...   # delete by ingress key
    def clear_all(self) -> None: ...                        # clear ONLY cross_connect_table
```

- **`FakeBackend`** — in-memory `dict[int, int]`. Enables building/testing the entire app + UI with no P4 Studio. Selected via `TOFINO_BACKEND=fake`.
- **`P4RuntimeBackend`** — real: `grpcio` + `p4runtime` stubs; reads `TOFINO_GRPC_TARGET`, `TOFINO_DEVICE_ID`, `TOFINO_PROGRAM_NAME`. (`BFRTBackend` is an optional alternate that uses the SDE's `bfrt_grpc`.)

Any backend failure (including unreachable) folds into `unhealthy` — no new health state.

---

## 6. P4 Dataplane

Minimal.
- Parser: only target-required boilerplate; no meaningful header parsing.
- Table `cross_connect_table`: match key = **ingress device port**, exact; action `set_egress(egress_port)` sets `egress_spec`; **default action = drop**.
- Match on device port; UI translation is app-side only. App manages/clears **only** this table.

```
p4/
  portofino.p4
  README.md   # P4 Studio / Tofino 1 build + emulator run notes (agent fills target boilerplate)
```

---

## 7. FastAPI App Structure

```
portofino/
  app/
    main.py            # FastAPI(); lifespan → reconcile on startup; mount routes + middleware + static
    config.py          # load + validate env (Section 15)
    controller.py      # Controller: async methods + asyncio.Lock; in-mem state, health/sync
    port_map.py        # load + BIJECTION validation; ui<->dev translation
    store.py           # JSON load/atomic-save: mappings + labels; separate port map + auth loaders
    auth.py            # argon2 verify; bootstrap from env; session + Basic Auth dependencies
    tofino/
      backend.py       # Protocol/ABC (Section 5)
      fake.py          # FakeBackend
      p4runtime.py     # P4RuntimeBackend (default real)
    routes/
      ui.py            # HTMX routes → Jinja2 partials (Section 8)
      api.py           # JSON REST (Section 11)
      auth.py          # /login /logout /session
    templates/
      base.html  panel.html  _ports.html  _conflict.html  login.html
    static/
      htmx.min.js      # vendored (committed to repo)
      lines.js         # ~30-40 lines: draw/redraw SVG connection lines
      app.css          # hand-written minimal CSS (no Tailwind/build)
  p4/  portofino.p4  README.md
  data/                # runtime JSON files (or paths via env)
  requirements.txt
  portofino.service    # systemd unit
  README.md
```

### Controller (shape)
```python
class Controller:
    def __init__(self, backend, port_map, store, port_count):
        self._lock = asyncio.Lock()
        self.backend = backend
        self.mappings: dict[int, int] = {}   # ingress_ui -> egress_ui
        self.labels: dict[int, str] = {}
        self.health = "healthy"; self.sync = "in_sync"

    async def connect(self, ingress, egress, force): ...     # async with self._lock
    async def disconnect(self, ingress, egress): ...          # async with self._lock
    async def refresh(self): ...                              # async with self._lock
    async def reconcile(self): ...                            # async with self._lock (startup)
    # reads (ports, mappings, labels, health) may be lock-free at this scale
```
Reconcile is invoked from FastAPI's **lifespan** startup, not blocking import.

---

## 8. UI — Jinja2 + HTMX + SVG

Single patch-panel page after login. **State split (deliberate):**
- **Transient selection** (which port is currently clicked, its highlight) = **client-side** in `lines.js`/DOM. No server round trip for mere selection.
- **Authoritative state** (mappings, 1:1 conflict rules) = **server-side**, hit only on actual mutations.

### 8.1 Layout
- Left column: ingress ports `1..N`, editable labels. Right column: egress ports `1..N`, editable labels.
- Each ingress element carries `data-mapped-egress="<n or empty>"` so the client can draw lines and detect pairs.
- `<svg>` overlay for connection lines; header with health/sync indicator, Refresh, logout.

### 8.2 Interaction flow
- **Click ingress** → client highlights it; if it has a mapping, client also highlights the counterpart + line. No request.
- **Click egress while an ingress is selected** → client `hx-post`s `/ui/mappings` with `{ingress, egress, force:false}`.
  - Server computes conflicts from live `mappings`. **No conflict** → returns re-rendered `_ports.html` → HTMX swaps → `htmx:afterSwap` fires `lines.js` redraw.
  - **Conflict** → returns `_conflict.html` (confirm dialog naming what will be removed, with hidden `ingress`/`egress` and a confirm button that `hx-post`s the same route with `force:true`).
- **Click a connected egress whose own connected ingress is selected** → `hx-post` `/ui/mappings/delete`.
- **Label edit** → `hx-put` `/ui/labels/<port>`; returns the updated port fragment.
- **Refresh** → `hx-post` `/ui/refresh`; returns re-rendered panel; `lines.js` redraws.

### 8.3 `lines.js` (the only real JS)
On page load, on `htmx:afterSwap`, and on `window.resize`: read every ingress element's `data-mapped-egress`, compute endpoint positions via `getBoundingClientRect()` relative to the SVG, (re)draw `<line>` elements. Highlight the line for the currently client-selected pair. ~30–40 lines, no dependencies.

### 8.4 Per-port visual states
`idle`, `selected`, `connected`, `connected-to-selected`, `conflict-pending` — CSS classes on the port element and the SVG line.

---

## 9. Write / Update Semantics (authoritative)

All inside `async with self._lock`; backend calls via `asyncio.to_thread`.

### 9.1 `connect(ingress, egress, force)`
1. Validate `ingress, egress ∈ 1..N`.
2. Validate both have port-map entries.
3. Compute conflicts: `old = mappings.get(ingress)`; `other = ingress' where mappings[ingress'] == egress`.
4. If (`old` or `other`) exists and `force is False` → return **conflict** + removal preview (UI: `_conflict.html`; API: HTTP 409). No device writes.
5. Else, under the lock:
   - if `other`: `backend.delete_entry(dev(other))`.
   - `backend.write_entry(dev(ingress), dev(egress))` (upsert covers same-ingress remap of `old`).
   - update `mappings`, then **persist JSON**.
   - **JSON persist fails** → `sync = "out_of_sync"`, return **success-with-warning** (never roll back the device).

### 9.2 `disconnect(ingress, egress)`
1. Verify `mappings.get(ingress) == egress`; else error.
2. `backend.delete_entry(dev(ingress))`.
3. On success: drop from `mappings`, persist. Persist fail → `out_of_sync`, success-with-warning.

### 9.3 Failure ordering (invariant)
**Tofino first, JSON second, always.** Tofino is live truth; JSON failure degrades to `out_of_sync`, never a device rollback.

---

## 10. Persistence Model

### 10.1 Mapping state (`MAPPINGS_FILE`) — UI port numbers
```json
{ "mappings": [{"ingress":1,"egress":2},{"ingress":3,"egress":8}],
  "labels": {"1":"Camera A","2":"Recorder A","3":"Feed B"},
  "last_sync_status": "ok" }
```
Labels freeform, non-unique, optional. Use **atomic writes** (temp file + `os.replace`) so a crash mid-write can't corrupt state.

### 10.2 Port map (`PORT_MAP_FILE`) — UI → device, **must be a bijection**
```json
{ "1": 28, "2": 56, "3": 72 }
```
Every UI port shown needs a valid device port. Invalid/missing/non-bijective → `unhealthy`.

### 10.3 Auth (`AUTH_FILE`)
```json
{ "username": "admin", "password_hash": "<argon2 hash>" }
```
Never plaintext. Bootstrapped from `BOOTSTRAP_USERNAME` / `BOOTSTRAP_PASSWORD` on first start if absent.

---

## 11. JSON REST API (authoritative shapes)

Separate from the HTMX `/ui/*` routes (which return HTML). All require auth (session **or** Basic).

- `GET /health` → `{"status":"healthy","tofino_connected":true,"sync_state":"in_sync"}`
- `GET /ports` → `{"port_count":64,"ports":[{"port":1,"label":"Camera A"}, ...]}`
- `GET /mappings` → `{"mappings":[{"ingress":1,"egress":2}]}`
- `POST /mappings` → `{"ingress":1,"egress":5,"force":true}`
  - `force=false` + conflict → **409** `{"conflict":true,"would_remove":[...]}`
  - `force=true` → applies 9.1; returns `{"status":"ok","removed":[...],"added":{...},"sync_state":"in_sync"}`
- `DELETE /mappings` → `{"ingress":1,"egress":5}`
- `POST /refresh` → `{"status":"ok","source":"tofino"}`
- `GET /labels` → labels map
- `PUT /labels/{port}` → `{"label":"Camera A"}`
- `POST /login`, `POST /logout`, `GET /session`

---

## 12. Reconciliation & Refresh

### 12.1 Startup reconcile (FastAPI lifespan)
1. Load auth; load + **bijection-validate** port map; load mappings JSON. Any parse/validate failure → `unhealthy`, stop.
2. `backend.status()` reachable (bounded retry/backoff). Never reachable → `unhealthy`.
3. `backend.clear_all()` (app table only).
4. For each JSON mapping: translate → `backend.write_entry(...)`.
5. All succeed → `healthy`+`in_sync`. Any fail → `partial_sync` (or `unhealthy` if channel dropped). Surface in `/health` + UI.

### 12.2 Refresh
1. `backend.read_all()` → device-port entries.
2. Reverse-translate device→UI (bijection makes this unambiguous).
3. Replace in-memory `mappings`; **preserve labels**.
4. Persist JSON. Re-render UI.

---

## 13. Health / Sync States
- `in_sync` — Tofino and JSON aligned.
- `out_of_sync` — device op ok, JSON persist failed.
- `partial_sync` — startup replay partially succeeded.
- `unhealthy` — cannot reach Tofino backend, invalid/non-bijective config, or unrecoverable error.

Visible in `GET /health` and the UI status area.

---

## 14. Validation Rules
- `ingress`, `egress` valid UI ports; both have port-map entries.
- Port map bijective (startup check).
- Duplicate identical mapping = no-op (or routed to disconnect), never a second entry.
- Config files must parse.
- **Allowed:** self-connect `1 -> 1`.
- **Not required:** unique labels.

---

## 15. Config (env)
```
PORT_COUNT            HTTP_BIND_ADDR         TOFINO_BACKEND=fake|p4runtime
MAPPINGS_FILE         SESSION_SECRET         TOFINO_GRPC_TARGET
PORT_MAP_FILE         BOOTSTRAP_USERNAME     TOFINO_DEVICE_ID
AUTH_FILE             BOOTSTRAP_PASSWORD     TOFINO_PROGRAM_NAME
```
`TOFINO_*` are consumed by the in-process backend. With `TOFINO_BACKEND=fake`, the `TOFINO_GRPC_*` values are ignored.

---

## 16. Dependencies

**`requirements.txt`** (minimal):
```
fastapi
uvicorn[standard]
jinja2
python-multipart      # HTML form posts
itsdangerous          # required by Starlette SessionMiddleware
argon2-cffi           # password hashing
grpcio                # real backend only (the one native dep)
protobuf              # real backend only
p4runtime             # P4Runtime stubs (real backend only)
# grpcio-tools        # BUILD-ONLY: regenerate stubs from .proto if needed
```
Runtime: **Python 3.9+**. Frontend deps are **vendored static files** (`htmx.min.js` committed to the repo) — nothing to install, no Node, no bundler.

With `TOFINO_BACKEND=fake`, the entire app runs on just the top five packages — no `grpcio` needed for dev/UI work.

**BFRT alternative:** `bfrt_grpc` is not on PyPI; it ships inside the Intel SDE and runs in the SDE's Python. Choosing P4Runtime (default) keeps Portofino in a clean venv.

---

## 17. Build Order (for the agent)

Build entirely against `FakeBackend` first; touch P4 Studio last.

1. **App core.** FastAPI skeleton, `config`, `store` (atomic JSON), `port_map` (+ bijection validation), `Controller` (asyncio.Lock) with connect/disconnect/refresh/reconcile + reads, `FakeBackend`. Hard-test the 1:1 conflict logic — the `1->2`,`7->5`,`1->5` case is the canonical unit test.
2. **JSON REST API + auth.** Routes per Section 11; session + Basic Auth; argon2; bootstrap-from-env.
3. **HTMX UI.** `base/panel/_ports/_conflict/login` templates; `/ui/*` routes returning partials; client selection + `lines.js` SVG drawer; conflict dialog; label edit; refresh; health indicator.
4. **Python client.** Thin REST wrapper: `get_ports`, `get_mappings`, `connect(ingress, egress, force=False)`, `disconnect`, `refresh`, `get_labels`, `set_label`. No direct gRPC.
5. **P4 dataplane + real backend.** Fill Tofino 1 / P4 Studio boilerplate; implement `P4RuntimeBackend`; verify in emulator; swap `TOFINO_BACKEND=p4runtime`.
6. **Robustness.** Sync-state transitions, startup failure states, error messages, emulator test coverage.

Steps 1–4 need no hardware, no emulator, no `grpcio`.

---

## 18. Deployment (offline / no build step)

Single service. To the target you ship: **the app source tree** + **a Python venv** (with `grpcio`/`p4runtime` wheels pre-staged) + the vendored `htmx.min.js` (already in the tree). Run under systemd:

```
uvicorn app.main:app --host <HTTP_BIND_ADDR host> --port <HTTP_BIND_ADDR port>
```

<!-- NOTE (coordinator): the original handoff was truncated mid-Section-18 at the uvicorn line; the command completion above is the only reconstructed text. -->

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import ensure_auth_file
from app.config import load_config
from app.controller import Controller
from app.port_map import PortMap, PortMapError, load_port_map
from app.routes import api as api_routes
from app.routes import auth as auth_routes
from app.routes import ui as ui_routes
from app.store import Store
from app.tofino.fake import FakeBackend


def _build_backend(name: str):
    if name == "fake":
        return FakeBackend()
    if name == "p4runtime":
        raise NotImplementedError("TOFINO_BACKEND=p4runtime is not implemented until Checkpoint 5")
    raise ValueError(f"unsupported TOFINO_BACKEND: {name}")


def _identity_port_map(port_count: int) -> PortMap:
    return PortMap({port: port for port in range(1, port_count + 1)}, port_count)


def create_app() -> FastAPI:
    cfg = load_config()
    backend = _build_backend(cfg.tofino_backend)
    store = Store(cfg.mappings_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_auth_file(cfg)

        try:
            port_map = load_port_map(cfg.port_map_file, cfg.port_count)
        except PortMapError:
            controller = Controller(backend, _identity_port_map(cfg.port_count), store, cfg.port_count)
            controller.health = "unhealthy"
            app.state.controller = controller
        else:
            controller = Controller(backend, port_map, store, cfg.port_count)
            app.state.controller = controller
            await controller.reconcile()

        yield

    app = FastAPI(lifespan=lifespan)
    app.state.config = cfg
    app.add_middleware(SessionMiddleware, secret_key=cfg.session_secret)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.include_router(auth_routes.router)
    app.include_router(api_routes.router)
    app.include_router(ui_routes.router)
    return app


app = create_app()

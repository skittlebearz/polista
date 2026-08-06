from __future__ import annotations

import asyncio
import logging
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
from app.tofino.bfrt import BFRTBackend

log = logging.getLogger("polista.main")


def _build_backend(cfg):
    if cfg.tofino_backend == "fake":
        return FakeBackend()
    if cfg.tofino_backend == "p4runtime":
        raise NotImplementedError("TOFINO_BACKEND=p4runtime is not implemented until Checkpoint 5")
    if cfg.tofino_backend == "bfrt":
        return BFRTBackend(
            cfg.tofino_grpc_target,
            int(cfg.tofino_device_id),
            cfg.tofino_program_name,
        )
    raise ValueError(f"unsupported TOFINO_BACKEND: {cfg.tofino_backend}")


def _identity_port_map(port_count: int) -> PortMap:
    return PortMap({port: port for port in range(1, port_count + 1)}, port_count)


async def _drift_loop(controller, interval: float) -> None:
    """Poll the device so the status LED reflects the table, not our memory."""
    while True:
        await asyncio.sleep(interval)
        try:
            await controller.check_drift()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A drift check that throws must never take the app down with it;
            # check_drift already marks health for the failures it can name.
            log.exception("drift check failed")


def create_app() -> FastAPI:
    cfg = load_config()
    backend = _build_backend(cfg)
    store = Store(cfg.mappings_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_auth_file(cfg)

        try:
            port_map = load_port_map(cfg.port_map_file, cfg.port_count)
        except PortMapError as exc:
            controller = Controller(backend, _identity_port_map(cfg.port_count), store, cfg.port_count)
            controller.mark_unhealthy(
                f"port map {cfg.port_map_file} is unusable: {exc}", exc, recoverable=False
            )
            app.state.controller = controller
        else:
            controller = Controller(backend, port_map, store, cfg.port_count)
            app.state.controller = controller
            await controller.reconcile()

        drift_task = None
        if cfg.drift_check_interval > 0:
            drift_task = asyncio.create_task(
                _drift_loop(controller, cfg.drift_check_interval)
            )

        try:
            yield
        finally:
            if drift_task is not None:
                drift_task.cancel()
                try:
                    await drift_task
                except asyncio.CancelledError:
                    pass
            await asyncio.to_thread(backend.close)

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

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import require_user
from app.controller import BackendError


router = APIRouter(dependencies=[Depends(require_user)])


class MappingCreate(BaseModel):
    ingress: int
    egress: int
    force: bool = False


class MappingDelete(BaseModel):
    ingress: int
    egress: int


class LabelUpdate(BaseModel):
    label: str


def _controller(request: Request):
    return request.app.state.controller


def _config(request: Request):
    return request.app.state.config


@router.get("/health")
async def health(request: Request):
    controller = _controller(request)
    return {
        "status": controller.health,
        "tofino_connected": controller.health == "healthy",
        "sync_state": controller.sync,
    }


@router.get("/ports")
async def ports(request: Request):
    controller = _controller(request)
    config = _config(request)
    labels = controller.labels
    return {
        "port_count": config.port_count,
        "ports": [
            {"port": port, "label": labels.get(port, "")}
            for port in range(1, config.port_count + 1)
        ],
    }


@router.get("/mappings")
async def mappings(request: Request):
    controller = _controller(request)
    return {
        "mappings": [
            {"ingress": ingress, "egress": egress}
            for ingress, egress in sorted(controller.mappings.items())
        ]
    }


@router.post("/mappings")
async def create_mapping(request: Request, body: MappingCreate):
    controller = _controller(request)
    try:
        result = await controller.connect(body.ingress, body.egress, force=body.force)
    except BackendError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.get("status") == "conflict":
        return JSONResponse(
            status_code=409,
            content={"conflict": True, "would_remove": result.get("would_remove", [])},
        )

    return result


@router.delete("/mappings")
async def delete_mapping(request: Request, body: MappingDelete):
    controller = _controller(request)
    try:
        return await controller.disconnect(body.ingress, body.egress)
    except BackendError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/refresh")
async def refresh(request: Request):
    controller = _controller(request)
    try:
        return await controller.refresh()
    except BackendError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/labels")
async def labels(request: Request):
    controller = _controller(request)
    return {str(port): label for port, label in sorted(controller.labels.items())}


@router.put("/labels/{port}")
async def update_label(request: Request, port: int, body: LabelUpdate):
    controller = _controller(request)
    try:
        return await controller.set_label(port, body.label)
    except BackendError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

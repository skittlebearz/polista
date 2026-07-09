from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials


router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def _controller(request: Request):
    return request.app.state.controller


def _config(request: Request):
    return request.app.state.config


def _session_user(request: Request) -> str | None:
    user = request.session.get("user")
    if user:
        return str(user)
    return None


def _require_session(request: Request) -> str:
    user = _session_user(request)
    if user is None:
        raise HTTPException(status_code=401)
    return user


def _panel_context(request: Request) -> dict:
    controller = _controller(request)
    config = _config(request)
    return {
        "request": request,
        "port_count": config.port_count,
        "mappings": controller.mappings,
        "labels": controller.labels,
        "health": controller.health,
        "sync": controller.sync,
    }


@router.get("/ui")
async def index(request: Request):
    if _session_user(request) is None:
        return RedirectResponse("/ui/login", status_code=303)
    return templates.TemplateResponse(request, "base.html", _panel_context(request))


@router.get("/ui/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/ui/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    config = _config(request)
    if not verify_credentials(username, password, config.auth_file):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials"},
            status_code=401,
        )

    request.session["user"] = username
    return RedirectResponse("/ui", status_code=303)


@router.post("/ui/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/ui/login", status_code=303)


@router.post("/ui/mappings")
async def create_mapping(
    request: Request,
    ingress: int = Form(...),
    egress: int = Form(...),
    force: str = Form("false"),
):
    _require_session(request)
    controller = _controller(request)
    force_bool = force.lower() in ("true", "1", "on")

    try:
        result = await controller.connect(ingress, egress, force=force_bool)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.get("status") == "conflict":
        return templates.TemplateResponse(
            request,
            "_conflict.html",
            {
                "ingress": ingress,
                "egress": egress,
                "would_remove": result.get("would_remove", []),
            },
            headers={"HX-Retarget": "#dialog", "HX-Reswap": "innerHTML"},
        )

    return templates.TemplateResponse(request, "_ports.html", _panel_context(request))


@router.post("/ui/mappings/delete")
async def delete_mapping(
    request: Request,
    ingress: int = Form(...),
    egress: int = Form(...),
):
    _require_session(request)
    controller = _controller(request)
    try:
        await controller.disconnect(ingress, egress)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return templates.TemplateResponse(request, "_ports.html", _panel_context(request))


@router.put("/ui/labels/{port}")
async def update_label(
    request: Request,
    port: int,
    label: str = Form(...),
):
    _require_session(request)
    controller = _controller(request)
    try:
        await controller.set_label(port, label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return templates.TemplateResponse(request, "_ports.html", _panel_context(request))


@router.post("/ui/refresh")
async def refresh(request: Request):
    _require_session(request)
    controller = _controller(request)
    await controller.refresh()
    return templates.TemplateResponse(request, "panel.html", _panel_context(request))

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app.auth import require_user, verify_credentials


router = APIRouter()


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    config = request.app.state.config
    if not verify_credentials(username, password, config.auth_file):
        raise HTTPException(status_code=401, detail="invalid credentials")

    request.session["user"] = username
    return {"status": "ok", "user": username}


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"status": "ok"}


@router.get("/session")
async def session(user: str = Depends(require_user)):
    return {"user": user}

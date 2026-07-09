from __future__ import annotations

import json
from pathlib import Path

from argon2 import PasswordHasher
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.store import load_auth


_basic = HTTPBasic(auto_error=False)


def ensure_auth_file(config) -> None:
    auth_path = Path(config.auth_file)
    if auth_path.exists():
        return

    auth_path.parent.mkdir(parents=True, exist_ok=True)
    password_hash = PasswordHasher().hash(config.bootstrap_password)
    data = {
        "username": config.bootstrap_username,
        "password_hash": password_hash,
    }
    with auth_path.open("w") as f:
        json.dump(data, f)


def verify_credentials(username, password, auth_path) -> bool:
    data = load_auth(auth_path)
    if not data or data.get("username") != username:
        return False

    password_hash = data.get("password_hash")
    if not password_hash:
        return False

    try:
        return PasswordHasher().verify(password_hash, password)
    except Exception:
        return False


async def require_user(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    session_user = request.session.get("user")
    if session_user:
        return str(session_user)

    if credentials is not None:
        config = request.app.state.config
        if verify_credentials(credentials.username, credentials.password, config.auth_file):
            return credentials.username

    raise HTTPException(
        status_code=401,
        headers={"WWW-Authenticate": "Basic"},
    )

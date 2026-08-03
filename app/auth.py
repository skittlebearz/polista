from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.store import load_auth


_basic = HTTPBasic(auto_error=False)
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${_b64encode(salt)}${_b64encode(derived_key)}"
    )


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        algorithm, n, r, p, encoded_salt, encoded_key = password_hash.split("$")
        if algorithm != "scrypt":
            return False
        expected_key = _b64decode(encoded_key)
        actual_key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(encoded_salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_key),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual_key, expected_key)


_dummy_password_hash = _hash_password("polista-dummy-password")


def ensure_auth_file(config) -> None:
    auth_path = Path(config.auth_file)
    if auth_path.exists():
        data = load_auth(auth_path)
        password_hash = data.get("password_hash", "") if data else ""
        if not password_hash.startswith("scrypt$"):
            legacy = "legacy Argon2" if password_hash.startswith("$argon2") else "unsupported"
            raise RuntimeError(
                f"{auth_path} uses an {legacy} password-hash format. Before starting "
                "Polista, move that file aside and re-bootstrap it with the existing "
                "BOOTSTRAP_USERNAME and BOOTSTRAP_PASSWORD values."
            )
        return

    auth_path.parent.mkdir(parents=True, exist_ok=True)
    password_hash = _hash_password(config.bootstrap_password)
    data = {
        "username": config.bootstrap_username,
        "password_hash": password_hash,
    }
    with auth_path.open("w") as f:
        json.dump(data, f)


def verify_credentials(username, password, auth_path) -> bool:
    data = load_auth(auth_path)
    matching_user = bool(data) and data.get("username") == username
    password_hash = data.get("password_hash") if matching_user else None
    password_hash = password_hash or _dummy_password_hash

    verified = _verify_password(password_hash, password)
    return bool(matching_user and verified)


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

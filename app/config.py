from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    port_count: int
    mappings_file: str
    port_map_file: str
    auth_file: str
    http_bind_addr: str
    session_secret: str
    bootstrap_username: str
    bootstrap_password: str
    tofino_backend: str
    tofino_grpc_target: str
    tofino_device_id: str
    tofino_program_name: str


def load_config() -> Config:
    try:
        port_count = int(os.environ.get("PORT_COUNT", "8"))
    except ValueError as exc:
        raise ValueError("PORT_COUNT must be a positive integer") from exc

    if port_count <= 0:
        raise ValueError("PORT_COUNT must be a positive integer")

    tofino_backend = os.environ.get("TOFINO_BACKEND", "fake")
    if tofino_backend not in {"fake", "p4runtime", "bfrt"}:
        raise ValueError('TOFINO_BACKEND must be "fake", "p4runtime", or "bfrt"')

    return Config(
        port_count=port_count,
        mappings_file=os.environ.get("MAPPINGS_FILE", "data/mappings.json"),
        port_map_file=os.environ.get("PORT_MAP_FILE", "data/port_map.json"),
        auth_file=os.environ.get("AUTH_FILE", "data/auth.json"),
        http_bind_addr=os.environ.get("HTTP_BIND_ADDR", "127.0.0.1:8000"),
        session_secret=os.environ.get("SESSION_SECRET", "dev-session-secret"),
        bootstrap_username=os.environ.get("BOOTSTRAP_USERNAME", "admin"),
        bootstrap_password=os.environ.get("BOOTSTRAP_PASSWORD", "admin"),
        tofino_backend=tofino_backend,
        tofino_grpc_target=os.environ.get("TOFINO_GRPC_TARGET", "127.0.0.1:50051"),
        tofino_device_id=os.environ.get("TOFINO_DEVICE_ID", "0"),
        tofino_program_name=os.environ.get("TOFINO_PROGRAM_NAME", "portofino"),
    )

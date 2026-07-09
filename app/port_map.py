from __future__ import annotations

import json
from pathlib import Path


class PortMapError(Exception):
    pass


class PortMap:
    def __init__(self, ui_to_dev: dict[int, int], port_count: int):
        if port_count <= 0:
            raise PortMapError("port_count must be positive")

        expected_ports = set(range(1, port_count + 1))
        actual_ports = set(ui_to_dev)
        missing_ports = expected_ports - actual_ports
        extra_ports = actual_ports - expected_ports
        if missing_ports:
            missing = ", ".join(str(port) for port in sorted(missing_ports))
            raise PortMapError(f"missing UI port mapping(s): {missing}")
        if extra_ports:
            extra = ", ".join(str(port) for port in sorted(extra_ports))
            raise PortMapError(f"unknown UI port mapping(s): {extra}")

        if len(set(ui_to_dev.values())) != len(ui_to_dev):
            raise PortMapError("port map must be injective")

        self._ui_to_dev = dict(ui_to_dev)
        self._dev_to_ui = {dev: ui for ui, dev in self._ui_to_dev.items()}

    def to_dev(self, ui: int) -> int:
        try:
            return self._ui_to_dev[ui]
        except KeyError as exc:
            raise PortMapError(f"unknown UI port: {ui}") from exc

    def to_ui(self, dev: int) -> int:
        try:
            return self._dev_to_ui[dev]
        except KeyError as exc:
            raise PortMapError(f"unknown device port: {dev}") from exc


def load_port_map(path, port_count: int) -> PortMap:
    port_map_path = Path(path)
    try:
        with port_map_path.open("r", encoding="utf-8") as f:
            raw_map = json.load(f)
    except FileNotFoundError as exc:
        raise PortMapError(f"port map file not found: {port_map_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PortMapError(f"failed to load port map: {port_map_path}") from exc

    if not isinstance(raw_map, dict):
        raise PortMapError("port map must be a JSON object")

    try:
        ui_to_dev = {int(ui): dev for ui, dev in raw_map.items()}
    except (TypeError, ValueError) as exc:
        raise PortMapError("port map keys must be integer strings") from exc

    if not all(isinstance(dev, int) for dev in ui_to_dev.values()):
        raise PortMapError("port map values must be integers")

    try:
        return PortMap(ui_to_dev, port_count)
    except PortMapError:
        raise


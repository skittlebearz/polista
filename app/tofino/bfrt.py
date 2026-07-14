"""BF Runtime backend for the Tofino switchd gRPC server."""

from __future__ import annotations

from typing import Any

from app.tofino.backend import TofinoBackend


class BFRTBackend(TofinoBackend):
    """Manage Portofino's ingress-to-egress table through BFRT."""

    TABLE_NAME = "pipe.Ingress.port_map"
    KEY_FIELD = "ig_intr_md.ingress_port"
    ACTION_NAME = "Ingress.send"
    PORT_FIELD = "port"

    def __init__(self, grpc_target: str, device_id: int, program_name: str) -> None:
        self.grpc_target = grpc_target
        self.device_id = device_id
        self.program_name = program_name
        self._gc: Any | None = None
        self._interface: Any | None = None
        self._table: Any | None = None
        self._target: Any | None = None

    def _invalidate(self) -> None:
        interface = self._interface
        self._gc = None
        self._interface = None
        self._table = None
        self._target = None
        if interface is not None:
            try:
                interface.tear_down_stream()
            except Exception:
                pass

    def _connect(self) -> None:
        if self._interface is not None:
            return

        # bfrt_grpc is supplied by the SDE container, so importing it at module
        # import time would make the normal fake-backend path unusable on hosts.
        import bfrt_grpc.client as gc

        interface = None
        last_error: Exception | None = None
        for client_id in range(10):
            try:
                interface = gc.ClientInterface(
                    self.grpc_target, client_id=client_id, device_id=self.device_id
                )
                break
            except Exception as exc:
                last_error = exc
        if interface is None:
            assert last_error is not None
            raise last_error

        try:
            interface.bind_pipeline_config(self.program_name)
            bfrt_info = interface.bfrt_info_get(self.program_name)
            table = bfrt_info.table_get(self.TABLE_NAME)
            target = gc.Target(device_id=self.device_id, pipe_id=0xFFFF)
        except Exception:
            try:
                interface.tear_down_stream()
            except Exception:
                pass
            raise

        self._gc = gc
        self._interface = interface
        self._table = table
        self._target = target

    def _connection(self) -> tuple[Any, Any, Any]:
        self._connect()
        assert self._gc is not None
        assert self._table is not None
        assert self._target is not None
        return self._gc, self._table, self._target

    def status(self) -> bool:
        try:
            self._connect()
            return True
        except Exception:
            self._invalidate()
            return False

    def write_entry(self, ingress_dev: int, egress_dev: int) -> None:
        try:
            gc, table, target = self._connection()
            key = table.make_key([gc.KeyTuple(self.KEY_FIELD, ingress_dev)])
            data = table.make_data(
                [gc.DataTuple(self.PORT_FIELD, egress_dev)], self.ACTION_NAME
            )
            table.entry_add_or_mod(target, [key], [data])
        except Exception:
            self._invalidate()
            raise

    def delete_entry(self, ingress_dev: int) -> None:
        try:
            gc, table, target = self._connection()
            key = table.make_key([gc.KeyTuple(self.KEY_FIELD, ingress_dev)])
            table.entry_del(target, [key])
        except Exception:
            self._invalidate()
            raise

    def read_all(self) -> list[tuple[int, int]]:
        try:
            _, table, target = self._connection()
            entries: list[tuple[int, int]] = []
            for data, key in table.entry_get(target):
                data_dict = data.to_dict()
                action_name = data_dict.get("action_name")
                if action_name is not None and action_name != self.ACTION_NAME:
                    continue
                ingress = key.to_dict()[self.KEY_FIELD]["value"]
                entries.append((ingress, data_dict[self.PORT_FIELD]))
            return entries
        except Exception:
            self._invalidate()
            raise

    def clear_all(self) -> None:
        try:
            _, table, target = self._connection()
            table.entry_del(target)
        except Exception:
            self._invalidate()
            raise

    def close(self) -> None:
        self._invalidate()

"""The vendored bfrt_grpc must actually drive BFRTBackend without an SDE.

Vendoring bfrt_grpc (vendor/, see vendor/README.md) is what lets Polista run on
a machine with no P4 Studio install. A plain `import bfrt_grpc` check is not
enough to prove that: the client only earns its keep if it can complete the real
write/read/delete/clear cycle over gRPC against a BF Runtime server.

So this stands up a stub BfRuntime server implementing the subset of
bfruntime.proto that BFRTBackend exercises, and drives the unmodified backend
against it. It is not a switch -- switchd behaviour is covered by
scripts/bfrt_verify.py inside the SDE -- it is a conformance harness for the
vendored client and the wire contract between them.
"""

import json
import socket
from concurrent import futures

import pytest

from app.tofino.bfrt import BFRTBackend, VENDOR_DIR, _import_bfrt_grpc

# Skip rather than fail where the bfrt extras are not installed: the fake
# backend is a supported configuration and requirements.txt marks these as
# bfrt-only.
pytest.importorskip("grpc", reason="bfrt extras not installed (pip install grpcio)")

import grpc  # noqa: E402

# Puts vendor/ on sys.path the same way the app does, so the stub server below
# can import the generated stubs by name.
_import_bfrt_grpc()

from bfrt_grpc import bfruntime_pb2 as pb  # noqa: E402
from bfrt_grpc import bfruntime_pb2_grpc as pb_grpc  # noqa: E402

TABLE_ID = 0x01000001
KEY_ID = 1
ACTION_SEND = 0x02000001
ACTION_DROP = 0x02000002
PORT_FIELD_ID = 1

# The shape bf-p4c emits for p4/polista.p4, trimmed to what info_parse needs.
BFRT_JSON = {
    "schema_version": "1.0.0",
    "tables": [
        {
            "name": "pipe.Ingress.port_map",
            "id": TABLE_ID,
            "table_type": "MatchAction_Direct",
            "size": 512,
            "attributes": [],
            "supported_operations": [],
            "key": [
                {
                    "id": KEY_ID,
                    "name": "ig_intr_md.ingress_port",
                    "repeated": False,
                    "mandatory": True,
                    "match_type": "Exact",
                    "type": {"type": "bytes", "width": 9},
                }
            ],
            "action_specs": [
                {
                    "id": ACTION_SEND,
                    "name": "Ingress.send",
                    "data": [
                        {
                            "id": PORT_FIELD_ID,
                            "name": "port",
                            "repeated": False,
                            "mandatory": True,
                            "read_only": False,
                            "type": {"type": "bytes", "width": 9},
                        }
                    ],
                },
                {"id": ACTION_DROP, "name": "Ingress.drop", "data": []},
            ],
            "data": [],
        }
    ],
}
NON_P4_JSON = {"schema_version": "1.0.0", "tables": []}


def _to_int(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


class StubBfRuntime(pb_grpc.BfRuntimeServicer):
    """Just enough BF Runtime to hold one exact-match table."""

    def __init__(self):
        self.entries: dict[int, int] = {}

    def StreamChannel(self, request_iterator, context):
        for req in request_iterator:
            if req.HasField("subscribe"):
                resp = pb.StreamMessageResponse()
                resp.subscribe.status.code = 0  # google.rpc.Code.OK
                yield resp

    def SetForwardingPipelineConfig(self, request, context):
        return pb.SetForwardingPipelineConfigResponse()

    def GetForwardingPipelineConfig(self, request, context):
        resp = pb.GetForwardingPipelineConfigResponse()
        config = resp.config.add()
        config.p4_name = "polista"
        config.bfruntime_info = json.dumps(BFRT_JSON).encode()
        resp.non_p4_config.bfruntime_info = json.dumps(NON_P4_JSON).encode()
        return resp

    def Write(self, request, context):
        for update in request.updates:
            entry = update.entity.table_entry
            keys = list(entry.key.fields)
            if update.type == pb.Update.DELETE:
                # No key list means "clear the table" -- the same convention the
                # real server uses, and what BFRTBackend.clear_all() relies on.
                if keys:
                    self.entries.pop(_to_int(keys[0].exact.value), None)
                else:
                    self.entries.clear()
                continue
            self.entries[_to_int(keys[0].exact.value)] = _to_int(entry.data.fields[0].stream)
        return pb.WriteResponse()

    def Read(self, request, context):
        resp = pb.ReadResponse()
        for ingress, egress in sorted(self.entries.items()):
            table_entry = resp.entities.add().table_entry
            table_entry.table_id = TABLE_ID
            key_field = table_entry.key.fields.add()
            key_field.field_id = KEY_ID
            key_field.exact.value = ingress.to_bytes(2, "big")
            table_entry.data.action_id = ACTION_SEND
            data_field = table_entry.data.fields.add()
            data_field.field_id = PORT_FIELD_ID
            data_field.stream = egress.to_bytes(2, "big")
        yield resp


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def switchd():
    """A stub BF Runtime server; yields its target address."""
    port = _free_port()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb_grpc.add_BfRuntimeServicer_to_server(StubBfRuntime(), server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(grace=None)


@pytest.fixture
def backend(switchd):
    device = BFRTBackend(switchd, 0, "polista")
    try:
        yield device
    finally:
        device.close()


def test_bfrt_grpc_is_imported_from_the_vendor_directory():
    """Guards the fallback: without vendor/ on sys.path this needs an SDE."""
    import bfrt_grpc.client as gc

    assert str(VENDOR_DIR) in gc.__file__


def test_status_connects_binds_and_fetches_the_pipeline(backend):
    assert backend.status() is True
    assert backend.last_error is None


def test_write_then_read_round_trips_through_grpc(backend):
    backend.write_entry(1, 2)
    backend.write_entry(3, 4)

    assert backend.read_all() == [(1, 2), (3, 4)]


def test_write_entry_is_an_upsert(backend):
    backend.write_entry(1, 2)
    backend.write_entry(1, 7)

    assert backend.read_all() == [(1, 7)]


def test_delete_entry_removes_only_its_own_key(backend):
    backend.write_entry(1, 2)
    backend.write_entry(3, 4)

    backend.delete_entry(1)

    assert backend.read_all() == [(3, 4)]


def test_clear_all_empties_the_table(backend):
    backend.write_entry(1, 2)
    backend.write_entry(3, 4)

    backend.clear_all()

    assert backend.read_all() == []


def test_status_is_false_and_keeps_why_when_switchd_is_down():
    """An unreachable target must not raise -- the controller reads last_error."""
    # Every client id is walked before giving up, so keep the per-try timeout
    # short; the default 1s would make this test take the best part of a minute.
    device = BFRTBackend(f"127.0.0.1:{_free_port()}", 0, "polista", subscribe_timeout=0)

    assert device.status() is False
    assert device.last_error is not None

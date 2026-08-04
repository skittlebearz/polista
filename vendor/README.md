# Vendored BF Runtime gRPC client

`bfrt_grpc` is the Python client Polista uses to talk to `bf_switchd` over BF
Runtime gRPC (`:50052`). It is **not on PyPI** — it ships inside the Intel SDE.
Rather than require an SDE install on every machine that runs the controller, a
copy lives here.

This is what makes Polista runnable on a plain host: `pip install -r
requirements.txt`, point `TOFINO_GRPC_TARGET` at a switch, done. You still need
the SDE to *compile* `p4/polista.p4` and to run `bf_switchd` / `tofino-model` —
only the control plane is freed.

## What is here

| file | origin |
|---|---|
| `bfrt_grpc/client.py` | upstream, unmodified |
| `bfrt_grpc/info_parse.py` | upstream, unmodified |
| `bfrt_grpc/bfruntime.proto` | upstream, unmodified |
| `bfrt_grpc/bfruntime_pb2.py`, `bfruntime_pb2_grpc.py` | **generated** from the `.proto` |
| `bfrt_grpc/__init__.py` | ours (upstream ships no `__init__.py`) |

Upstream is [p4lang/open-p4studio](https://github.com/p4lang/open-p4studio),
`pkgsrc/bf-drivers/src/bf_rt/bfruntime_grpc_client/python/` (client sources) and
`pkgsrc/bf-drivers/src/bf_rt/proto/bfruntime.proto`. Copied from `main` on
2026-08-04, matching the SDE this was developed against (open-p4studio 9.13.4).

Licensed **Apache-2.0** — see the SPDX headers on each file and open-p4studio's
`LICENSE`.

The whole thing is pure Python. It has no C extensions, reads no SDE paths, and
loads nothing from disk at runtime: the table schema it needs arrives from
`bf_switchd` over the wire via `GetForwardingPipelineConfig`. Nothing about it
actually required the SDE — only the *prebuilt* stubs' pinned protobuf did.

## Why the stubs are generated here, not copied

The SDE's prebuilt `*_pb2.py` are compiled against protobuf 4.23.4 / grpcio
1.60.0 and drag that pin along, which is what forced Polista onto the SDE's own
interpreter. Generating from the `.proto` drops the pin.

They are generated with **grpcio-tools 1.60.0** on purpose: newer protoc emits a
`ValidateProtobufRuntimeVersion` call and a `GRPC_GENERATED_VERSION` floor that
would refuse to load on *older* runtimes. The 1.60-era output carries neither, so
it works across the range — verified importing and driving `BFRTBackend` on both
protobuf 4.25.9 / grpcio 1.60.0 and protobuf 7.35.1 / grpcio 1.83.0.

## Refreshing from upstream

```sh
pip install grpcio-tools grpcio-status 'setuptools<81'
bash scripts/regen_bfrt_stubs.sh
```

The generated files are committed, so this is only needed when `bfruntime.proto`
is updated. Keep using an older `grpcio-tools` (1.60.x) for the reason above.

## Precedence

`app/tofino/bfrt.py` appends this directory to `sys.path` — the *end*, so an
SDE-provided `bfrt_grpc` already on `PYTHONPATH` still wins. Running inside the
SDE container against its own client keeps working unchanged.

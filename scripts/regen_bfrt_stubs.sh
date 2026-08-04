#!/usr/bin/env bash
# Regenerate the vendored bfrt_grpc protobuf stubs from bfruntime.proto.
#
# The generated *_pb2*.py files are committed, so this only needs re-running
# when bfruntime.proto is updated from upstream open-p4studio. Needs
# grpcio-tools, which is a build-only dependency (see requirements.txt).
#
#     pip install grpcio-tools grpcio-status
#     bash scripts/regen_bfrt_stubs.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/vendor/bfrt_grpc"
PYTHON="${PYTHON:-python3}"

if ! "$PYTHON" -c "import grpc_tools.protoc" 2>/dev/null; then
    echo "grpcio-tools is not installed for $PYTHON — pip install grpcio-tools" >&2
    exit 1
fi

# bfruntime.proto imports google/rpc/status.proto. grpcio-status ships that
# .proto file, so resolve the import from there rather than fetching it.
# google.rpc is a namespace package with no __file__, so locate it through a
# module that has one.
GOOGLE_PROTO_ROOT="$("$PYTHON" - <<'PY'
import pathlib
import google.rpc.status_pb2 as m
print(pathlib.Path(m.__file__).parent.parent.parent)
PY
)"
if [[ ! -f "$GOOGLE_PROTO_ROOT/google/rpc/status.proto" ]]; then
    echo "google/rpc/status.proto not found — pip install grpcio-status" >&2
    exit 1
fi

"$PYTHON" -m grpc_tools.protoc \
    -I"$PKG" -I"$GOOGLE_PROTO_ROOT" \
    --python_out="$PKG" --grpc_python_out="$PKG" \
    "$PKG/bfruntime.proto"

# protoc emits a flat `import bfruntime_pb2`, but client.py imports the stubs as
# `bfrt_grpc.bfruntime_pb2_grpc`. Generating into a bfrt_grpc/ proto dir instead
# would fix the import but rename the module to bfrt__grpc_dot_bfruntime__pb2,
# which client.py also relies on by name. Rewriting the one line is simpler.
sed -i 's/^import bfruntime_pb2 as bfruntime__pb2$/from bfrt_grpc import bfruntime_pb2 as bfruntime__pb2/' \
    "$PKG/bfruntime_pb2_grpc.py"

echo "regenerated $PKG/bfruntime_pb2.py and bfruntime_pb2_grpc.py"

# Sanity check, best-effort: the interpreter used to *generate* the stubs is
# often a throwaway with only grpcio-tools in it, so a failure here does not
# mean the stubs are bad. Run the test suite for the real verification.
if PYTHONPATH="$ROOT/vendor" "$PYTHON" -c "import bfrt_grpc.client" 2>/dev/null; then
    echo "bfrt_grpc imports cleanly with $PYTHON"
else
    echo "note: $PYTHON cannot import bfrt_grpc (missing runtime deps there?)."
    echo "      verify with: .venv/bin/python -m pytest tests/test_vendored_bfrt.py"
fi

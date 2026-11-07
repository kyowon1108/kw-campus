#!/bin/bash
# Entry point for the web Lambdas (Lambda Web Adapter starts this and proxies HTTP to it).
# KW_APP picks the ASGI app: the public MCP by default, kwcampus.bridge.app:app for the bridge.
# -u: stdout is a pipe here; unbuffered, log lines reach CloudWatch before the sandbox freezes.
# A kw-mcp data release that build_lambda.sh copied into the bundle: kw_mcp (school_guide, department_curriculum,
# the rules) reads it instead of the wheel's copy once it verifies against the release key (kw_mcp.datasync).
# Set here, so the stack needs no new variable.
DATA_DIR="$(cd "$(dirname "$0")" && pwd)/kwcampus/bridge/static/data"
if [ -z "${KW_DATA_DIR:-}" ] && [ -f "$DATA_DIR/data.json" ]; then export KW_DATA_DIR="$DATA_DIR"; fi
exec python3 -u -m uvicorn "${KW_APP:-kwcampus.public_http:app}" --host 127.0.0.1 --port "${AWS_LWA_PORT:-8080}" \
  --log-level warning --no-access-log

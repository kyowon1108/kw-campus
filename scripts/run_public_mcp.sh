#!/bin/bash
# Entry point for the web Lambdas (Lambda Web Adapter starts this and proxies HTTP to it).
# KW_APP picks the ASGI app: the public MCP by default, kwcampus.bridge.app:app for the bridge.
exec python3 -m uvicorn "${KW_APP:-kwcampus.public_http:app}" --host 127.0.0.1 --port "${AWS_LWA_PORT:-8080}" \
  --log-level warning --no-access-log

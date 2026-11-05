#!/bin/bash
# Entry point for the public MCP Lambda (Lambda Web Adapter starts this and proxies HTTP to it).
exec python3 -m uvicorn kwcampus.public_http:app --host 127.0.0.1 --port "${AWS_LWA_PORT:-8080}" \
  --log-level warning --no-access-log

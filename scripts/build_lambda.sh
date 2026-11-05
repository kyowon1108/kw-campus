#!/usr/bin/env bash
# Build the Lambda bundle (Python 3.13, arm64) into build/lambda.
set -euo pipefail
cd "$(dirname "$0")/.."
KW_MCP_DIR="${KW_MCP_DIR:-../kw-mcp}"
rm -rf build/lambda && mkdir -p build/lambda
# kw-mcp (KLAS client, shared with the PC agent) is a local path dependency: ship its wheel.
(cd "$KW_MCP_DIR" && rm -f dist/kw_mcp-*.whl && uv build --wheel --out-dir dist --quiet)
WHEEL="$(ls "$KW_MCP_DIR"/dist/kw_mcp-*.whl)"
uv export --no-dev --no-hashes --no-emit-project --no-emit-package kw-mcp --format requirements-txt \
  -o build/requirements.txt >/dev/null
uv pip install -r build/requirements.txt --target build/lambda \
  --python-platform aarch64-manylinux2014 --python-version 3.13 --only-binary :all: --quiet
uv pip install "$WHEEL" --no-deps --target build/lambda --python-platform aarch64-manylinux2014 \
  --python-version 3.13 --quiet
cp -R src/kwcampus build/lambda/
cp scripts/run_public_mcp.sh build/lambda/run.sh && chmod +x build/lambda/run.sh
# The bridge also serves that wheel to PCs installing the agent (/install.sh).
mkdir -p build/lambda/kwcampus/bridge/static && cp "$WHEEL" build/lambda/kwcampus/bridge/static/
# ...and the Claude Desktop extension, if it has been packed (`mcpb pack . dist/kw-klas.mcpb` in kw-mcp).
if [ -f "$KW_MCP_DIR/dist/kw-klas.mcpb" ]; then
  cp "$KW_MCP_DIR/dist/kw-klas.mcpb" build/lambda/kwcampus/bridge/static/
else
  echo "warning: $KW_MCP_DIR/dist/kw-klas.mcpb not found; /dist/kw-klas.mcpb will answer 404" >&2
fi
find build/lambda -name '__pycache__' -prune -exec rm -rf {} +
du -sh build/lambda

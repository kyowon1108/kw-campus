#!/usr/bin/env bash
# Build the Lambda bundle (Python 3.13, arm64) into build/lambda.
#
# This deploys the bridge; it does not release kw-mcp. The PC agent and the Claude Desktop extension
# the bridge serves come from a finished, signed kw-mcp release (`scripts/release.sh` in kw-mcp writes
# dist/release/). This script refuses to build without one, or with one that does not verify against
# the release key, so a deploy can never ship unsigned or half-built agent code.
#
# Everything is built in build/lambda.tmp and moved into place at the end: a failed build leaves the
# last good build/lambda untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
KW_MCP_DIR="${KW_MCP_DIR:-../kw-mcp}"
RELEASE="$KW_MCP_DIR/dist/release"
OUT=build/lambda
TMP=build/lambda.tmp

if [ ! -f "$RELEASE/latest.json" ]; then
  echo "error: no kw-mcp release in $RELEASE. Run scripts/release.sh in kw-mcp first." >&2
  exit 1
fi
# Signatures (wheel, constraints, extension) against kw_mcp.release.PUBLIC_KEY, and every file's hash.
uv run --project "$KW_MCP_DIR" --frozen --quiet python -m kw_mcp.release check "$RELEASE"
read -r WHEEL CONSTRAINTS MCPB < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(m["wheel"], m["constraints"]["file"], m["mcpb"]["file"])' "$RELEASE/latest.json")

rm -rf "$TMP" && mkdir -p "$TMP/lambda"
uv export --locked --no-dev --no-hashes --no-emit-project --no-emit-package kw-mcp --format requirements-txt \
  -o "$TMP/requirements.txt" >/dev/null
uv pip install -r "$TMP/requirements.txt" --target "$TMP/lambda" \
  --python-platform aarch64-manylinux2014 --python-version 3.13 --only-binary :all: --quiet
# kw-mcp (KLAS client, shared with the PC agent): the released wheel, so the bridge runs released code.
uv pip install "$RELEASE/$WHEEL" --no-deps --target "$TMP/lambda" --python-platform aarch64-manylinux2014 \
  --python-version 3.13 --quiet
cp -R src/kwcampus "$TMP/lambda/"
cp scripts/run_public_mcp.sh "$TMP/lambda/run.sh" && chmod +x "$TMP/lambda/run.sh"
# The bridge serves the release to PCs: /install.sh and /install.ps1 install that wheel with those
# pins, agents update from latest.json, and /setup links the extension.
STATIC="$TMP/lambda/kwcampus/bridge/static"
mkdir -p "$STATIC"
cp "$RELEASE/$WHEEL" "$RELEASE/$CONSTRAINTS" "$RELEASE/$MCPB" "$RELEASE/latest.json" "$STATIC/"
uv run --project "$KW_MCP_DIR" --frozen --quiet python -m kw_mcp.release check "$STATIC" >/dev/null
find "$TMP/lambda" -name '__pycache__' -prune -exec rm -rf {} +
for f in run.sh kwcampus/__init__.py kw_mcp/__init__.py kwcampus/bridge/static/latest.json; do
  [ -e "$TMP/lambda/$f" ] || { echo "error: the bundle lacks $f" >&2; exit 1; }
done

rm -rf "$OUT.old"
if [ -d "$OUT" ]; then mv "$OUT" "$OUT.old"; fi
mv "$TMP/lambda" "$OUT"
cp "$TMP/requirements.txt" build/requirements.txt
rm -rf "$OUT.old" "$TMP"
du -sh "$OUT"

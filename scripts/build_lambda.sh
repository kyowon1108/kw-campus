#!/usr/bin/env bash
# Build the Lambda bundle (Python 3.13, arm64) into build/lambda.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build/lambda && mkdir -p build/lambda
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o build/requirements.txt >/dev/null
uv pip install -r build/requirements.txt --target build/lambda \
  --python-platform aarch64-manylinux2014 --python-version 3.13 --only-binary :all: --quiet
cp -R src/kwcampus build/lambda/
find build/lambda -name '__pycache__' -prune -exec rm -rf {} +
du -sh build/lambda

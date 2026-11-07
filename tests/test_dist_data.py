"""/dist/data/: a kw-mcp data release (signed data.json and only the files it lists) served with the right
cache headers, build_lambda's check refusing anything not signed by the release key, and the Lambda's own
knowledge tools reading the newer data (run.sh points KW_DATA_DIR at it)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kw_mcp import datasync, knowledge, release
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from kwcampus.bridge import install

ROOT = Path(__file__).resolve().parent.parent
VERSION = 20261110093000
QUESTION = "브리지자료시험질문 크와크와"


@pytest.fixture
def key(monkeypatch):
    private = Ed25519PrivateKey.generate()  # a throwaway key, never the release key
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(release, "PUBLIC_KEY", base64.b64encode(public).decode())
    return private


@pytest.fixture
def static(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(install, "DATA_DIR", tmp_path / "data")
    install._data_at.cache_clear()
    return tmp_path


@pytest.fixture
def client():
    return TestClient(Starlette(routes=[Route(path, handler) for path, handler in install.routes()]))


def ship_data(folder: Path, private, files: dict[str, bytes]) -> dict:
    """What build_lambda.sh copies into static/data: kw-mcp's dist/data (the files and a signed data.json)."""
    for name, data in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    manifest = release.sign_data(folder, list(files), VERSION, "1.2", "1.2.0", private)
    (folder / "data.json").write_text(json.dumps(manifest))
    install._data_at.cache_clear()
    return manifest


def test_dist_data_serves_only_the_listed_files_with_cache_headers(client, static, key):
    m = ship_data(static / "data", key, {"knowledge/faq.json": b'{"entries": []}', "rules/graduation.json": b"{}"})
    (static / "data" / "rules" / "secret.json").write_text("{}")  # on disk but not in data.json
    (static / "latest.json").write_text("{}")
    r = client.get("/dist/data/data.json")
    assert r.status_code == 200 and r.json()["data_version"] == VERSION and r.headers["cache-control"] == "no-cache"
    sha = m["files"]["knowledge/faq.json"]["sha256"]
    r = client.get(f"/dist/data/knowledge/faq.json?sha256={sha}")
    assert r.content == b'{"entries": []}' and r.headers["cache-control"] == install.DATA_CACHE
    assert "immutable" in install.DATA_CACHE and r.headers["content-type"].startswith("application/json")
    assert client.get("/dist/data/knowledge/faq.json").headers["cache-control"] == "no-cache"  # no hash: may change
    assert client.get(f"/dist/data/knowledge/faq.json?sha256={'0' * 64}").status_code == 404  # not these bytes
    for path in ("rules/secret.json", "rules/..%2F..%2Flatest.json", "DRY-RUN-NOT-DEPLOYABLE", "rules", ""):
        assert client.get(f"/dist/data/{path}").status_code == 404, path
    assert client.get("/dist/data").status_code == 404
    # a path that reaches the route with dot segments in it (clients normally resolve them first)
    handler = dict(install.routes())["/dist/data/{path:path}"]
    for path in ("../latest.json", "knowledge/../../latest.json", "knowledge//faq.json", "/etc/passwd"):
        scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [],
                 "path_params": {"path": path}}
        assert asyncio.run(handler(Request(scope))).status_code == 404, path


def test_unsigned_data_is_not_served(client, static, key):
    m = ship_data(static / "data", key, {"knowledge/faq.json": b'{"entries": []}'})
    (static / "data" / "data.json").write_text(json.dumps({**m, "signature": m["signature"][::-1]}))
    install._data_at.cache_clear()
    assert client.get("/dist/data/data.json").status_code == 404
    assert client.get("/dist/data/knowledge/faq.json").status_code == 404


def test_without_a_data_release_the_bridge_still_serves_the_rest(client, static):
    assert client.get("/dist/data/data.json").status_code == 404
    assert client.get("/start").status_code == 200


def test_build_lambdas_check_refuses_data_not_signed_by_the_release_key(tmp_path, key, monkeypatch):
    m = ship_data(tmp_path, key, {"knowledge/faq.json": b'{"entries": []}'})
    assert release.check_data(tmp_path)["data_version"] == VERSION
    (tmp_path / "data.json").write_text(json.dumps({**m, "min_app": "1.0.0"}))  # changed after signing
    with pytest.raises(ValueError, match="not signed"):
        release.check_data(tmp_path)
    (tmp_path / "data.json").write_text(json.dumps(m))
    # the command build_lambda.sh runs (shipped key): a throwaway or dry-run signature stops the build
    done = subprocess.run([sys.executable, "-m", "kw_mcp.release", "check-data", str(tmp_path)],
                          capture_output=True, text=True, check=False)
    assert done.returncode != 0 and "data check failed" in done.stderr
    script = (ROOT / "scripts" / "build_lambda.sh").read_text()
    assert 'kw_mcp.release check-data "$DATA_RELEASE"' in script and 'check-data "$STATIC/data"' in script


def test_run_sh_points_kw_mcp_at_the_bundled_data(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "kwcampus" / "bridge" / "static" / "data").mkdir(parents=True)
    (bundle / "run.sh").write_text((ROOT / "scripts" / "run_public_mcp.sh").read_text())
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "python3").write_text('#!/bin/sh\necho "KW_DATA_DIR=${KW_DATA_DIR:-}"\n')
    (fake / "python3").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != datasync.DATA_DIR_ENV} | {"PATH": f"{fake}:/usr/bin:/bin"}

    def run() -> str:
        return subprocess.run(["bash", str(bundle / "run.sh")], env=env, capture_output=True, text=True,
                              check=True).stdout

    assert run().strip() == "KW_DATA_DIR="  # no data release in this bundle: the wheel's own data
    (bundle / "kwcampus" / "bridge" / "static" / "data" / "data.json").write_text("{}")
    assert run().strip() == f"KW_DATA_DIR={(bundle / 'kwcampus' / 'bridge' / 'static' / 'data').resolve()}"


def test_the_lambdas_knowledge_tools_read_the_newer_data(tmp_path, key, monkeypatch):
    faq = json.loads((Path(knowledge.__file__).with_name("knowledge") / "faq.json").read_text(encoding="utf-8"))
    faq["entries"].append({**faq["entries"][0], "id": "bridge-data-entry", "questions": [QUESTION, "크와 질문", "시험"]})
    ship_data(tmp_path / "data", key, {"knowledge/faq.json": json.dumps(faq, ensure_ascii=False).encode()})
    monkeypatch.setattr(datasync, "bundled_version", lambda: VERSION - 1)  # the wheel has data_version
    monkeypatch.setattr(datasync, "app_version", lambda: "1.2.0")
    monkeypatch.setattr(datasync, "_seen", None)
    monkeypatch.setenv(datasync.DATA_DIR_ENV, str(tmp_path / "data"))
    assert knowledge.guide(QUESTION)["answers"][0]["id"] == "bridge-data-entry"
    assert datasync.sync(force=True) == "off"  # the Lambda never downloads
    monkeypatch.setattr(datasync, "bundled_version", lambda: VERSION)  # a release with newer data of its own
    monkeypatch.setattr(datasync, "_seen", None)
    assert all(a["id"] != "bridge-data-entry" for a in knowledge.guide(QUESTION)["answers"])


def test_an_installed_copy_syncs_through_the_bridge_route(client, static, key, tmp_path, monkeypatch):
    """End to end: kw_mcp.datasync on a PC fetching this route's data.json and files (hash-addressed)."""
    from kw_mcp import agent

    ship_data(static / "data", key, {"rules/graduation.json": b'{"version": {"built_at": "2099-06-06"}}'})
    monkeypatch.setattr(agent, "CONFIG_DIR", tmp_path / "pc")
    monkeypatch.setattr(datasync, "bundled_version", lambda: VERSION - 1)
    monkeypatch.setattr(datasync, "app_version", lambda: "1.2.0")
    monkeypatch.setattr(datasync, "_seen", None)
    monkeypatch.delenv(datasync.DATA_DIR_ENV, raising=False)
    assert datasync.sync(client, force=True) == f"updated to {VERSION}"
    assert datasync.path("rules/graduation.json").read_bytes() == b'{"version": {"built_at": "2099-06-06"}}'
    assert datasync.sync(client, force=True) == "up to date"

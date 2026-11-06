"""The installer scripts, /setup, /privacy and /dist serve exactly the signed kw-mcp release."""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kw_mcp import release
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from kwcampus.bridge import install

BASE = "https://bridge.example"
CUTOFF = "2026-11-01T00:00:00Z"


@pytest.fixture
def key(monkeypatch):
    private = Ed25519PrivateKey.generate()  # a throwaway key, never the release key
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(release, "PUBLIC_KEY", base64.b64encode(public).decode())
    return private


@pytest.fixture
def static(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "STATIC_DIR", tmp_path)
    install._release_at.cache_clear()
    return tmp_path


@pytest.fixture
def client():
    app = Starlette(routes=[Route(path, handler) for path, handler in install.routes(BASE)])
    return TestClient(app)


def ship(static, private, version="0.9.1"):
    """What build_lambda.sh copies into static/: wheel, pins and a signed latest.json."""
    wheel = static / f"kw_mcp-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("kw_mcp/__init__.py", "")
        z.writestr(f"kw_mcp-{version}.dist-info/METADATA", f"Name: kw-mcp\nVersion: {version}\n")
    pins = static / release.constraints_name(version)
    pins.write_text(f"# kw-mcp {version}\n# exclude-newer: {CUTOFF}\nhttpx==0.28.1\n")
    manifest = release.sign(wheel, private)
    manifest["constraints"] = release.sign_constraints(pins, version, private)
    (static / "latest.json").write_text(json.dumps(manifest))
    install._release_at.cache_clear()
    return manifest


def test_scripts_install_the_signed_wheel_with_its_signed_pins(client, static, key):
    assert client.get("/install.sh").status_code == 503  # nothing shipped yet
    m = ship(static, key)
    pins = m["constraints"]
    sh = client.get("/install.sh").text
    assert f'curl -fsSL -o "$TMP/{m["wheel"]}" "{BASE}/dist/{m["wheel"]}"' in sh
    assert f'curl -fsSL -o "$TMP/{pins["file"]}" "{BASE}/dist/{pins["file"]}"' in sh
    assert f'{m["sha256"]}  $TMP/{m["wheel"]}' in sh and f'{pins["sha256"]}  $TMP/{pins["file"]}' in sh
    assert f'--no-build --constraints "$TMP/{pins["file"]}"' in sh and f'--exclude-newer "{CUTOFF}"' in sh
    assert '.config/kw-campus/releases/"' in sh  # kept for a rollback
    assert 'kw-mcp link "$INVITE" --server' in sh and "__" not in sh.replace("__init__", "")
    ps1 = client.get("/install.ps1").text
    assert f"-ne '{m['sha256']}'" in ps1 and f"-ne '{pins['sha256']}'" in ps1
    assert f"--no-build --constraints $pins --exclude-newer '{CUTOFF}'" in ps1
    assert "([System.Uri]$whl).AbsoluteUri" in ps1  # percent-encodes spaces and Korean in the profile path
    assert "-replace" not in ps1 and "__" not in ps1
    assert client.get(f"/dist/{m['wheel']}").content == (static / m["wheel"]).read_bytes()
    assert client.get(f"/dist/{pins['file']}").text.startswith("# kw-mcp")
    assert client.get("/dist/..%2Fapp.py").status_code == 404


def test_an_unsigned_or_incomplete_release_is_not_installable(client, static, key):
    m = ship(static, key)
    (static / "latest.json").write_text(json.dumps({**m, "signature": m["signature"][::-1]}))
    install._release_at.cache_clear()
    assert client.get("/install.sh").status_code == 503
    assert client.get(f"/dist/{m['wheel']}").status_code == 404
    unpinned = {k: v for k, v in m.items() if k != "constraints"}  # an old-style manifest
    (static / "latest.json").write_text(json.dumps(unpinned))
    install._release_at.cache_clear()
    assert client.get("/install.ps1").status_code == 503
    ship(static, key)
    (static / release.constraints_name("0.9.1")).unlink()
    install._release_at.cache_clear()
    assert client.get("/install.sh").status_code == 503


def test_setup_page_and_extension_downloads(client, static, key):
    page = client.get("/setup").text
    assert "kw-mcp code" in page and "/dist/kw-klas.mcpb" in page and "(아직 없음)" in page and "/privacy" in page
    assert "보조 경로" in page and "항상 켜 두는 PC" in page  # web/mobile and ChatGPT need an always-on PC
    assert "동의하는 것으로 봅니다" in page  # one-line consent for the agent path
    assert "kw-mcp connections" in page and "kw-mcp revoke" in page
    # A malformed %-escape in the fragment must not stop the script before the boxes are filled.
    script = page.split("<script>")[1]
    assert "try { raw = decodeURIComponent(raw); }" in script
    assert script.index("`${base}/mcp`") < script.index("decodeURIComponent")
    assert client.get("/dist/kw-klas.mcpb").status_code == 404
    (static / "kw-klas.mcpb").write_bytes(b"PK mcpb")
    assert client.get("/dist/kw-klas.mcpb").content == b"PK mcpb"
    zipped = client.get("/dist/kw-klas.zip")  # same file for "Install Unpacked Extension"
    assert zipped.content == b"PK mcpb" and zipped.headers["content-type"] == "application/zip"
    assert hashlib.sha256(b"PK mcpb").hexdigest() in client.get("/setup").text
    assert client.get("/dist/latest.json").status_code == 404
    (static / "latest.json").write_text('{"version": "0.2.0"}')
    r = client.get("/dist/latest.json")
    assert r.json() == {"version": "0.2.0"} and r.headers["cache-control"] == "no-cache"


def test_privacy_page_has_the_article_30_items():
    page = install.PRIVACY_PAGE
    for needed in ("처리 목적", "보유 기간", "위탁", "국외", "AWS", "서울", "Anthropic", "OpenAI", "권리", "안전성 확보",
                   "보호책임자", "kyowon1108@gmail.com", "72시간", "Discord", "kw-mcp unlink", "kw-mcp revoke",
                   "동의하는 것으로 봅니다", "180일", "2주"):
        assert needed in page, needed
    assert "간편 연결" not in page and "비밀번호를 승인 페이지" not in page  # that mode is gone
    assert "따로 저장하지 않습니다" in page  # Discord roles live only in Discord
    assert "초대해 준 사람에게" not in page  # a named contact instead

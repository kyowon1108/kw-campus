"""/start, /setup, /privacy, and /dist serving exactly the signed kw-mcp release and the extension."""

from __future__ import annotations

import base64
import json
import re
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kw_mcp import release
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from kwcampus import invites
from kwcampus.bridge import install, store

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
    app = Starlette(routes=[Route(path, handler) for path, handler in install.routes()])
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


def test_dist_serves_the_signed_wheel_and_pins_for_installed_agents(client, static, key):
    """No installer scripts any more, but agents already installed still update from /dist."""
    m = ship(static, key)
    pins = m["constraints"]
    assert client.get(f"/dist/{m['wheel']}").content == (static / m["wheel"]).read_bytes()
    assert client.get(f"/dist/{pins['file']}").text.startswith("# kw-mcp")
    assert client.get("/dist/latest.json").json()["wheel"] == m["wheel"]
    assert client.get("/dist/..%2Fapp.py").status_code == 404
    for gone in ("/install.sh", "/install.ps1"):
        assert client.get(gone).status_code == 404


def test_an_unsigned_or_incomplete_release_is_not_served(client, static, key):
    m = ship(static, key)
    (static / "latest.json").write_text(json.dumps({**m, "signature": m["signature"][::-1]}))
    install._release_at.cache_clear()
    assert client.get(f"/dist/{m['wheel']}").status_code == 404
    ship(static, key)
    (static / release.constraints_name("0.9.1")).unlink()
    install._release_at.cache_clear()
    assert client.get(f"/dist/{m['wheel']}").status_code == 404


def _script(page: str) -> str:
    return re.search(r"<script nonce=\"([^\"]+)\">(.*?)</script>", page, re.DOTALL).group(2)


def test_start_page(client):
    r = client.get("/start")
    page = r.text
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    nonce = re.search(r"<script nonce=\"([^\"]+)\">", page).group(1)
    csp = r.headers["content-security-policy"]
    assert f"script-src 'nonce-{nonce}'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["referrer-policy"] == "no-referrer"
    # one line about what it reads, three real questions
    assert "Claude가 내 KLAS 과제·성적·출석과 학교 공지·학식·학사일정을 보고 답해요." in page
    for needed in ("이번 주 과제 뭐 남았어?", "복전하려면 몇 학점 들어야 돼?", "내일 학식 뭐야?"):
        assert needed in page, needed
    # four one-line steps: Discord invite, Claude Desktop, the extension (and the Windows Store fallback), web and phone
    steps = re.search(r'<ol class="steps">(.*?)</ol>', page, re.DOTALL).group(1)
    assert steps.count("<li>") == 4
    assert "디스코드 #설치-안내에서 ‘설치하기’" in steps and 'href="https://claude.ai/download"' in steps
    # no release in this bundle: plain links; with one, each link carries ?v=<version> (its own cache key)
    assert re.search(r'href="/dist/kw-campus\.mcpb(\?v=[0-9][0-9A-Za-z.]*)?"', steps)
    assert re.search(r'href="/dist/kw-campus\.zip(\?v=[0-9][0-9A-Za-z.]*)?"', steps)
    assert "학번·비밀번호·초대 코드" in steps and "“웹이랑 폰에서도 쓰게 연결해 줘”라고 말해요" in steps
    # two short notes and one footer line
    assert "비밀번호는 내 PC에만 저장돼요." in page and "PC가 꺼져 있으면 웹·폰에서 내 정보는 안 보여요." in page
    footer = re.search(r"<footer>(.*?)</footer>", page).group(1)
    assert "학교 공식 서비스 아님" in footer and 'href="/privacy"' in footer and "mailto:kyowon1108@gmail.com" in footer
    # never asks for a password, and no terminal commands
    assert 'type="password"' not in page and "<input" not in page and "<form" not in page
    assert "kw-mcp" not in page and "curl" not in page and "install.sh" not in page
    assert "<link" not in page and "src=\"http" not in page  # no external assets


def test_start_page_shows_the_invite_only_through_its_script(client):
    page = client.get("/start#kwc_should-not-matter").text  # a fragment never reaches the server anyway
    assert 'id="invite-box" class="card accent" hidden' in page and '<code id="invite"></code>' in page
    assert "kwc_should" not in page
    script = _script(page)
    assert "location.hash" in script and "fetch(" not in script and "XMLHttpRequest" not in script
    assert "data.s === location.origin" in script  # an invite for another server isn't shown
    assert script.index("button[data-copy]") < script.index("decodeURIComponent")  # copy works whatever the hash


def test_start_page_never_links_the_discord_server(client, monkeypatch):
    """Invite-only: no join link on /start, even with KW_DISCORD_INVITE_URL set somewhere."""
    monkeypatch.setenv("KW_DISCORD_INVITE_URL", "https://discord.gg/abc")
    page = client.get("/start").text
    assert "discord.gg" not in page and "디스코드 열기" not in page


def test_setup_sends_to_start(client):
    r = client.get("/setup", follow_redirects=False)
    # no fragment in Location: the browser carries /setup#kwc_... over to /start#kwc_...
    assert r.status_code == 302 and r.headers["location"] == "/start"


def test_extension_downloads(client, static):
    assert client.get("/dist/kw-campus.mcpb").status_code == 404
    (static / "kw-campus.mcpb").write_bytes(b"PK mcpb")
    assert client.get("/dist/kw-campus.mcpb").content == b"PK mcpb"
    assert client.get("/dist/kw-klas.mcpb").content == b"PK mcpb"  # links handed out before 1.0.0
    assert client.get("/dist/kw-klas.zip").content == b"PK mcpb"
    # every release has the same 1980 Last-Modified: a browser must revalidate, never reuse an older copy
    assert client.get("/dist/kw-campus.mcpb").headers["cache-control"] == "no-cache"
    assert client.get("/dist/kw-campus.mcpb?v=1.1.0").content == b"PK mcpb"
    zipped = client.get("/dist/kw-campus.zip")  # same file for "Install Unpacked Extension"
    assert zipped.content == b"PK mcpb" and zipped.headers["content-type"] == "application/zip"
    assert client.get("/dist/latest.json").status_code == 404
    (static / "latest.json").write_text('{"version": "0.2.0"}')
    r = client.get("/dist/latest.json")
    assert r.json() == {"version": "0.2.0"} and r.headers["cache-control"] == "no-cache"


def test_privacy_page_has_the_article_30_items(client):
    page = client.get("/privacy").text
    assert page == install.privacy_page()
    grant_days = f"{store.GRANT_TTL // 86400}일"  # the grant lifetime the code enforces, not a copy of it
    for needed in ("처리 목적", "보유 기간", "위탁", "국외", "AWS", "서울", "Anthropic", "OpenAI", "권리", "안전성 확보",
                   "보호책임자", "kyowon1108@gmail.com", "72시간", "Discord", "커넥터를 지우면",
                   "동의하는 것으로 봅니다", grant_days, "2주"):
        assert needed in page, needed
    assert "kw-mcp" not in page and "방법 1" not in page  # no terminal commands, one way to install
    # Discord-issued invites: who got them, the hash, when, and the cap the code enforces
    invite_row = re.search(r"<tr><td>Discord에서 받은 초대 코드(.*?)</tr>", page).group(1)
    for needed in ("Discord 사용자 ID", "코드의 해시", "받은 시각", f"한 사람당 {invites.MAX_PER_MEMBER}개"):
        assert needed in invite_row, needed
    assert f"받을 수 있는 코드는 {invites.MAX_PER_MEMBER}개" in page and "코드 자체는 저장하지 않습니다" in page
    # the 30-minute authorize request and the kw_pair cookie
    assert store.AUTHREQ_TTL == 1800 and "__AUTHREQ_MIN__" not in page and "30분 동안 열어 둘 수 있습니다" in page
    assert "<code>kw_pair</code>" in page and "<code>/oauth</code> 주소에만" in page
    assert f"길어도 {store.PAIR_TTL // 60}분 뒤" in page
    assert "180일" not in page and "__" not in page  # every period marker filled in
    assert "간편 연결" not in page and "비밀번호를 승인 페이지" not in page  # that mode is gone
    assert "따로 저장하지 않습니다" in page  # Discord roles live only in Discord (invites are another paragraph)
    assert "초대해 준 사람에게" not in page  # a named contact instead

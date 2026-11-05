import base64
import hashlib
import json
import os
import re
import time
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.testclient import TestClient

from kwcampus import params
from kwcampus.bridge import app as bridge
from kwcampus.bridge import crypto, password, store, ws

INVITE = "friend-token-0123456789abcdef"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeDevice:
    """Signs like WebCrypto (ECDSA P-256, raw r||s) and encrypts like the extension."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        nums = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64u(nums.x.to_bytes(32, "big")), "y": b64u(nums.y.to_bytes(32, "big"))}
        self.uid = None

    def sign(self, message: str) -> str:
        r, s = decode_dss_signature(self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))
        return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    @staticmethod
    def encrypt(key_b64u: str, request_id: str, data: dict) -> dict:
        iv = os.urandom(12)
        ct = AESGCM(crypto.b64u_decode(key_b64u)).encrypt(iv, json.dumps(data).encode(), request_id.encode())
        return {"request_id": request_id, "iv": b64u(iv), "ct": b64u(ct)}


SNAPSHOT = {
    "semester": "2026,2", "fetched_at": "2026-11-05T12:00:00+09:00",
    "courses": [{"subj": "U2026A", "yearhakgi": "2026,2", "subjNm": "데이터마이닝", "profNm": "김교수",
                 "lctrumSchdulInfo": "목 3교시/새빛101", "subjLabel": "x"}],
    "course_posts": [{"subjNm": "데이터마이닝", "typeNm": "공지사항", "title": "휴강 안내", "registDt": "2026-11-05T01:00:00+00:00"}],
    "sections": {"U2026A": {
        "tasks": [{"title": "HW3", "startdate": "2026-11-02 00:00:00", "expiredate": "2099-11-07 23:59:59",
                   "reexpiredate": None, "submityn": "N", "score": None}],
        "online": [], "projects": [], "quizzes": []}},
}


@pytest.fixture
def env(aws, monkeypatch):
    dynamodb = boto3.resource("dynamodb")
    t = dynamodb.create_table(
        TableName="kw-bridge",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    aws["ssm"].put_parameter(Name="/kw/bridge/jwt-secret", Type="SecureString", Value="test-secret-" + "x" * 32)
    aws["ssm"].put_parameter(Name="/kw/bridge/seal-key", Type="SecureString", Value=b64u(os.urandom(32)))
    aws["ssm"].put_parameter(Name="/kw/mcp/tokens", Type="String", Value=json.dumps([hashlib.sha256(INVITE.encode()).hexdigest()]))
    params.clear_cache()
    bridge._SNAP.clear()
    sent = []

    def fake_post(conn, message):
        # The "extension" answers immediately by posting an encrypted result.
        sent.append((conn, message))
        if message["type"] == "call":
            body = FakeDevice.encrypt(message["key"], message["request_id"],
                                      {"result": {"op": message["op"], **message["args"]}})
            store.put_result(t, body["request_id"], body["iv"], body["ct"])
            return True
        if message["type"] != "snapshot":
            return True
        body = FakeDevice.encrypt(message["key"], message["request_id"], SNAPSHOT)
        store.put_result(t, body["request_id"], body["iv"], body["ct"])
        return True

    monkeypatch.setattr(bridge, "_post_to_connection", fake_post)
    with TestClient(bridge.build_app(), base_url="http://localhost:8080") as client:
        yield {"client": client, "table": t, "sent": sent}


def register(client, device):
    r = client.post("/device/register", json={"invite": INVITE, "public_key": device.jwk})
    assert r.status_code == 201, r.text
    assert r.json()["mcp_url"] == bridge.RESOURCE
    device.uid = r.json()["uid"]
    return device


def signed(device, purpose):
    ts = str(int(time.time()))
    return {"uid": device.uid, "ts": ts, "sig": device.sign(f"kw-{purpose}:{device.uid}:{ts}")}


def pair_code(client, device):
    r = client.post("/device/pair", json=signed(device, "pair"))
    assert r.status_code == 200, r.text
    return r.json()["code"]


def authorize(client, verifier="v" * 43):
    reg = client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s1",
        "code_challenge": crypto.pkce_s256(verifier), "code_challenge_method": "S256"})
    assert page.status_code == 200 and "읽기 전용" in page.text and "<b>claude.ai</b>" in page.text
    nonce = json.loads(re.search(r"const nonce = (\"[^\"]+\");", page.text).group(1))
    return reg, nonce


def oauth(client, device):
    verifier = crypto.random_token(32)
    reg, nonce = authorize(client, verifier)
    code = pair_code(client, device).lower()  # case and the dash don't matter
    approved = client.post("/oauth/approve", json={"nonce": nonce, "code": code}).json()
    q = parse_qs(urlparse(approved["redirect_to"]).query)
    assert q["state"] == ["s1"]
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                              "code_verifier": verifier, "client_id": reg["client_id"],
                                              "redirect_uri": REDIRECT}).json()
    return reg, tokens


def mcp_call(client, token, method, params_=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params_} if params_ is not None else {})}
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}",
                                                  "Accept": "application/json, text/event-stream",
                                                  "MCP-Protocol-Version": "2025-06-18"})


def test_metadata_and_unauthenticated_mcp(env):
    c = env["client"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["issuer"] == bridge.ISSUER
    prm = c.get("/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200 and prm.json()["resource"] == bridge.RESOURCE
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    assert c.get("/mcp").status_code == 405


def test_full_flow_tools_come_from_the_extension(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1")
    _, tokens = oauth(c, device)
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]

    listed = mcp_call(c, tokens["access_token"], "tools/list").json()["result"]["tools"]
    assert {"this_week", "today_briefing", "my_notices", "my_assignments", "latest_notices"} <= {t["name"] for t in listed}

    out = mcp_call(c, tokens["access_token"], "tools/call", {"name": "my_assignments", "arguments": {}}).json()
    rows = out["result"]["structuredContent"]["result"]
    assert rows[0]["title"] == "HW3" and rows[0]["course"] == "데이터마이닝"
    assert env["sent"][0][1]["type"] == "paired" and env["sent"][0][1]["redirect_host"] == "claude.ai"
    conn, message = env["sent"][1]
    assert conn == "conn-1" and message["type"] == "snapshot" and message["result_url"].endswith("/device/result")
    # nothing personal left in the table after the call
    leftovers = [i["PK"] for i in env["table"].scan()["Items"] if i["PK"].startswith("RESULT#")]
    assert leftovers == []


def test_refresh_rotates_and_old_refresh_dies(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg, tokens = oauth(c, device)
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": reg["client_id"]}
    new = c.post("/oauth/token", data=data).json()
    assert new["access_token"] and new["refresh_token"] != tokens["refresh_token"]
    assert c.post("/oauth/token", data=data).json()["error"] == "invalid_grant"


def test_connection_code_is_single_use_and_guessing_closes_the_request(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    _, nonce = authorize(c)
    code = pair_code(c, device)
    assert c.post("/oauth/approve", json={"nonce": nonce, "code": "AAAA-AAAA"}).status_code == 403
    assert c.post("/oauth/approve", json={"nonce": nonce, "code": code}).status_code == 200
    _, nonce2 = authorize(c)
    assert c.post("/oauth/approve", json={"nonce": nonce2, "code": code}).status_code == 403  # used up
    for _ in range(4):
        c.post("/oauth/approve", json={"nonce": nonce2, "code": "BBBB-BBBB"})
    fresh = pair_code(c, device)
    assert c.post("/oauth/approve", json={"nonce": nonce2, "code": fresh}).status_code == 400  # request closed


def test_pair_needs_device_signature_and_unlink_revokes_tokens(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    forged = signed(device, "pair") | {"sig": FakeDevice().sign("kw-pair:x:0")}
    assert c.post("/device/pair", json=forged).status_code == 403
    _, tokens = oauth(c, device)
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 200
    assert c.post("/device/unlink", json=signed(device, "pair")).status_code == 403  # wrong purpose
    assert c.post("/device/unlink", json=signed(device, "unlink")).status_code == 200
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 401


def test_register_needs_invite_and_unknown_redirects_are_refused(env):
    c = env["client"]
    assert c.post("/device/register", json={"invite": "nope", "public_key": FakeDevice().jwk}).status_code == 403
    for bad in ("http://evil.example/cb", "https://evil.example/cb", "https://claude.ai.evil.example/cb",
                "https://evil.example\\@claude.ai/api/mcp/auth_callback", "http://evil.example\\@localhost/cb",
                "http://user@localhost/cb", "https://claude.ai/other",
                "https://chatgpt.com/connector/oauth/abc/../../evil", "https://chatgpt.com/connector/oauth/a\\@evil.example"):
        assert c.post("/oauth/register", json={"redirect_uris": [bad]}).status_code == 400
    for good in ("https://chatgpt.com/connector_platform_oauth_redirect", "http://localhost:6274/oauth/callback",
                 "https://chatgpt.com/connector/oauth/abc_DEF-123"):
        assert c.post("/oauth/register", json={"redirect_uris": [good]}).status_code == 201


def test_tool_error_when_extension_not_connected(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    _, tokens = oauth(c, device)
    out = mcp_call(c, tokens["access_token"], "tools/call", {"name": "my_courses", "arguments": {}}).json()
    assert out["result"]["isError"] and "에이전트가 연결돼 있지 않습니다" in out["result"]["content"][0]["text"]


def test_ws_connect_requires_fresh_device_signature(env):
    device = FakeDevice()
    register(env["client"], device)
    ts = str(int(time.time()))

    def connect(uid, ts_, sig):
        return ws.handler({"requestContext": {"routeKey": "$connect", "connectionId": "abc"},
                           "queryStringParameters": {"uid": uid, "ts": ts_, "sig": sig}}, None)

    assert connect(device.uid, ts, device.sign(f"kw-connect:{device.uid}:{ts}"))["statusCode"] == 200
    assert store.latest_connection(env["table"], device.uid) == "abc"
    old = str(int(time.time()) - 3600)
    assert connect(device.uid, old, device.sign(f"kw-connect:{device.uid}:{old}"))["statusCode"] == 401
    assert connect(device.uid, ts, FakeDevice().sign(f"kw-connect:{device.uid}:{ts}"))["statusCode"] == 401
    ws.handler({"requestContext": {"routeKey": "$disconnect", "connectionId": "abc"}}, None)
    assert store.latest_connection(env["table"], device.uid) is None


def test_installer_serves_scripts_and_the_shipped_wheel(env, tmp_path, monkeypatch):
    from kwcampus.bridge import install

    c = env["client"]
    monkeypatch.setattr(install, "STATIC_DIR", tmp_path)
    assert c.get("/install.sh").status_code == 503  # nothing shipped yet
    (tmp_path / "kw_mcp-0.1.0-py3-none-any.whl").write_bytes(b"PK fake wheel")
    sh = c.get("/install.sh").text
    digest = hashlib.sha256(b"PK fake wheel").hexdigest()
    assert 'curl -fsSL -o "$TMP/kw_mcp-0.1.0-py3-none-any.whl" "http://localhost:8080/dist/kw_mcp-0.1.0-py3-none-any.whl"' in sh
    assert f"{digest}  $TMP/" in sh and '"kw-mcp @ file://$TMP/' in sh and 'kw-mcp link "$INVITE"' in sh
    ps1 = c.get("/install.ps1").text
    assert "/dist/kw_mcp-0.1.0-py3-none-any.whl" in ps1 and f"-ne '{digest}'" in ps1
    assert "$whl.Replace('\\', '/')" in ps1 and "-replace" not in ps1  # a lone '\\' is not a valid regex
    assert c.get("/dist/kw_mcp-0.1.0-py3-none-any.whl").content == b"PK fake wheel"
    assert c.get("/dist/..%2Fapp.py").status_code == 404
    assert "kw-mcp code" in c.get("/setup").text and "/dist/kw-klas.mcpb" in c.get("/setup").text
    assert c.get("/dist/kw-klas.mcpb").status_code == 404
    (tmp_path / "kw-klas.mcpb").write_bytes(b"PK mcpb")
    assert c.get("/dist/kw-klas.mcpb").content == b"PK mcpb"
    assert c.get("/dist/latest.json").status_code == 404
    (tmp_path / "latest.json").write_text('{"version": "0.2.0"}')
    r = c.get("/dist/latest.json")
    assert r.json() == {"version": "0.2.0"} and r.headers["cache-control"] == "no-cache"


class FakeKlas:
    """Stands in for kw_mcp's KlasClient + KlasService inside bridge.password."""

    accounts = {"2022000000": "right-pass"}
    logins = 0

    def __init__(self, login_id, secret):
        self.login_id, self.secret = login_id, secret

    async def login(self):
        from kw_mcp.klas import KlasBadPassword

        FakeKlas.logins += 1
        if self.accounts.get(self.login_id) != self.secret:
            raise KlasBadPassword("개인번호 또는 비밀번호가 일치하지 않습니다. — 10분 동안 재시도하지 않습니다.")

    async def aclose(self):
        pass


class FakeService:
    def __init__(self, client):
        self.client = client

    async def snapshot(self):
        await self.client.login()
        return SNAPSHOT


@pytest.fixture
def fake_klas(monkeypatch, aws):
    aws["ssm"].put_parameter(Name="/kw/bridge/password-mode", Type="String", Value="true")
    monkeypatch.setattr(password, "KlasClient", FakeKlas)
    monkeypatch.setattr(password, "KlasService", FakeService)
    monkeypatch.setattr(FakeKlas, "accounts", {"2022000000": "right-pass"})
    password._SESSIONS.clear()
    FakeKlas.logins = 0
    return FakeKlas


def password_oauth(client, secret="right-pass", invite=INVITE):
    verifier = crypto.random_token(32)
    reg, nonce = authorize(client, verifier)
    r = client.post("/oauth/approve", json={"nonce": nonce, "mode": "password", "invite": invite,
                                            "login_id": "2022000000", "password": secret})
    if r.status_code != 200:
        return reg, r
    q = parse_qs(urlparse(r.json()["redirect_to"]).query)
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                              "code_verifier": verifier, "client_id": reg["client_id"],
                                              "redirect_uri": REDIRECT}).json()
    return reg, tokens


def test_password_mode_answers_tools_and_stores_no_login(env, fake_klas):
    c = env["client"]
    reg, tokens = password_oauth(c, invite=f"kwc_{b64u(json.dumps({'s': 'x', 't': INVITE}).encode())}")
    out = mcp_call(c, tokens["access_token"], "tools/call", {"name": "my_assignments", "arguments": {}}).json()
    assert out["result"]["structuredContent"]["result"][0]["title"] == "HW3"
    assert [m for _, m in env["sent"] if m["type"] == "snapshot"] == []  # no PC involved
    dump = json.dumps(env["table"].scan()["Items"], default=str)
    assert "2022000000" not in dump and "right-pass" not in dump
    assert "right-pass" not in tokens["access_token"] and "2022000000" not in tokens["refresh_token"]
    # refresh keeps the sealed login working
    new = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                       "client_id": reg["client_id"]}).json()
    bridge._SNAP.clear()
    again = mcp_call(c, new["access_token"], "tools/call", {"name": "my_courses", "arguments": {}}).json()
    assert again["result"]["structuredContent"]["result"][0]["course"] == "데이터마이닝"


def test_password_mode_refuses_bad_invite_before_touching_klas(env, fake_klas):
    _, r = password_oauth(env["client"], invite="wrong")
    assert r.status_code == 403 and fake_klas.logins == 0
    _, r = password_oauth(env["client"], secret="typo")
    assert r.status_code == 403 and "일치하지 않습니다" in r.json()["error_description"]
    assert "10분" not in r.json()["error_description"]  # the CLI hint about retries is cut


def test_password_change_stops_the_connection_and_forces_reconnect(env, fake_klas, monkeypatch):
    c = env["client"]
    reg, tokens = password_oauth(c)
    monkeypatch.setattr(FakeKlas, "accounts", {"2022000000": "changed"})
    password._SESSIONS.clear()
    out = mcp_call(c, tokens["access_token"], "tools/call", {"name": "this_week", "arguments": {}}).json()
    assert out["result"]["isError"] and "다시 연결" in out["result"]["content"][0]["text"]
    logins = fake_klas.logins
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 401  # client re-authorizes
    refresh = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                           "client_id": reg["client_id"]}).json()
    assert refresh["error"] == "invalid_grant" and fake_klas.logins == logins  # no more KLAS attempts


def test_sealed_login_is_bound_to_its_client(env, fake_klas):
    login = password.Login("cid1", "2022000000", "right-pass")
    blob = password.seal(login, "client-a")
    assert password.unseal(blob, "client-a") == login
    assert password.unseal(blob, "client-b") is None
    assert "right-pass" not in repr(login)


def test_public_json_api_is_served_with_the_invite_token(env):
    c = env["client"]
    assert c.get(f"/c/{INVITE}/api/notices").status_code == 200
    assert c.get("/c/wrong-token/api/notices").status_code == 404
    assert c.post(f"/c/{INVITE}/mcp", json={}).status_code == 404


def test_password_mode_is_off_by_default_and_switching_off_ends_connections(env, aws, monkeypatch):
    c = env["client"]
    _, nonce = authorize(c)
    page = c.get("/oauth/authorize", params={"response_type": "code", "client_id": c.post(
        "/oauth/register", json={"client_name": "x", "redirect_uris": [REDIRECT]}).json()["client_id"],
        "redirect_uri": REDIRECT, "code_challenge": "a" * 43, "code_challenge_method": "S256"}).text
    assert 'id="pw"' not in page and 'id="pc"' in page
    r = c.post("/oauth/approve", json={"nonce": nonce, "mode": "password", "invite": INVITE,
                                       "login_id": "2022000000", "password": "right-pass"})
    assert r.status_code == 403
    # a connection made while the switch was on stops when it goes off
    aws["ssm"].put_parameter(Name="/kw/bridge/password-mode", Type="String", Value="true")
    monkeypatch.setattr(password, "KlasClient", FakeKlas)
    monkeypatch.setattr(password, "KlasService", FakeService)
    _, tokens = password_oauth(c)
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 200
    aws["ssm"].put_parameter(Name="/kw/bridge/password-mode", Type="String", Value="false", Overwrite=True)
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 401


def test_results_only_fill_slots_the_bridge_opened(env):
    c = env["client"]
    assert c.post("/device/result", json={"request_id": "made-up", "iv": "aa", "ct": "bb"}).status_code == 409
    store.open_result(env["table"], "r1")
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "x" * 300_001}).status_code == 400
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "bb"}).status_code == 202
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "cc"}).status_code == 409


def test_device_signatures_are_single_use(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    body = signed(device, "pair")
    assert c.post("/device/pair", json=body).status_code == 200
    assert c.post("/device/pair", json=body).status_code == 403


def test_single_item_reads_need_an_updated_agent(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "old-agent")  # version 1: snapshot only
    _, tokens = oauth(c, device)
    args = {"name": "my_assignment_detail", "arguments": {"course": "데이터", "title": "HW3"}}
    old = mcp_call(c, tokens["access_token"], "tools/call", args).json()["result"]
    assert old["isError"] and "업데이트" in old["content"][0]["text"]
    store.add_connection(env["table"], device.uid, "new-agent", 2)
    new = mcp_call(c, tokens["access_token"], "tools/call", args).json()["result"]
    assert new["structuredContent"] == {"op": "assignment_detail", "course": "데이터", "title": "HW3"}
    sent = [m for conn, m in env["sent"] if m["type"] == "call"]
    assert sent[-1]["op"] == "assignment_detail" and sent[-1]["args"] == {"course": "데이터", "title": "HW3"}


def test_ws_connect_records_agent_version(env):
    device = FakeDevice()
    register(env["client"], device)
    ts = str(int(time.time()))
    q = {"uid": device.uid, "ts": ts, "sig": device.sign(f"kw-connect:{device.uid}:{ts}"), "v": "2"}
    assert ws.handler({"requestContext": {"routeKey": "$connect", "connectionId": "c2"},
                       "queryStringParameters": q}, None)["statusCode"] == 200
    assert store.latest_connection_info(env["table"], device.uid) == ("c2", 2)

import ast
import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from kwcampus import params, public_data
from kwcampus.bridge import app as bridge
from kwcampus.bridge import crypto, shapes, store, ws
from kwcampus.config import KST

INVITE = "friend-token-0123456789abcdef"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeDevice:
    """Signs like the PC agent (ECDSA P-256, raw r||s) and encrypts like it."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        nums = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64u(nums.x.to_bytes(32, "big")), "y": b64u(nums.y.to_bytes(32, "big"))}
        self.uid = None

    def sign_rs(self, message: str) -> tuple[int, int]:
        return decode_dss_signature(self.key.sign(message.encode(), ec.ECDSA(hashes.SHA256())))

    def sign(self, message: str) -> str:
        r, s = self.sign_rs(message)
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
    aws["ssm"].put_parameter(Name="/kw/mcp/tokens", Type="String", Value=json.dumps([hashlib.sha256(INVITE.encode()).hexdigest()]))
    params.clear_cache()
    public_data.clear_cache()
    bridge._SNAP.clear()
    sent = []
    snapshots = {}  # uid -> snapshot its agent answers with (default SNAPSHOT)
    answers = {"department": {"result": None}}  # op -> the agent's whole answer; other ops echo their args

    def fake_post(conn, message):
        # The "agent" answers immediately by posting an encrypted result.
        sent.append((conn, message))
        if message["type"] == "call":
            echo = {"op": message["op"], **message["args"]}
            result = [echo] if message["op"] in ("attendance", "exam_info") else echo  # list-returning ops
            answer = answers.get(message["op"], {"result": result})
            body = FakeDevice.encrypt(message["key"], message["request_id"], answer)
            store.put_result(t, body["request_id"], body["iv"], body["ct"])
            return True
        if message["type"] != "snapshot":
            return True
        uid = t.get_item(Key={"PK": f"CONN#{conn}", "SK": "META"}).get("Item", {}).get("uid")
        body = FakeDevice.encrypt(message["key"], message["request_id"], snapshots.get(uid, SNAPSHOT))
        store.put_result(t, body["request_id"], body["iv"], body["ct"])
        return True

    monkeypatch.setattr(bridge, "_post_to_connection", fake_post)
    with TestClient(bridge.build_app(), base_url="http://localhost:8080") as client:
        yield {"client": client, "table": t, "sent": sent, "snapshots": snapshots, "answers": answers,
               "public": aws["public"]}


def register(client, device):
    r = client.post("/device/register", json={"invite": INVITE, "public_key": device.jwk})
    assert r.status_code == 201, r.text
    assert r.json()["mcp_url"] == bridge.RESOURCE
    device.uid = r.json()["uid"]
    return device


def signed(device, purpose, ts=None):
    ts = str(int(time.time())) if ts is None else ts
    return {"uid": device.uid, "ts": ts, "sig": device.sign(f"kw-{purpose}:{device.uid}:{ts}")}


def pair_code(client, device):
    r = client.post("/device/pair", json=signed(device, "pair"))
    assert r.status_code == 200, r.text
    return r.json()["code"]


def authorize(client, verifier="v" * 43, name="Claude"):
    reg = client.post("/oauth/register", json={"client_name": name, "redirect_uris": [REDIRECT]}).json()
    page = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s1",
        "code_challenge": crypto.pkce_s256(verifier), "code_challenge_method": "S256"})
    assert page.status_code == 200 and "읽기 전용" in page.text and "<b>claude.ai</b>" in page.text
    nonce = json.loads(re.search(r"const nonce = (\"[^\"]+\");", page.text).group(1))
    return reg, nonce


def oauth(client, device, name="Claude"):
    verifier = crypto.random_token(32)
    reg, nonce = authorize(client, verifier, name)
    code = pair_code(client, device).lower()  # case and the dash don't matter
    approved = client.post("/oauth/approve", json={"nonce": nonce, "code": code}).json()
    q = parse_qs(urlparse(approved["redirect_to"]).query)
    assert q["state"] == ["s1"]
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                              "code_verifier": verifier, "client_id": reg["client_id"],
                                              "redirect_uri": REDIRECT}).json()
    return reg, tokens


def refresh(client, reg, token):
    return client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token,
                                             "client_id": reg["client_id"]})


def grants(client, device):
    r = client.post("/device/grants", json=signed(device, "grants"))
    assert r.status_code == 200, r.text
    return r.json()["grants"]


def revoke(client, device, gid, signed_gid=None):
    return client.post("/device/revoke", json={**signed(device, f"revoke:{signed_gid or gid}"), "grant_id": gid})


def mcp_call(client, token, method, params_=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params_} if params_ is not None else {})}
    return client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}",
                                                  "Accept": "application/json, text/event-stream",
                                                  "MCP-Protocol-Version": "2025-06-18"})


def tool(client, token, name, **arguments):
    return mcp_call(client, token, "tools/call", {"name": name, "arguments": arguments}).json()["result"]


def connect(uid, ts, sig, v=None, conn="abc"):
    q = {"uid": uid, "ts": ts, "sig": sig, **({"v": v} if v is not None else {})}
    return ws.handler({"requestContext": {"routeKey": "$connect", "connectionId": conn},
                       "queryStringParameters": q}, None)["statusCode"]


def test_metadata_and_unauthenticated_mcp(env):
    c = env["client"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["issuer"] == bridge.ISSUER
    assert meta["authorization_response_iss_parameter_supported"] is True
    prm = c.get("/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200 and prm.json()["resource"] == bridge.RESOURCE
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    assert c.get("/mcp").status_code == 405


def test_full_flow_tools_come_from_the_agent(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1")
    _, tokens = oauth(c, device)
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]

    listed = mcp_call(c, tokens["access_token"], "tools/list").json()["result"]["tools"]
    assert {"this_week", "today_briefing", "my_notices", "my_assignments", "latest_notices",
            "list_notice_categories"} <= {t["name"] for t in listed}

    rows = tool(c, tokens["access_token"], "my_assignments")["structuredContent"]["result"]
    assert rows[0]["title"] == "HW3" and rows[0]["course"] == "데이터마이닝"
    paired = env["sent"][0][1]
    assert paired["type"] == "paired" and paired["redirect_host"] == "claude.ai" and paired["app"] == "Claude"
    assert paired["client_name"] == "Claude" and paired["grant_id"] == grants(c, device)[0]["id"]
    conn, message = env["sent"][1]
    assert conn == "conn-1" and message["type"] == "snapshot" and message["result_url"].endswith("/device/result")
    # nothing personal left in the table after the call: answered slots keep only a hash
    slots = [i for i in env["table"].scan()["Items"] if i["PK"].startswith("RESULT#")]
    assert slots and all("ct" not in i and "iv" not in i for i in slots)
    inflight = env["table"].get_item(Key={"PK": f"INFLIGHT#{device.uid}", "SK": "META"})["Item"]
    assert int(inflight["n"]) == 0  # the slot was given back


def test_refresh_rotates_with_a_grace_window(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg, tokens = oauth(c, device)
    new = refresh(c, reg, tokens["refresh_token"]).json()
    assert new["access_token"] and new["refresh_token"] != tokens["refresh_token"]
    again = refresh(c, reg, tokens["refresh_token"])  # a retry after a lost response, within 60 s
    assert again.status_code == 200 and again.json()["refresh_token"] not in (new["refresh_token"], tokens["refresh_token"])
    assert refresh(c, {"client_id": "c_" + "A" * 22}, new["refresh_token"]).json()["error"] == "invalid_client"
    assert mcp_call(c, new["access_token"], "tools/list").status_code == 200


def test_refresh_reuse_after_the_grace_revokes_the_whole_family(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg, tokens = oauth(c, device)
    new = refresh(c, reg, tokens["refresh_token"]).json()
    gid = tokens["refresh_token"].split(".")[0]
    old_key = {"PK": f"GRANT#{gid}", "SK": f"RT#{crypto.sha256_hex(tokens['refresh_token'])}"}
    env["table"].update_item(Key=old_key, UpdateExpression="SET rotated_at = :t",
                             ExpressionAttributeValues={":t": int(time.time()) - bridge.REFRESH_GRACE_S - 5})
    assert refresh(c, reg, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    # every token of that grant is dead now, including the newest pair
    assert refresh(c, reg, new["refresh_token"]).json()["error"] == "invalid_grant"
    assert mcp_call(c, new["access_token"], "tools/list").status_code == 401
    assert grants(c, device) == []
    assert not [i for i in env["table"].scan()["Items"] if i["PK"] == f"GRANT#{gid}"]


def test_grants_are_listed_and_revoked_one_at_a_time(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg_a, tok_a = oauth(c, device, "Claude")
    reg_b, tok_b = oauth(c, device, "Evil\n[INFO] fake log line\u202e")
    listed = {g["client"]: g for g in grants(c, device)}
    assert set(listed) == {"Evil [INFO] fake log line", "Claude"}  # the self-chosen name, cleaned
    first = listed["Claude"]
    assert set(first) == {"id", "client", "redirect_host", "created", "last_used", "expires"}
    assert first["redirect_host"] == "claude.ai" and first["last_used"]
    created, expires = (datetime.fromisoformat(first[k]) for k in ("created", "expires"))
    assert (expires - created).days == 120 and created.utcoffset().total_seconds() == 9 * 3600
    evil = listed["Evil [INFO] fake log line"]["id"]

    assert revoke(c, device, evil, signed_gid=first["id"]).status_code == 403  # the signature names the grant
    assert revoke(c, device, evil).json() == {"ok": True}
    assert mcp_call(c, tok_b["access_token"], "tools/list").status_code == 401  # at once, not after an hour
    assert refresh(c, reg_b, tok_b["refresh_token"]).json()["error"] == "invalid_grant"
    assert mcp_call(c, tok_a["access_token"], "tools/list").status_code == 200  # the other app keeps working
    assert refresh(c, reg_a, tok_a["refresh_token"]).status_code == 200
    assert [g["client"] for g in grants(c, device)] == ["Claude"]
    assert revoke(c, device, evil).status_code == 404
    assert c.post("/device/revoke", json={**signed(device, "revoke:x"), "grant_id": "x"}).status_code == 400


def test_pair_needs_device_signature_and_unlink_deletes_grants_and_tokens(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    forged = signed(device, "pair") | {"sig": FakeDevice().sign("kw-pair:x:0")}
    r = c.post("/device/pair", json=forged)
    assert r.status_code == 403 and r.json()["error"] == "bad_signature"
    reg, tokens = oauth(c, device)
    refresh(c, reg, tokens["refresh_token"])
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 200
    assert c.post("/device/unlink", json=signed(device, "pair")).status_code == 403  # wrong purpose
    assert c.post("/device/unlink", json=signed(device, "unlink")).status_code == 200
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 401
    left = [i["PK"] for i in env["table"].scan()["Items"] if i["PK"].startswith(("DEV#", "GRANT#"))]
    assert left == []  # device, grants and every refresh token of them
    r = c.post("/device/pair", json=signed(device, "pair"))
    assert r.status_code == 403 and r.json()["error"] == "unknown_device"


def test_device_errors_tell_a_wrong_clock_from_a_removed_device(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    r = c.post("/device/pair", json=signed(device, "pair", ts=str(int(time.time()) - 9 * 3600)))
    assert r.status_code == 401 and r.json()["error"] == "stale_timestamp"
    assert abs(r.json()["server_time"] - time.time()) < 5
    ghost = FakeDevice()
    ghost.uid = "u_" + "A" * 22
    assert c.post("/device/pair", json=signed(ghost, "pair")).json()["error"] == "unknown_device"
    for bad in ({"uid": "x", "ts": "1", "sig": "y"}, {"uid": device.uid, "ts": 5, "sig": "y"}, [1], "nope"):
        assert c.post("/device/pair", json=bad).status_code == 400


def test_device_signatures_are_single_use_in_every_spelling(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    ts = str(int(time.time()))
    message = f"kw-pair:{device.uid}:{ts}"
    while True:  # a signature whose text has '-' or '_', so the +/ spelling differs
        r, s = device.sign_rs(message)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        sig = b64u(raw)
        if "-" in sig or "_" in sig:
            break
    body = {"uid": device.uid, "ts": ts}
    assert c.post("/device/pair", json={**body, "sig": sig}).status_code == 200
    assert c.post("/device/pair", json={**body, "sig": sig}).json()["error"] == "replayed"
    # The same bytes spelled differently: padding, the +/ alphabet, a stray space, and the last
    # character's 4 unused bits flipped. All used to verify and pass the replay guard.
    for variant in (sig + "==", sig.replace("-", "+").replace("_", "/"), " " + sig, sig[:-1] + _flip_low_bit(sig[-1])):
        out = c.post("/device/pair", json={**body, "sig": variant})
        assert out.status_code == 403 and out.json()["error"] == "bad_signature", variant
    twin = b64u(r.to_bytes(32, "big") + (P256_N - s).to_bytes(32, "big"))  # (r, n - s) verifies too
    assert crypto.verify_device_signature(device.jwk, message, twin)
    assert c.post("/device/pair", json={**body, "sig": twin}).json()["error"] == "replayed"


_B64U = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _flip_low_bit(ch: str) -> str:
    return _B64U[_B64U.index(ch) ^ 1]


def test_strict_base64_accepts_only_the_canonical_spelling():
    raw = os.urandom(64)
    canonical = b64u(raw)
    assert crypto.b64u_decode_strict(canonical) == raw
    for other in (canonical + "==", "+" + canonical[1:], "/" + canonical[1:], canonical[:-1] + _flip_low_bit(canonical[-1]),
                  canonical + "\n", canonical[:-1], None, 5):
        assert crypto.b64u_decode_strict(other) is None, other
    # the last of 86 characters carries 4 unused bits: 16 spellings decode alike, one is canonical
    same = [canonical[:-1] + ch for ch in _B64U if base64.urlsafe_b64decode(canonical[:-1] + ch + "==") == raw]
    assert len(same) == 16 and [s for s in same if crypto.b64u_decode_strict(s)] == [canonical]
    assert not crypto.verify_device_signature({"kty": "EC"}, "m", "*" + canonical[1:])
    with pytest.raises(ValueError):
        crypto.signature_replay_key("x")


def test_ws_connect_requires_fresh_device_signature(env):
    device = FakeDevice()
    register(env["client"], device)
    ts = str(int(time.time()))
    assert connect(device.uid, ts, device.sign(f"kw-connect:{device.uid}:{ts}:2"), "2") == 200
    assert store.latest_connection(env["table"], device.uid) == "abc"
    assert int(store.get_device(env["table"], device.uid)["last_seen"]) >= int(ts)
    old = str(int(time.time()) - 3600)
    assert connect(device.uid, old, device.sign(f"kw-connect:{device.uid}:{old}:2"), "2") == 401
    assert connect(device.uid, ts, FakeDevice().sign(f"kw-connect:{device.uid}:{ts}:2"), "2") == 403
    assert connect(device.uid, ts, "*" * 86, "2") == 403  # undecodable: refused, not a Lambda error
    assert connect("u_" + "x" * 500, ts, "x") == 400
    ws.handler({"requestContext": {"routeKey": "$disconnect", "connectionId": "abc"}}, None)
    assert store.latest_connection(env["table"], device.uid) is None


def test_ws_connect_signature_covers_the_version(env):
    device = FakeDevice()
    register(env["client"], device)
    ts = str(int(time.time()))
    signed_v2 = device.sign(f"kw-connect:{device.uid}:{ts}:2")
    assert connect(device.uid, ts, signed_v2, "3", conn="c3") == 403  # v changed after signing
    assert connect(device.uid, ts, signed_v2, "2", conn="c2") == 200
    assert store.latest_connection_info(env["table"], device.uid) == ("c2", 2)
    assert connect(device.uid, ts, signed_v2, "2", conn="c9") == 409  # the same URL again
    # agents before 1.0 sign without v; accepted until the TODO date in ws.py
    ts2 = str(int(time.time()) + 1)
    assert connect(device.uid, ts2, device.sign(f"kw-connect:{device.uid}:{ts2}"), "2", conn="old") == 200
    assert store.latest_connection_info(env["table"], device.uid)[1] == 2


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


def test_malformed_input_gets_400_not_500(env):
    c = env["client"]
    assert c.post("/oauth/register", json=[1, 2]).status_code == 400
    assert c.post("/oauth/register", content=b"{" * 20_000, headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/oauth/approve", json=[1]).status_code == 400
    assert c.post("/device/register", content=b"not json").status_code == 400
    assert c.post("/device/register", json={"invite": INVITE, "public_key": "x"}).status_code == 400
    jwk = FakeDevice().jwk
    assert c.post("/device/register", json={"invite": INVITE, "public_key": {**jwk, "x": jwk["x"] + "=="}}).status_code == 400
    store.open_result(env["table"], "r1")
    assert c.post("/device/result", json={"request_id": "r1", "iv": "a" * 500_000, "ct": "bb"}).status_code == 400
    assert c.post("/device/result", json=["r1"]).status_code == 400
    assert c.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "nope"}).status_code == 401
    assert c.post("/oauth/token", data={"grant_type": "password"}).json()["error"] == "unsupported_grant_type"
    # small enough, but nested too deep for json (RecursionError), on every JSON route
    for path in ("/oauth/register", "/oauth/approve", "/device/register", "/device/result", "/device/pair"):
        deep = c.post(path, content=b"[" * 16_000, headers={"Content-Type": "application/json"})
        assert deep.status_code in (400, 403), path
    # a bearer token with non-ASCII characters (compare_digest refuses such str)
    bad_bearer = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"Authorization": "Bearer x.y.\xe9".encode("latin-1")})
    assert bad_bearer.status_code == 401
    # a redirect URI urlparse can't read
    assert c.post("/oauth/register", json={"redirect_uris": ["http://[::1"]}).status_code == 400


def test_authorize_checks_pkce_and_resource_and_reports_to_the_client(env):
    c = env["client"]
    reg = c.post("/oauth/register", json={"client_name": "x", "redirect_uris": [REDIRECT]}).json()
    base = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s",
            "code_challenge": crypto.pkce_s256("v" * 43), "code_challenge_method": "S256"}
    for change, error in (({"code_challenge": "한"}, "invalid_request"), ({"code_challenge": "a" * 42}, "invalid_request"),
                          ({"resource": "https://evil.example/mcp"}, "invalid_target")):
        r = c.get("/oauth/authorize", params={**base, **change}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"] == [error] and q["state"] == ["s"] and q["iss"] == [bridge.ISSUER]
    assert c.get("/oauth/authorize", params={**base, "resource": bridge.RESOURCE + "/"}).status_code == 200
    gone = c.get("/oauth/authorize", params={**base, "client_id": "c_" + "Z" * 22})
    assert gone.status_code == 400 and "지우고 다시 추가" in gone.text


def test_approve_page_resists_framing_and_cross_site_posts(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg, nonce = authorize(c)
    page = c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": crypto.pkce_s256("v" * 43), "code_challenge_method": "S256"})
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'none'" in csp and page.headers["x-frame-options"] == "DENY"
    script_nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{script_nonce}">' in page.text
    assert 'id="pw"' not in page.text and "비밀번호를 입력" not in page.text  # no 간편 연결 form any more
    code = pair_code(c, device)
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Content-Type": "text/plain"}):
        r = c.post("/oauth/approve", content=json.dumps({"nonce": nonce, "code": code}),
                   headers={"Content-Type": "application/json", **headers})
        assert r.status_code == 403
    r = c.post("/oauth/approve", json={"nonce": nonce, "mode": "password", "invite": INVITE,
                                       "login_id": "2022000000", "password": "x"})
    assert r.status_code == 403  # the password mode is gone: no code, no grant
    ok = c.post("/oauth/approve", json={"nonce": nonce, "code": code},
                headers={"Origin": bridge._ORIGIN, "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200 and "redirect_to" in ok.json()


def test_client_registration_expires_unless_used_and_slides_when_used(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg, tokens = oauth(c, device)
    unused = c.post("/oauth/register", json={"client_name": "x", "redirect_uris": [REDIRECT]}).json()
    ttl = lambda cid: int(env["table"].get_item(Key={"PK": f"CLIENT#{cid}", "SK": "META"})["Item"]["ttl"]) - time.time()
    assert abs(ttl(unused["client_id"]) - store.CLIENT_PENDING_TTL) < 60
    assert abs(ttl(reg["client_id"]) - store.CLIENT_TTL) < 60
    env["table"].update_item(Key={"PK": f"CLIENT#{reg['client_id']}", "SK": "META"}, UpdateExpression="SET #t = :t",
                             ExpressionAttributeNames={"#t": "ttl"}, ExpressionAttributeValues={":t": int(time.time()) - 1})
    assert refresh(c, reg, tokens["refresh_token"]).json()["error"] == "invalid_client"  # the client registers again


def test_invite_withdrawal_stops_devices_it_let_in(env, aws):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg, tokens = oauth(c, device)
    aws["ssm"].put_parameter(Name="/kw/mcp/tokens", Type="String", Value="[]", Overwrite=True)
    params.clear_cache()
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 401
    assert refresh(c, reg, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert c.post("/device/pair", json=signed(device, "pair")).json()["error"] == "invite_revoked"
    assert c.post("/device/unlink", json=signed(device, "unlink")).status_code == 200  # always allowed


def test_legacy_refresh_token_moves_into_a_grant(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    reg = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    legacy = crypto.random_token(32)
    env["table"].put_item(Item={"PK": f"REFRESH#{crypto.sha256_hex(legacy)}", "SK": "META", "uid": device.uid,
                                "client_id": reg["client_id"], "ttl": int(time.time()) + 3600})
    new = refresh(c, reg, legacy).json()
    assert new["refresh_token"].startswith("g_") and mcp_call(c, new["access_token"], "tools/list").status_code == 200
    assert [g["client"] for g in grants(c, device)] == ["Claude"]
    assert refresh(c, reg, legacy).json()["error"] == "invalid_grant"  # used up
    # access tokens from before grants keep working until they run out, unless the PC is gone
    now = int(time.time())
    old_access = crypto.sign_jwt(bridge._jwt_secret(), {"iss": bridge.ISSUER, "sub": device.uid, "aud": bridge.RESOURCE,
                                                         "client_id": reg["client_id"], "scope": bridge.SCOPE,
                                                         "iat": now, "exp": now + 600})
    assert mcp_call(c, old_access, "tools/list").status_code == 200
    p_access = crypto.sign_jwt(bridge._jwt_secret(), {"iss": bridge.ISSUER, "sub": "p_abc", "aud": bridge.RESOURCE,
                                                       "client_id": reg["client_id"], "scope": bridge.SCOPE,
                                                       "iat": now, "exp": now + 600})
    assert mcp_call(c, p_access, "tools/list").status_code == 401  # 간편 연결 tokens are refused


def test_tool_error_when_agent_not_connected(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    _, tokens = oauth(c, device)
    out = tool(c, tokens["access_token"], "my_courses")
    assert out["isError"] and "에이전트가 연결돼 있지 않습니다" in out["content"][0]["text"]


def test_inflight_and_per_minute_limits(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    key = {"PK": f"INFLIGHT#{device.uid}", "SK": "META"}
    env["table"].put_item(Item={**key, "n": bridge.MAX_INFLIGHT, "ttl": int(time.time()) + 30})
    busy = tool(c, tokens["access_token"], "my_grades")
    assert busy["isError"] and "3개" in busy["content"][0]["text"]
    env["table"].put_item(Item={**key, "n": bridge.MAX_INFLIGHT, "ttl": int(time.time()) - 1})  # left by a crash
    assert tool(c, tokens["access_token"], "my_grades")["structuredContent"] == {"op": "grades"}
    assert int(env["table"].get_item(Key=key)["Item"]["n"]) == 0
    minute = int(time.time()) // 60
    for m in (minute, minute + 1):
        env["table"].put_item(Item={"PK": f"RATE#{device.uid}#{m}", "SK": "META", "n": bridge.MAX_CALLS_PER_MINUTE,
                                    "ttl": int(time.time()) + 120})
    limited = tool(c, tokens["access_token"], "my_grades")
    assert limited["isError"] and "1분" in limited["content"][0]["text"]


def test_installer_pages_are_mounted_on_the_bridge(env, tmp_path, monkeypatch):
    # The scripts, /setup, /privacy and /dist themselves are tested in test_install.py.
    from kwcampus.bridge import install

    c = env["client"]
    monkeypatch.setattr(install, "STATIC_DIR", tmp_path)
    assert c.get("/install.sh").status_code == 503  # no signed release shipped
    assert "kw-mcp code" in c.get("/setup").text and "보호책임자" in c.get("/privacy").text
    assert c.get("/dist/kw-campus.mcpb").status_code == 404


def test_public_json_api_is_served_with_the_invite_token(env):
    c = env["client"]
    assert c.get(f"/c/{INVITE}/api/notices").status_code == 200
    assert c.get("/c/wrong-token/api/notices").status_code == 404
    assert c.post(f"/c/{INVITE}/mcp", json={}).status_code == 404


def test_results_only_fill_slots_the_bridge_opened_and_retries_are_idempotent(env):
    c = env["client"]
    assert c.post("/device/result", json={"request_id": "made-up", "iv": "aa", "ct": "bb"}).status_code == 409
    store.open_result(env["table"], "r1")
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "x" * 300_001}).status_code == 400
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "bb"}).status_code == 202
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "bb"}).status_code == 202  # retried
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "cc"}).status_code == 409
    assert store.take_result(env["table"], "r1")["ct"] == "bb"
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "bb"}).status_code == 202  # after reading
    assert c.post("/device/result", json={"request_id": "r1", "iv": "aa", "ct": "cc"}).status_code == 409
    assert store.take_result(env["table"], "r1") is None
    store.open_result(env["table"], "r2")
    assert store.take_result(env["table"], "r2", close=True) is None  # the bridge gave up waiting
    assert c.post("/device/result", json={"request_id": "r2", "iv": "aa", "ct": "late"}).status_code == 409


def test_a_result_landing_after_the_last_poll_is_still_read(env, monkeypatch):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    monkeypatch.setattr(bridge, "SNAPSHOT_WAIT_S", 0.01)
    real_take = store.take_result

    def late(t, request_id, *, close=False):  # nothing until the closing look
        return real_take(t, request_id, close=close) if close else None

    monkeypatch.setattr(store, "take_result", late)
    assert tool(c, tokens["access_token"], "my_grades")["structuredContent"] == {"op": "grades"}


def test_single_item_reads_need_an_updated_agent(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "old-agent")  # version 1: snapshot only
    _, tokens = oauth(c, device)
    args = {"course": "데이터", "title": "HW3"}
    old = tool(c, tokens["access_token"], "my_assignment_detail", **args)
    assert old["isError"] and "업데이트" in old["content"][0]["text"]
    store.add_connection(env["table"], device.uid, "new-agent", 2)
    new = tool(c, tokens["access_token"], "my_assignment_detail", **args)
    assert new["structuredContent"] == {"op": "assignment_detail", "course": "데이터", "title": "HW3"}
    sent = [m for conn, m in env["sent"] if m["type"] == "call"]
    assert sent[-1]["op"] == "assignment_detail" and sent[-1]["args"] == {"course": "데이터", "title": "HW3"}


def test_ws_connect_records_agent_version(env):
    device = FakeDevice()
    register(env["client"], device)
    ts = str(int(time.time()))
    assert connect(device.uid, ts, device.sign(f"kw-connect:{device.uid}:{ts}"), "2", conn="c2") == 200
    assert store.latest_connection_info(env["table"], device.uid) == ("c2", 2)


def test_grades_and_attachments_go_to_the_agent(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    assert tool(c, tokens["access_token"], "my_grades")["structuredContent"] == {"op": "grades"}
    att = tool(c, tokens["access_token"], "my_attachment", course="데이터", title="10주차 자료", part=2)
    assert att["structuredContent"] == {"op": "read_attachment", "course": "데이터", "title": "10주차 자료",
                                        "file_name": "", "part": "2", "source": "post"}


def test_attendance_exams_and_class_changes_go_to_the_agent(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    assert tool(c, tokens["access_token"], "my_attendance")["structuredContent"]["result"] == [{"op": "attendance", "course": ""}]
    assert tool(c, tokens["access_token"], "my_exams", course="데이터")["structuredContent"]["result"] == [
        {"op": "exam_info", "course": "데이터"}]
    assert tool(c, tokens["access_token"], "my_class_changes", days=7)["structuredContent"] == {"op": "class_changes", "days": "7"}


def test_two_students_never_see_each_other(env):
    c = env["client"]
    alice, bob = register(c, FakeDevice()), register(c, FakeDevice())
    store.add_connection(env["table"], alice.uid, "conn-alice")
    store.add_connection(env["table"], bob.uid, "conn-bob", 2)
    env["snapshots"][bob.uid] = {**SNAPSHOT, "courses": [{**SNAPSHOT["courses"][0], "subjNm": "운영체제"}],
                                 "course_posts": []}
    _, tok_a = oauth(c, alice)
    _, tok_b = oauth(c, bob)
    assert tool(c, tok_a["access_token"], "my_courses")["structuredContent"]["result"][0]["course"] == "데이터마이닝"
    assert tool(c, tok_b["access_token"], "my_courses")["structuredContent"]["result"][0]["course"] == "운영체제"
    assert set(bridge._SNAP) == {alice.uid, bob.uid}
    assert bridge._SNAP[bob.uid][1]["courses"][0]["subjNm"] == "운영체제"
    tool(c, tok_b["access_token"], "my_grades")
    tool(c, tok_a["access_token"], "my_grades")  # alice's agent is version 1
    by_conn = {(conn, m["type"]) for conn, m in env["sent"] if m["type"] in ("snapshot", "call")}
    assert by_conn == {("conn-alice", "snapshot"), ("conn-bob", "snapshot"), ("conn-bob", "call")}
    a_grants, b_grants = grants(c, alice), grants(c, bob)
    assert revoke(c, alice, b_grants[0]["id"]).status_code == 404  # one PC can't end another's grant
    assert [g["id"] for g in grants(c, bob)] == [b_grants[0]["id"]] and a_grants


def test_every_op_the_bridge_sends_is_one_the_agent_knows():
    from kw_mcp import agent
    from kw_mcp.service import SNAPSHOT_FIELDS

    tree = ast.parse(inspect.getsource(bridge))
    calls = {node.args[0].value: {k.arg for k in node.keywords} for node in ast.walk(tree)
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "agent_call"}
    assert len(calls) == 17
    for op, args in calls.items():
        assert op in agent.CALLS, op
        # The agent also takes the optional ids its lists hand out (agent.OPTIONAL_ARGS); every
        # other argument it knows, the bridge sends.
        assert args <= set(agent.CALLS[op]) and set(agent.CALLS[op]) - args <= agent.OPTIONAL_ARGS, op
    assert set(agent.CALLS) == set(calls) | {"deadlines"}  # every agent read has a tool
    assert agent.CALLS["deadlines"] == ("days",)
    shapes_source = inspect.getsource(shapes)
    for section, fields in SNAPSHOT_FIELDS.items():
        assert section in shapes_source or section in ("courses",), section
        assert all(f'"{f}"' in shapes_source or f in ("subjLabel", "evltnSe", "isonoff") for f in fields), section


def test_course_notes_and_class_change_flags_match_kw_mcp():
    from kw_mcp import service as local

    snap = {**SNAPSHOT, "courses": [{**SNAPSHOT["courses"][0], "lctrumSchdulInfo": "토 18교시/미지정"}]}
    assert shapes.courses(snap)[0]["note"] == local.schedule_note("토 18교시/미지정")
    assert bridge.CLASS_CHANGE_WORDS is local.CLASS_CHANGE_WORDS


def test_shapes_follow_kw_mcp_rules():
    from kw_mcp import service as local

    for name, query in (("데이터마이닝", "데이터 마이닝"), ("데이터 마이닝", "데이터마이닝"), ("Data Mining", "data mining"), ("x", "y")):
        assert shapes._matches(name, query) == local._matches(name, query)
    assert shapes.assignments(SNAPSHOT, " 데이터 마이닝")[0]["title"] == "HW3"
    with pytest.raises(ToolError, match="이 학기 과목: 데이터마이닝"):
        shapes.assignments(SNAPSHOT, "운영체제")
    with pytest.raises(ToolError):
        shapes.course_posts(SNAPSHOT, "운영체제")
    assert shapes.course_posts({**SNAPSHOT, "course_posts": []}, "데이터") == []
    posts = shapes.course_posts({**SNAPSHOT, "course_posts": [{**SNAPSHOT["course_posts"][0], "title": "기말 &amp; 과제"}]})
    assert posts[0]["title"] == "기말 & 과제"
    snap = {**SNAPSHOT, "sections": {"U2026A": {**SNAPSHOT["sections"]["U2026A"], "tasks": [
        {**SNAPSHOT["sections"]["U2026A"]["tasks"][0], "expiredate": "2026-11-07 23:59"}]}}}
    at_2359_30 = datetime(2026, 11, 7, 23, 59, 30, tzinfo=KST)
    assert [i["title"] for i in shapes.deadlines(snap, at_2359_30, 1)["items"]] == ["HW3"]  # open through 23:59:59


def test_briefing_hides_timetable_outside_the_term(env, monkeypatch):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1")
    _, tokens = oauth(c, device)
    monkeypatch.setattr(bridge, "_now", lambda: datetime(2027, 1, 14, 9, 0, tzinfo=KST))  # winter break, Thursday
    result = tool(c, tokens["access_token"], "today_briefing")["structuredContent"]
    assert result["classes"] == [] and "2026,2" in result["classes_note"]
    # an old agent's deadlines come from the snapshot, marked as missing what it can't know
    assert result["deadlines_incomplete"]["missing"] == ["online_test", "survey", "discussion"]


def test_public_notice_api_honors_since_without_a_query(aws):
    from datetime import date
    from unittest import mock

    from kwcampus.public_http import _resolve_day

    rows = [{"title": "옛 공지", "posted_date": "2026-09-01", "category_id": None, "pinned": False},
            {"title": "새 공지", "posted_date": "2026-11-04", "category_id": None, "pinned": False}]
    with mock.patch.object(public_data, "notices", return_value=rows):
        assert [r["title"] for r in public_data.latest(None, None, 10, True, date(2026, 10, 29))] == ["새 공지"]
        assert len(public_data.latest(None, None, 10, True)) == 2
    assert _resolve_day(" ") == _resolve_day(None)


def test_graduation_goes_to_the_agent(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    assert tool(c, tokens["access_token"], "my_graduation")["structuredContent"] == {"op": "graduation_progress"}


def test_new_reads_go_to_the_agent(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    for name in ("my_online_tests", "my_surveys", "my_discussions", "my_course_evaluation", "my_course_overview"):
        tool(c, tokens["access_token"], name)
    digest = tool(c, tokens["access_token"], "my_academic_deadlines", days=30)["structuredContent"]
    assert digest["klas"] == {"op": "application_windows"} and isinstance(digest["calendar"], list)
    assert [m["op"] for _, m in env["sent"] if m["type"] == "call"] == [
        "online_tests", "surveys", "discussions", "course_evaluation", "course_overview", "application_windows"]


def test_deadlines_come_from_the_agent_and_fall_back_to_the_snapshot(env, monkeypatch):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    out = tool(c, tokens["access_token"], "my_deadlines", days=7)["structuredContent"]
    assert out == {"op": "deadlines", "days": "7"}  # the agent's own list
    assert [m["op"] for _, m in env["sent"] if m["type"] == "call"] == ["deadlines"]

    real = bridge._agent_request

    async def before_0_8(uid, payload, need_version=1):  # an agent that doesn't know "deadlines" yet
        if payload["type"] == "call":
            return {"error": "unsupported"}
        return await real(uid, payload, need_version)

    monkeypatch.setattr(bridge, "_agent_request", before_0_8)
    out = tool(c, tokens["access_token"], "my_deadlines", days=7)["structuredContent"]
    assert "items" in out and out["days"] == 7
    assert out["incomplete"]["missing"] == ["online_test", "survey", "discussion"]
    assert "업데이트" in out["incomplete"]["message"]
    week = tool(c, tokens["access_token"], "this_week", days=100000)["structuredContent"]
    assert week["days"] == 180 and week["incomplete"]["missing"] == ["online_test", "survey", "discussion"]


def test_academic_deadlines_still_show_the_calendar_for_an_older_agent(env, monkeypatch):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)

    async def before_0_8(uid, payload, need_version=1):
        return {"error": "unsupported"}

    monkeypatch.setattr(bridge, "_agent_request", before_0_8)
    digest = tool(c, tokens["access_token"], "my_academic_deadlines")["structuredContent"]
    assert "업데이트" in digest["klas"]["error"] and isinstance(digest["calendar"], list)


def test_parallel_briefing_requests_share_one_snapshot(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1")  # an agent from before calls: snapshot only
    _, tokens = oauth(c, device)
    tool(c, tokens["access_token"], "today_briefing")
    assert [m["type"] for _, m in env["sent"] if m["type"] in ("snapshot", "call")] == ["snapshot"]


def _briefing_agent(monkeypatch, *, snapshot=None, deadlines=None, snapshot_delay=0.0):
    """A version-2 agent whose two briefing answers are given (an exception is raised)."""
    async def fake(uid, payload, need_version=1):
        if payload["type"] == "snapshot":
            await asyncio.sleep(snapshot_delay)
            if isinstance(snapshot, Exception):
                raise snapshot
            return snapshot or SNAPSHOT
        if isinstance(deadlines, Exception):
            raise deadlines
        return deadlines

    monkeypatch.setattr(bridge, "_agent_request", fake)


def test_briefing_answers_with_what_arrived(env, monkeypatch):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "agent", 2)
    _, tokens = oauth(c, device)
    monkeypatch.setattr(bridge, "_now", lambda: datetime(2099, 11, 6, 9, 0, tzinfo=KST))  # Friday, 2099-11-07 is due
    monkeypatch.setattr(bridge, "term_in_session", lambda semester, now: True)

    # the agent's deadline list fails fast while the snapshot takes a while: the briefing waits for
    # the snapshot, falls back to it for deadlines, and leaves nothing running
    _briefing_agent(monkeypatch, deadlines=ToolError("PC 에이전트가 제때 응답하지 않았습니다."), snapshot_delay=0.3)
    out = tool(c, tokens["access_token"], "today_briefing")
    result = out["structuredContent"]
    assert not out.get("isError") and bridge._SNAP_PENDING == {}
    assert [i["title"] for i in result["due_today_or_tomorrow"]] == ["HW3"]
    assert result["deadlines_incomplete"]["missing"] == ["online_test", "survey", "discussion"]
    assert "제때 응답하지" in result["deadlines_incomplete"]["message"] and "incomplete" not in result

    # the snapshot fails, the deadline list arrives
    bridge._SNAP.clear()
    items = [{"kind": "online_test", "title": "중간", "due": "2099-11-07 10:00"},
             {"kind": "survey", "title": "날짜 없음", "due": "언젠가"}]  # an unparseable due is left out
    # the shape KlasService.deadlines() really answers with: no data_as_of, the time it read in "now"
    _briefing_agent(monkeypatch, snapshot=ToolError("KLAS에서 데이터를 읽지 못했습니다."),
                    deadlines={"result": {"semester": "2099,2", "now": "2099-11-06 08:00", "until": "2099-11-07 23:59",
                                          "days": 2, "items": items}})
    result = tool(c, tokens["access_token"], "today_briefing")["structuredContent"]
    assert result["incomplete"] == {"missing": ["classes", "new_course_posts"], "message": "KLAS에서 데이터를 읽지 못했습니다."}
    assert [i["title"] for i in result["due_today_or_tomorrow"]] == ["중간"] and result["classes"] == []
    assert result["data_as_of"] == "2099-11-06T08:00:00+09:00" and "cafeteria" in result

    # both fail (the PC is off): the public parts still answer
    _briefing_agent(monkeypatch, snapshot=ToolError("PC 에이전트가 연결돼 있지 않습니다."),
                    deadlines=ToolError("PC 에이전트가 연결돼 있지 않습니다."))
    result = tool(c, tokens["access_token"], "today_briefing")["structuredContent"]
    assert result["incomplete"]["missing"] == ["classes", "new_course_posts", "due_today_or_tomorrow"]
    assert "new_school_notices" in result and result["due_today_or_tomorrow"] == []

    # a bug is still a bug, not a partial answer
    _briefing_agent(monkeypatch, deadlines=RuntimeError("boom"))
    assert tool(c, tokens["access_token"], "today_briefing")["isError"]


def _notice(table, source, post_id, title, posted, category_id, pinned=False):
    table.put_item(Item={"PK": f"SRC#{source}", "SK": f"N#{post_id}", "post_id": str(post_id), "source": source,
                         "title": title, "category_id": category_id, "department": "", "posted_date": posted,
                         "updated_date": posted, "pinned": pinned, "first_seen": int(time.time()), "url": "u"})


def test_campus_alerts_use_the_main_board_and_filter_before_the_limit(env, monkeypatch):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1")
    _, tokens = oauth(c, device)
    monkeypatch.setattr(bridge, "_now", lambda: datetime(2026, 11, 6, 9, 0, tzinfo=KST))
    pub = env["public"]
    for i in range(40):  # newer department and job posts with alert words
        _notice(pub, "dept-ee", 900 + i, f"[휴강] 전자공학과 과목 {i} 점검", "2026-11-06", "dept-ee")
    _notice(pub, "kw-job", 1, "기상청 채용 안내", "2026-11-06", "job")
    _notice(pub, "kw-notice", 55001, "11/7 새빛관 정전 안내", "2026-11-04", "general")
    _notice(pub, "kw-notice", 55002, "2학기 수강 철회 안내", "2026-11-05", "academic")  # yesterday, under the 40 newer posts
    # these two would sort first (newest date, highest id) if they weren't left out
    _notice(pub, "kw-notice", 99998, "고정 공지", "2026-11-06", "academic", pinned=True)
    _notice(pub, "kw-faq", 99999, "휴학 FAQ", "2026-11-06", "faq")
    public_data.clear_cache()
    result = tool(c, tokens["access_token"], "today_briefing")["structuredContent"]
    assert [n["title"] for n in result["campus_alerts"]["notices"]] == ["11/7 새빛관 정전 안내"]
    # 학교 공지 is the main board, as in kw-mcp: department and job posts don't push it out
    assert [n["title"] for n in result["new_school_notices"]] == ["2학기 수강 철회 안내"]


def test_my_notices_clamps_days_and_takes_the_students_department(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1")
    _, tokens = oauth(c, device)
    out = tool(c, tokens["access_token"], "my_notices", days=10_000_000)["structuredContent"]
    assert out["days"] == 180 and "department" not in out  # no college is assumed for anyone
    assert tool(c, tokens["access_token"], "my_notices", days=-3)["structuredContent"]["days"] == 1
    assert tool(c, tokens["access_token"], "my_notices", days=0)["structuredContent"]["days"] == 7  # as kw-mcp
    # The department is the student's own, from the agent's 학적; a name the model passes is not used.
    dept = tool(c, tokens["access_token"], "my_notices", department="전자공학과")["structuredContent"]
    # a protocol-1 agent can't answer the department request: the rest still answers
    assert "department" not in dept and "kw-mcp update" in dept["department_note"] and dept["public_data"] == "ok"


def test_snapshot_cache_is_swept_on_every_request(env):
    bridge._SNAP["u_stale"] = (time.monotonic() - bridge.SNAPSHOT_CACHE_S - 1, {"courses": []})
    assert env["client"].get("/healthz").status_code == 200
    assert "u_stale" not in bridge._SNAP


def test_tool_errors_stay_out_of_the_logs():
    assert not logging.getLogger("mcp.server.mcpserver.server").isEnabledFor(logging.INFO)
    assert logging.getLogger("mcp").level == logging.WARNING

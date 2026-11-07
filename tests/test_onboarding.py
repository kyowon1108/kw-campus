"""The no-terminal onboarding: /connect leaves the PC's connection code in a kw_pair cookie, the authorize
page approves with it in one click, Discord-issued invites work everywhere a hand-made one does, and a PC
that is off gets a message a student can act on. Made-up codes and ids only; AWS is moto."""

from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, urlparse

import pytest
import test_bridge
from kw_mcp.agent import parse_invite
from test_bridge import (
    REDIRECT,
    FakeDevice,
    mcp_call,
    pair_code,
    register,
    signed,
    tool,
)

from kwcampus import invites
from kwcampus.bridge import app as bridge
from kwcampus.bridge import crypto, store

env = test_bridge.env
MEMBER = "123456789012345678"


def claim(c, code, **headers):
    return c.post("/connect/claim", json={"code": code}, headers=headers)


def open_authorize(c, verifier="v" * 43):
    reg = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [REDIRECT]}).json()
    page = c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s1",
        "code_challenge": crypto.pkce_s256(verifier), "code_challenge_method": "S256"})
    assert page.status_code == 200, page.text
    nonce = json.loads(re.search(r"const nonce = (\"[^\"]+\");", page.text).group(1))
    return reg, nonce, page.text


def set_cookie_header(response) -> str:
    found = [h for h in response.headers.get_list("set-cookie") if h.startswith(f"{bridge.PAIR_COOKIE}=")]
    assert len(found) == 1, response.headers.get_list("set-cookie")
    return found[0]


# --- /connect -----------------------------------------------------------------------------------------

def test_connect_page_reads_the_code_from_the_fragment_only(env):
    r = env["client"].get("/connect")
    page = r.text
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    nonce = re.search(r'<script nonce="([^"]+)">', page).group(1)
    csp = r.headers["content-security-policy"]
    assert f"script-src 'nonce-{nonce}'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert f"<code id=\"mcp-url\">{bridge.RESOURCE}</code>" in page
    assert 'href="https://claude.ai/settings/connectors"' in page and "Claude 커넥터 설정 열기" in page
    steps = re.search(r'<ol class="steps">(.*?)</ol>', page, re.DOTALL).group(1)
    assert steps.count("<li") == 3 and "사용자 지정 커넥터 추가" in steps and "이 PC와 연결하기" in steps
    assert "폰 앱에서도" in steps and 'id="code" class="big"' in page  # the code, large, for another device
    assert "코드가 만료됐어요" in page and "다시 말해 주세요" in page  # what the page says on a 404
    script = page.split(f'<script nonce="{nonce}">')[1]
    assert "location.hash" in script and "fetch('/connect/claim'" in script
    assert "kw-mcp" not in page and "<input" not in page


def test_claim_checks_the_shape_then_the_code_and_sets_a_narrow_cookie(env):
    c, device = env["client"], register(env["client"], FakeDevice())
    for bad in ("", "ABCD", "ABCD-EFG1", "ABCD-EFGHI", "ABCDEFGH" * 2, "IOU0-1LAB", None, 12345678, ["ABCD-EFGH"]):
        r = c.post("/connect/claim", json={"code": bad})
        assert r.status_code == 400 and r.json() == {"error": "bad_code"}, bad
    assert c.post("/connect/claim", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    unknown = claim(c, "ABCD-EFGH")
    assert unknown.status_code == 404 and unknown.json() == {"error": "expired"}
    assert not unknown.headers.get_list("set-cookie")

    code = pair_code(c, device)  # "XXXX-XXXX"
    r = claim(c, code.lower())  # any case, with or without the dash
    assert r.status_code == 200 and r.json()["ok"] is True
    cookie = set_cookie_header(r)
    attrs = {part.strip().split("=")[0].lower(): part.strip() for part in cookie.split(";")}
    assert cookie.startswith(f"kw_pair={code.replace('-', '')};")
    assert "httponly" in attrs and attrs["path"] == "Path=/oauth" and attrs["samesite"].lower() == "samesite=lax"
    assert 0 < int(attrs["max-age"].split("=")[1]) <= bridge.PAIR_COOKIE_MAX_S
    assert "secure" not in attrs and bridge.PAIR_COOKIE_SECURE is False  # BASE_URL is http://localhost here
    # looking doesn't use the code up
    assert store.peek_pair(env["table"], crypto.sha256_hex(code.replace("-", "")))["uid"] == device.uid
    assert claim(c, code.replace("-", "")).status_code == 200


def test_cookie_lives_no_longer_than_the_code_and_is_secure_off_localhost(env, monkeypatch):
    c, device = env["client"], register(env["client"], FakeDevice())
    code = pair_code(c, device)
    key = {"PK": f"PAIR#{crypto.sha256_hex(code.replace('-', ''))}", "SK": "META"}

    def expires_in(seconds):
        env["table"].update_item(Key=key, UpdateExpression="SET #t = :t", ExpressionAttributeNames={"#t": "ttl"},
                                 ExpressionAttributeValues={":t": int(time.time()) + seconds})

    expires_in(100)
    monkeypatch.setattr(bridge, "PAIR_COOKIE_SECURE", True)
    cookie = set_cookie_header(claim(c, code))
    age = int(re.search(r"Max-Age=(\d+)", cookie).group(1))
    assert 95 <= age <= 100 and "Secure" in cookie
    expires_in(-1)
    assert claim(c, code).status_code == 404  # expired: the page asks Claude Desktop again


def test_claim_refuses_other_sites(env):
    """Another site must not plant its own PC's code in a student's browser."""
    c, device = env["client"], register(env["client"], FakeDevice())
    code = pair_code(c, device)
    assert claim(c, code, Origin="https://evil.example").status_code == 403
    assert claim(c, code, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = c.post("/connect/claim", data={"code": code})  # a plain form post
    assert r.status_code == 403 and not r.headers.get_list("set-cookie")


# --- /oauth/authorize and /oauth/approve with the cookie ---------------------------------------------

def test_authorize_offers_one_click_only_with_a_live_cookie(env):
    c, device = env["client"], register(env["client"], FakeDevice())
    _, _, page = open_authorize(c)
    assert 'id="pc-approve"' not in page and 'id="pc"' in page and "XXXX-XXXX" in page
    assert "kw-mcp" not in page and "/setup" not in page
    c.cookies.set(bridge.PAIR_COOKIE, "not-a-code", domain="localhost.local", path="/oauth")
    assert 'id="pc-approve"' not in open_authorize(c)[2]
    c.cookies.clear()
    c.cookies.set(bridge.PAIR_COOKIE, "ABCDEFGH", domain="localhost.local", path="/oauth")  # shaped, not pending
    assert 'id="pc-approve"' not in open_authorize(c)[2]
    store.put_pair(env["table"], crypto.sha256_hex("ABCDEFGH"), device.uid)  # pending, but no network tag
    assert 'id="pc-approve"' not in open_authorize(c)[2]
    store.take_pair(env["table"], crypto.sha256_hex("ABCDEFGH"))
    mine = pair_code(c, device).replace("-", "")  # this network's own code, cookie set by hand: it is sent
    c.cookies.set(bridge.PAIR_COOKIE, mine, domain="localhost.local", path="/oauth")
    assert 'id="pc-approve"' in open_authorize(c)[2]
    c.cookies.clear()
    code = pair_code(c, device)
    assert claim(c, code).status_code == 200
    _, _, page = open_authorize(c)
    assert 'id="pc-approve"' in page and "이 PC와 연결하기" in page
    shown = re.search(r'<p class="muted">코드 ([A-Z0-9]{4}-[A-Z0-9]{4})</p>', page).group(1)
    assert shown == code  # the student can match it with what Claude Desktop showed
    assert 'id="pc"' in page  # a code from another device can still be typed


def test_one_click_approval_works_once_and_clears_the_cookie(env):
    c, device = env["client"], register(env["client"], FakeDevice())
    code = pair_code(c, device)
    assert claim(c, code).status_code == 200
    verifier = crypto.random_token(32)
    reg, nonce, _ = open_authorize(c, verifier)
    r = c.post("/oauth/approve", json={"nonce": nonce, "use_pair_cookie": True})
    assert r.status_code == 200, r.text
    cleared = set_cookie_header(r)
    assert cleared.startswith('kw_pair="";') and "Max-Age=0" in cleared and "Path=/oauth" in cleared
    assert bridge.PAIR_COOKIE not in c.cookies
    q = parse_qs(urlparse(r.json()["redirect_to"]).query)
    tokens = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                          "code_verifier": verifier, "client_id": reg["client_id"],
                                          "redirect_uri": REDIRECT}).json()
    assert mcp_call(c, tokens["access_token"], "tools/list").status_code == 200

    # The same code again, even with the cookie put back by hand: used up, and it counts as a miss.
    _, nonce, page = open_authorize(c)
    assert 'id="pc-approve"' not in page
    c.cookies.set(bridge.PAIR_COOKIE, code.replace("-", ""), domain="localhost.local", path="/oauth")
    again = c.post("/oauth/approve", json={"nonce": nonce, "use_pair_cookie": True})
    said = again.json()["error_description"]
    assert again.status_code == 403 and "만료됐어요" in said
    assert "“웹이랑 폰에서도 쓰게 연결해 줘”라고 다시 보내면 새 코드가 나와요" in said and "kw-mcp" not in said
    assert set_cookie_header(again).startswith('kw_pair="";')
    assert int(env["table"].get_item(Key={"PK": f"AUTHREQ#{nonce}", "SK": "META"})["Item"]["attempts"]) == 1


def test_one_click_approval_goes_through_the_same_checks(env):
    c, device = env["client"], register(env["client"], FakeDevice())
    assert claim(c, pair_code(c, device)).status_code == 200
    _, nonce, _ = open_authorize(c)
    cross = c.post("/oauth/approve", json={"nonce": nonce, "use_pair_cookie": True},
                   headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403 and bridge.PAIR_COOKIE in c.cookies  # nothing was taken
    # only an explicit use_pair_cookie reads the cookie; a missing code is a miss like any other
    plain = c.post("/oauth/approve", json={"nonce": nonce})
    assert plain.status_code == 403 and bridge.PAIR_COOKIE in c.cookies
    for _ in range(store.MAX_CODE_ATTEMPTS - 2):
        c.post("/oauth/approve", json={"nonce": nonce, "code": "ZZZZ-ZZZZ"})
    last = c.post("/oauth/approve", json={"nonce": nonce, "use_pair_cookie": True})
    assert last.status_code == 200, last.text  # the fifth try, and the right code


def test_authorize_request_lasts_thirty_minutes(env):
    _, nonce, _ = open_authorize(env["client"])
    row = env["table"].get_item(Key={"PK": f"AUTHREQ#{nonce}", "SK": "META"})["Item"]
    assert store.AUTHREQ_TTL == 1800 and abs(int(row["ttl"]) - time.time() - 1800) < 60


# --- Discord-issued invites ----------------------------------------------------------------------------

def test_a_discord_invite_registers_a_pc_and_opens_the_public_api_until_revoked(env):
    c, t = env["client"], env["table"]
    server, token = parse_invite(invites.issue(t, MEMBER, bridge.BASE_URL))
    assert server == bridge.BASE_URL
    device = FakeDevice()
    r = c.post("/device/register", json={"invite": token, "public_key": device.jwk})
    assert r.status_code == 201, r.text
    device.uid = r.json()["uid"]
    assert store.get_device(t, device.uid)["invite_hash"] == invites.digest(token)
    assert c.get(f"/c/{token}/api/notices").status_code == 200  # the same token is a public-data token
    store.add_connection(t, device.uid, "conn-1")
    assert claim(c, pair_code(c, device)).status_code == 200
    verifier = crypto.random_token(32)
    reg, nonce, _ = open_authorize(c, verifier)
    q = parse_qs(urlparse(c.post("/oauth/approve", json={"nonce": nonce, "use_pair_cookie": True})
                          .json()["redirect_to"]).query)
    access = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                          "code_verifier": verifier, "client_id": reg["client_id"],
                                          "redirect_uri": REDIRECT}).json()["access_token"]
    assert mcp_call(c, access, "tools/list").status_code == 200

    assert invites.revoke(t, invites.digest(token)) is True
    assert c.get(f"/c/{token}/api/notices").status_code == 404
    assert mcp_call(c, access, "tools/list").status_code == 401
    assert c.post("/device/pair", json=signed(device, "pair")).json()["error"] == "invite_revoked"
    again = c.post("/device/register", json={"invite": token, "public_key": FakeDevice().jwk})
    assert again.status_code == 403 and again.json()["error"] == "invalid_invite"


def test_a_discord_invite_keeps_the_per_invite_pc_cap(env):
    c, t = env["client"], env["table"]
    _, token = parse_invite(invites.issue(t, MEMBER, bridge.BASE_URL))
    for _ in range(bridge.MAX_DEVICES_PER_INVITE):
        assert c.post("/device/register", json={"invite": token, "public_key": FakeDevice().jwk}).status_code == 201
    refused = c.post("/device/register", json={"invite": token, "public_key": FakeDevice().jwk})
    assert refused.status_code == 409 and refused.json()["limit"] == bridge.MAX_DEVICES_PER_INVITE
    assert invites.check(token, t, ttl_s=0) == invites.digest(token)  # the PC count doesn't spoil the invite


def test_a_whole_invite_string_is_not_a_token(env):
    c, t = env["client"], env["table"]
    invite = invites.issue(t, MEMBER, bridge.BASE_URL)
    r = c.post("/device/register", json={"invite": invite, "public_key": FakeDevice().jwk})
    assert r.status_code == 403  # the extension sends the token inside, as before


# --- a PC that is off, asleep or gone ----------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["off", "dropped", "slow"])
def test_pc_off_messages_say_what_to_do_and_what_still_works(env, monkeypatch, answer):
    c, device = env["client"], register(env["client"], FakeDevice())
    if answer != "off":
        store.add_connection(env["table"], device.uid, "conn-1")
    if answer == "dropped":
        monkeypatch.setattr(bridge, "_post_to_connection", lambda conn, message: False)
    if answer == "slow":
        monkeypatch.setattr(bridge, "SNAPSHOT_WAIT_S", 0.2)
        monkeypatch.setattr(bridge, "_post_to_connection", lambda conn, message: True)  # never answers
    reg, nonce, _ = open_authorize(c)
    approved = c.post("/oauth/approve", json={"nonce": nonce, "code": pair_code(c, device)}).json()
    q = parse_qs(urlparse(approved["redirect_to"]).query)
    access = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                          "code_verifier": "v" * 43, "client_id": reg["client_id"],
                                          "redirect_uri": REDIRECT}).json()["access_token"]
    out = tool(c, access, "my_assignments")
    text = out["content"][0]["text"]
    assert out["isError"]
    assert "KLAS를 못 열었어요" in text or "KLAS 내용을 못 가져왔어요" in text  # what didn't work
    assert "잠자기" in text  # why
    assert "다시 물어봐 주세요" in text and "로그인" in text  # what to do
    assert "공지·학식·학사일정·학사 안내(school_guide)는 PC 없이도 볼 수 있어요" in text  # what still works
    assert "에이전트" not in text and "kw-mcp" not in text and "습니다" not in text


# --- the cookie only for a browser on the PC's own network ---------------------------------------------

def from_ip(ip: str) -> dict:
    """What the Lambda Web Adapter adds: API Gateway's requestContext (payload 2.0), sourceIp included."""
    return {"x-amzn-request-context": json.dumps({"http": {"method": "POST", "sourceIp": ip}})}


def pair_from(c, device, ip: str) -> str:
    r = c.post("/device/pair", json=signed(device, "pair"), headers=from_ip(ip))
    assert r.status_code == 200, r.text
    return r.json()["code"]


def test_a_code_from_another_network_sets_no_cookie(env):
    """Someone with a PC of their own sends a student /connect#<their code>: the student's browser is on
    another network, so no cookie, no one-click, and the page doesn't show that code."""
    c, t = env["client"], env["table"]
    device = register(c, FakeDevice())
    code = pair_from(c, device, "203.0.113.5")
    row = store.peek_pair(t, crypto.sha256_hex(code.replace("-", "")))
    assert re.fullmatch(r"[0-9a-f]{32}", row["net"]) and "203.0.113" not in json.dumps(row, default=str)

    other = claim(c, code, **from_ip("198.51.100.7"))
    assert other.status_code == 200 and other.json() == {"ok": True, "cookie": False}
    assert not other.headers.get_list("set-cookie") and bridge.PAIR_COOKIE not in c.cookies
    assert 'id="pc-approve"' not in open_authorize(c)[2]
    assert store.peek_pair(t, crypto.sha256_hex(code.replace("-", "")))  # still usable by typing it

    same = claim(c, code, **from_ip("203.0.113.5"))
    assert same.json()["cookie"] is True and set_cookie_header(same).startswith("kw_pair=")


def test_network_tag_reads_the_gateway_address_not_what_the_client_says(env):
    c = env["client"]
    device = register(c, FakeDevice())
    # IPv6: the same /64 counts as the same network (temporary addresses change within it)
    code = pair_from(c, device, "2001:db8:1:2::10")
    assert claim(c, code, **from_ip("2001:db8:1:2:abcd::99")).json()["cookie"] is True
    assert claim(c, code, **from_ip("2001:db8:1:3::10")).json()["cookie"] is False
    # without the adapter's header: the last X-Forwarded-For hop, the one the proxy added
    code = c.post("/device/pair", json=signed(device, "pair"),
                  headers={"X-Forwarded-For": "203.0.113.5"}).json()["code"]
    spoofed = claim(c, code, **{"X-Forwarded-For": "203.0.113.5, 198.51.100.7"})
    assert spoofed.json()["cookie"] is False
    assert claim(c, code, **{"X-Forwarded-For": "1.2.3.4, 203.0.113.5"}).json()["cookie"] is True
    # the adapter's context wins over a forwarded-for header
    assert claim(c, code, **{**from_ip("198.51.100.7"), "X-Forwarded-For": "203.0.113.5"}).json()["cookie"] is False


def test_connect_page_states(env):
    page = env["client"].get("/connect").text
    script = page.split('<script nonce="')[1]
    manual = re.search(r'<p id="manual" class="card accent" hidden>(.*?)</p>', page).group(1)
    assert manual == "자동 연결이 안 돼요. 커넥터를 추가할 때 Claude 데스크톱이 알려 준 코드를 넣어 주세요."
    assert "cookie === false" in script and "getElementById('code-card').hidden = true" in script
    assert script.index("cookie === false") < script.index("getElementById('code').textContent")
    later = re.search(r'<p id="later" class="card accent" hidden>(.*?)</p>', page).group(1)
    assert later == "잠시 뒤에 다시 열어 주세요." and "r.status === 404 ? 'expired' : 'later'" in script
    assert f"[{bridge.CODE_ALPHABET}]{{4}}" in script  # the same alphabet the server takes


# --- connection codes per PC ------------------------------------------------------------------------------

def test_a_pc_gets_at_most_ten_codes_an_hour(env):
    c, device = env["client"], register(env["client"], FakeDevice())
    for _ in range(bridge.MAX_PAIRS_PER_HOUR):
        pair_code(c, device)
    r = c.post("/device/pair", json=signed(device, "pair"))
    assert r.status_code == 429 and r.json()["error"] == "too_many_codes"
    assert r.json()["message"] == "연결 코드를 너무 자주 받았어요. 한 시간쯤 뒤에 다시 해 주세요."
    assert pair_code(c, register(c, FakeDevice()))  # per PC: another one is unaffected


# --- what an agent says reaches the student without links ----------------------------------------------

def test_agent_text_loses_links_and_length():
    def said(data):
        with pytest.raises(bridge.ToolError) as caught:
            bridge._raise_for(data)
        return str(caught.value)

    hostile = "로그인 오류. https://evil.example/login 에서 다시 로그인 www.evil.kr 또는 help@evil.com evil.co.kr/x"
    text = said({"error": "klas_login_required", "message": hostile})
    assert text.startswith(bridge._ERRORS["klas_login_required"]) and "로그인 오류." in text
    assert "evil" not in text and "http" not in text and "@" not in text
    assert "evil" not in said({"error": "not_found", "message": "javascript:alert(1) 그런 글이 없습니다 evil.com"})
    assert said({"error": "klas_error", "message": "점검 중\u202e입니다\n" + "가" * 500}).startswith("점검 중 입니다 ")
    assert len(said({"error": "klas_error", "message": "가" * 500})) == bridge.AGENT_TEXT_MAX
    assert said({"error": "not_found", "message": "HW3.pdf 첨부가 없습니다."}) == "HW3.pdf 첨부가 없습니다."



def test_a_cookie_from_another_network_gets_no_one_click_and_keeps_its_code(env):
    """A kw_pair cookie that didn't come from /connect/claim on this network (cookie tossing from a
    sibling host): no one-click on the page, refused at approval like a bad code, cookie cleared, and
    the code itself is left for typing."""
    c, t = env["client"], env["table"]
    device = register(c, FakeDevice())
    code = pair_from(c, device, "203.0.113.5")
    plain = code.replace("-", "")
    c.cookies.set(bridge.PAIR_COOKIE, plain, domain="localhost.local", path="/oauth")
    reg, nonce, page = open_authorize(c)
    assert 'id="pc-approve"' not in page  # the page is opened from another network than the PC's
    r = c.post("/oauth/approve", json={"nonce": nonce, "use_pair_cookie": True}, headers=from_ip("198.51.100.7"))
    assert r.status_code == 403 and r.json()["error_description"] == bridge._BAD_CODE
    assert set_cookie_header(r).startswith('kw_pair="";') and bridge.PAIR_COOKIE not in c.cookies
    assert store.peek_pair(t, crypto.sha256_hex(plain))  # not used up
    assert int(t.get_item(Key={"PK": f"AUTHREQ#{nonce}", "SK": "META"})["Item"]["attempts"]) == 1
    # the same cookie on the PC's own network still works
    c.cookies.set(bridge.PAIR_COOKIE, plain, domain="localhost.local", path="/oauth")
    page = c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT, "state": "s1",
        "code_challenge": crypto.pkce_s256("v" * 43), "code_challenge_method": "S256"},
        headers=from_ip("203.0.113.5")).text
    assert 'id="pc-approve"' in page
    ok = c.post("/oauth/approve", json={"nonce": nonce, "use_pair_cookie": True}, headers=from_ip("203.0.113.5"))
    assert ok.status_code == 200 and "redirect_to" in ok.json()
    assert store.peek_pair(t, crypto.sha256_hex(plain)) is None

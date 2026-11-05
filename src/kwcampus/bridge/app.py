"""Bridge server: OAuth for Claude/ChatGPT connectors + MCP tools answered by the user's browser.

Flow for a personal tool call:
  connector --Bearer--> /mcp --(WebSocket)--> user's PC agent --(own KLAS login)--> KLAS
  PC agent --AES-GCM ciphertext--> /device/result --> this request decrypts in memory --> answer

Connecting, on /oauth/authorize when the user adds the connector:
  PC 에이전트 (default): `kw-mcp link` registers the PC (public key only) and prints a one-time
    connection code; the password never leaves the PC.
  간편 연결 (off unless /kw/bridge/password-mode is "true"): KLAS id/password typed once, sealed into
    the tokens the connector holds and unsealed per request to log in from here (bridge/password.py).
    Off because KLAS demands 2FA for logins from AWS addresses.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

import boto3
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from kwcampus import params, public_http
from kwcampus.bridge import crypto, install, password, shapes, store
from kwcampus.config import KST
from kwcampus.obs import log

BASE_URL = os.environ.get("KW_BRIDGE_BASE_URL", "http://localhost:8080").rstrip("/")
ISSUER = str(AnyHttpUrl(BASE_URL))  # pydantic adds the trailing slash; use the same string everywhere
RESOURCE = f"{BASE_URL}/mcp"
WS_MGMT_URL = os.environ.get("KW_BRIDGE_WS_MGMT", "")
WS_URL = os.environ.get("KW_BRIDGE_WS_URL", "")
JWT_SECRET_PARAM = "/kw/bridge/jwt-secret"
INVITES_PARAM = "/kw/mcp/tokens"  # an MCP access token doubles as an invite code
SCOPE = "klas.read"
ACCESS_TTL = 3600
SNAPSHOT_WAIT_S = 22
SNAPSHOT_CACHE_S = 60
DEVICE_SKEW_S = 300
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no I/L/O/U/0/1

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

INSTRUCTIONS = """\
광운대학교 KLAS 개인 정보(과제·퀴즈·온라인강의·팀플·시간표·과목 게시판)와 학교 공개 정보(공지·학식·학사일정)를 조회합니다.
- 개인 정보는 요청할 때마다 사용자 PC의 KW Campus 에이전트가 사용자 본인의 KLAS 로그인으로 읽어 옵니다. PC가 켜져 있어야 합니다(브라우저는 필요 없음).
- "이번 주 할 일"은 this_week, "오늘 뭐 있어"는 today_briefing, "새 공지"는 my_notices를 먼저 쓰세요.
- 응답의 data_as_of는 KLAS에서 읽은 시각입니다. 읽기 전용이며 제출·수정은 할 수 없습니다. 비공식 도구입니다.
"""


def _jwt_secret() -> bytes:
    secret = params.get(JWT_SECRET_PARAM, decrypt=True, ttl_s=600)
    if not secret:
        raise RuntimeError("bridge JWT secret is not configured")
    return secret.encode()


class BridgeTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        claims = crypto.verify_jwt(_jwt_secret(), token, RESOURCE)
        if not claims or not _grant_alive(claims["sub"]):
            return None  # bad token, unlinked PC, or a password KLAS rejected: the client re-authorizes
        return AccessToken(token=token, client_id=claims["client_id"], scopes=claims.get("scope", "").split(),
                           expires_at=int(claims["exp"]), resource=RESOURCE, subject=claims["sub"])


# 간편 연결 is off unless this switch is "true". KLAS asks for 2FA (app or e-mail) on logins from
# IPs it treats as foreign (notice DUID 53483, from 2026-10-26), and it treats AWS addresses that way,
# so a server-side login can't run unattended. Turning it off also ends existing 간편 connections.
PASSWORD_MODE_PARAM = "/kw/bridge/password-mode"


def _password_mode() -> bool:
    return params.is_true(PASSWORD_MODE_PARAM)


def _grant_alive(sub: str) -> bool:
    t = store.table()
    if sub.startswith("p_"):
        return _password_mode() and not store.is_bad_login(t, sub[2:])
    return store.device_key(t, sub) is not None


mcp = MCPServer(
    "kw-campus",
    instructions=INSTRUCTIONS,
    token_verifier=BridgeTokenVerifier(),
    auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE, required_scopes=[SCOPE],
                      validate_token_resource=True),
)


# --- snapshot from the user's extension ---------------------------------------------

_SNAP: dict[str, tuple[float, dict]] = {}


def _post_to_connection(conn: str, message: dict) -> bool:
    client = boto3.client("apigatewaymanagementapi", endpoint_url=WS_MGMT_URL)
    try:
        client.post_to_connection(ConnectionId=conn, Data=json.dumps(message).encode())
        return True
    except client.exceptions.GoneException:
        return False


_ERRORS = {
    "klas_bad_password": "KLAS가 저장된 비밀번호를 거부했습니다(비밀번호를 바꿨다면 이 경우입니다). 같은 비밀번호로 계속 시도하면 보안문자가 걸리므로 이 연결을 멈췄습니다. Claude/ChatGPT에서 커넥터를 다시 연결해 주세요.",
    "klas_login_required": "PC 에이전트가 KLAS에 로그인하지 못했습니다. PC에서 `kw-mcp setup`으로 학번·비밀번호를 다시 저장해 주세요.",
    "klas_blocked": "KLAS가 자동 로그인을 막았습니다(보안문자 또는 2차 인증).",
    "klas_error": "KLAS에서 데이터를 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
}


async def snapshot_for(uid: str, claims: dict | None = None) -> dict:
    hit = _SNAP.get(uid)
    if hit and time.monotonic() - hit[0] < SNAPSHOT_CACHE_S:
        return hit[1]
    data = await (_password_snapshot(uid, claims or {}) if uid.startswith("p_") else _agent_snapshot(uid))
    if data.get("error"):
        detail = str(data.get("message") or "")[:300]
        raise ToolError(" ".join(x for x in (_ERRORS.get(data["error"], _ERRORS["klas_error"]), detail) if x))
    _SNAP[uid] = (time.monotonic(), data)
    return data


async def _password_snapshot(uid: str, claims: dict) -> dict:
    login = password.unseal(str(claims.get("cred", "")), str(claims.get("client_id", "")))
    if not login or f"p_{login.cid}" != uid:
        raise ToolError("연결 정보를 읽지 못했습니다. Claude/ChatGPT에서 커넥터를 다시 연결해 주세요.")
    data = await password.snapshot(login)
    if data.get("error") == "klas_bad_password":
        store.mark_bad_login(store.table(), login.cid)
    return data


async def _agent_snapshot(uid: str) -> dict:
    t = store.table()
    conn = store.latest_connection(t, uid)
    if not conn:
        raise ToolError("PC 에이전트가 연결돼 있지 않습니다. PC가 켜져 있는지 확인해 주세요(로그인하면 에이전트가 자동으로 연결됩니다).")
    request_id, key = crypto.random_token(16), crypto.new_result_key()
    store.open_result(t, request_id)  # /device/result only fills slots opened here
    command = {"type": "snapshot", "request_id": request_id, "key": crypto.b64u(key),
               "result_url": f"{BASE_URL}/device/result"}
    if not await asyncio.to_thread(_post_to_connection, conn, command):
        store.remove_connection(t, conn)
        raise ToolError("PC 에이전트 연결이 방금 끊겼습니다. 잠시 뒤 다시 시도해 주세요.")
    deadline = time.monotonic() + SNAPSHOT_WAIT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(0.4)
        item = store.take_result(t, request_id)
        if item:
            return crypto.decrypt_result(key, item["iv"], item["ct"], request_id)
    store.close_result(t, request_id)
    raise ToolError("PC 에이전트가 제때 응답하지 않았습니다. PC가 잠자기 상태가 아닌지 확인해 주세요.")


async def _snapshot() -> dict:
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("인증이 필요합니다.")
    claims = crypto.verify_jwt(_jwt_secret(), token.token, RESOURCE) if token.subject.startswith("p_") else None
    return await snapshot_for(token.subject, claims)


def _now() -> datetime:
    return datetime.now(UTC).astimezone(KST)


# --- personal tools -----------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY)
async def my_courses() -> list[dict[str, Any]]:
    """내 수강 과목: 과목명, 교수, 강의 시간(요일·교시·강의실)."""
    return shapes.courses(await _snapshot())


@mcp.tool(annotations=READ_ONLY)
async def my_assignments(course: str | None = None, pending_only: bool = False) -> list[dict[str, Any]]:
    """내 과제(마감순): 시작·마감·지각 마감, 제출 여부, 점수. course는 과목명 일부."""
    return shapes.assignments(await _snapshot(), course, pending_only)


@mcp.tool(annotations=READ_ONLY)
async def my_quizzes(course: str | None = None, pending_only: bool = False) -> list[dict[str, Any]]:
    """내 퀴즈(마감순): 유형, 시작·마감, 제출 여부, 제한 시간, 문항 수."""
    return shapes.quizzes(await _snapshot(), course, pending_only)


@mcp.tool(annotations=READ_ONLY)
async def my_online_lectures(course: str | None = None, incomplete_only: bool = False) -> list[dict[str, Any]]:
    """내 온라인 강의(마감순): 주차, 수강 기간, 진도율."""
    return shapes.online_lectures(await _snapshot(), course, incomplete_only)


@mcp.tool(annotations=READ_ONLY)
async def my_team_projects(course: str | None = None) -> list[dict[str, Any]]:
    """내 팀 프로젝트: 기간, 제출 여부, 점수, 팀 번호(팀원 정보 없음)."""
    return shapes.projects(await _snapshot(), course)


@mcp.tool(annotations=READ_ONLY)
async def my_course_notices(course: str | None = None) -> list[dict[str, Any]]:
    """내 과목 게시판 최근 글(공지·강의 자료실 등)."""
    return shapes.course_posts(await _snapshot(), course)


@mcp.tool(annotations=READ_ONLY)
async def my_deadlines(days: int = 7) -> dict[str, Any]:
    """앞으로 days일 안에 마감되는 미제출 과제·퀴즈·팀플과 미완료 온라인 강의."""
    return shapes.deadlines(await _snapshot(), _now(), days)


@mcp.tool(annotations=READ_ONLY)
async def this_week(days: int = 7) -> dict[str, Any]:
    """이번 주 할 일: 마감 목록과 같은 기간의 학사일정."""
    snap = await _snapshot()
    now = _now()
    return {**shapes.deadlines(snap, now, days),
            "academic_events": public_http.academic_calendar(now.date().isoformat(),
                                                             (now.date() + timedelta(days=days)).isoformat())}


@mcp.tool(annotations=READ_ONLY)
async def today_briefing() -> dict[str, Any]:
    """오늘 브리핑: 오늘 수업(교시·강의실), 오늘·내일 마감, 오늘 학식, 최근 과목 게시판 글과 학교 공지."""
    snap = await _snapshot()
    now = _now()
    end_tomorrow = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    soon = [i for i in shapes.deadlines(snap, now, 2)["items"]
            if (shapes.parse_kst(i.get("late_due") if i.get("late_submission") else i["due"]) or end_tomorrow) <= end_tomorrow]
    since = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    return {
        "date": now.strftime("%Y-%m-%d"), "weekday": shapes.WEEKDAYS[now.weekday()],
        "classes": shapes.classes_on(snap, now), "due_today_or_tomorrow": soon,
        "cafeteria": public_http.cafeteria_menu(now.date().isoformat()),
        "new_course_posts": [p for p in shapes.course_posts(snap) if (p["posted"] or "") >= since],
        "new_school_notices": public_http.search_notices("", None, (now.date() - timedelta(days=1)).isoformat(), 10),
        "data_as_of": snap.get("fetched_at"),
    }


@mcp.tool(annotations=READ_ONLY)
async def my_notices(days: int = 7) -> dict[str, Any]:
    """내 공지함: 최근 days일의 내 과목 게시판 글 + 학교 학사·장학 공지."""
    snap = await _snapshot()
    now = _now()
    since_dt = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    since = (now.date() - timedelta(days=days)).isoformat()
    return {"days": days,
            "course_posts": [p for p in shapes.course_posts(snap) if (p["posted"] or "") >= since_dt],
            "school_academic": public_http.search_notices("", "학사", since, 20),
            "school_scholarship": public_http.search_notices("", "장학", since, 20),
            "data_as_of": snap.get("fetched_at")}


# public tools: same functions as the public MCP, so one connector covers everything
for _fn in (public_http.latest_notices, public_http.search_notices, public_http.get_notice,
            public_http.cafeteria_menu, public_http.academic_calendar):
    mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))(_fn)


# --- OAuth authorization server -------------------------------------------------------

def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return _json({"error": error, "error_description": description}, status)


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def as_metadata(request: Request) -> Response:
    return _json({
        "issuer": ISSUER,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
    })


# Only the apps this connector is for may receive codes; otherwise anyone could register a client
# that redirects to their own site and phish a connection through the approval page. Exact URLs,
# not host checks: urlparse and browsers disagree on inputs like "https://evil.example\\@claude.ai/".
REDIRECT_URIS = frozenset({
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
})
MAX_REDIRECT_URIS = 5


def _redirect_ok(uri: str) -> bool:
    if uri in REDIRECT_URIS:
        return True
    # Claude Code and MCP Inspector listen on the user's own PC.
    if len(uri) > 300 or any(ch in uri for ch in "\\@ \t\r\n#"):
        return False
    parsed = urlparse(uri)
    return parsed.scheme == "http" and parsed.netloc.split(":")[0] in ("localhost", "127.0.0.1")


@mcp.custom_route("/oauth/register", methods=["POST"])
async def register_client(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        return _oauth_error("invalid_client_metadata", "JSON body required")
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not 0 < len(uris) <= MAX_REDIRECT_URIS \
            or not all(isinstance(u, str) and _redirect_ok(u) for u in uris):
        return _oauth_error("invalid_redirect_uri", "only Claude, ChatGPT or localhost callback URLs are accepted")
    client_id = "c_" + crypto.random_token(16)
    name = str(body.get("client_name") or "MCP client")[:100]
    store.put_client(store.table(), client_id, name, uris)
    return _json({"client_id": client_id, "client_name": name, "redirect_uris": uris,
                  "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"], "client_id_issued_at": int(time.time())}, 201)


_AUTHORIZE_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>KW Campus 연결</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;padding:0 16px;color:#222;line-height:1.5}}
label{{display:block;margin:10px 0 2px;font-size:14px}} input{{font-size:16px;padding:8px 10px;width:100%;box-sizing:border-box}}
button{{font-size:16px;padding:10px 18px;border-radius:8px;border:0;background:#8b1d2c;color:#fff;cursor:pointer;margin-top:12px}}
details{{margin-top:28px}} .muted{{color:#666;font-size:13px}} #status{{margin-top:16px;font-weight:600}}</style></head><body>
<h2>KW Campus 연결</h2>
<p><b>{client}</b>이(가) 내 KLAS 과제·퀴즈·온라인강의·시간표·과목 공지를 <b>읽기 전용</b>으로 조회하려고 합니다.</p>
<p class="muted">승인하면 <b>{redirect_host}</b>(으)로 돌아갑니다. 직접 Claude·ChatGPT에서 커넥터를 추가하다 열린 페이지가 아니라면 입력하지 마세요.</p>
{password_block}
{agent_block}
<p id="status"></p>
<p class="muted">광운대학교가 만들거나 승인한 도구가 아닙니다. 연결한 사람 본인의 정보만 조회합니다.</p>
<script>
const nonce = {nonce_json};
const status = document.getElementById('status');
async function send(body, button) {{
  button.disabled = true;
  status.textContent = body.mode === 'password' ? 'KLAS 로그인 확인 중…' : '확인 중…';
  try {{
    const r = await fetch('/oauth/approve', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{nonce, ...body}})}});
    const j = await r.json();
    if (j.redirect_to) {{ status.textContent = '연결됐습니다. 돌아가는 중…'; location.href = j.redirect_to; return; }}
    status.textContent = j.error_description || j.error;
  }} catch (e) {{ status.textContent = '서버에 연결하지 못했습니다. 다시 시도해 주세요.'; }}
  button.disabled = false;
}}
const v = (id) => document.getElementById(id).value;
const pw = document.getElementById('pw');
if (pw) pw.onsubmit = (e) => {{ e.preventDefault();
  send({{mode: 'password', invite: v('invite'), login_id: v('login_id'), password: v('password')}}, e.submitter); }};
document.getElementById('pc').onsubmit = (e) => {{ e.preventDefault(); send({{mode: 'code', code: v('code')}}, e.submitter); }};
</script></body></html>"""

_PASSWORD_FORM = """<form id="pw">
<label for="invite">초대 코드</label><input id="invite" autocomplete="off" required>
<label for="login_id">KLAS 학번</label><input id="login_id" autocomplete="username" inputmode="numeric" required>
<label for="password">KLAS 비밀번호</label><input id="password" type="password" autocomplete="current-password" required>
<p class="muted">비밀번호는 서버에 저장하지 않습니다. 암호화해서 이 연결(Claude/ChatGPT가 보관하는 토큰) 안에만 두고,
조회할 때마다 서버 메모리에서만 풀어 KLAS에 로그인합니다. 비밀번호를 서버에 맡기기 싫다면 아래 PC 에이전트 방식을 쓰세요.</p>
<button>연결</button></form>"""

_AGENT_FORM = """<form id="pc"><label for="code">연결 코드</label><input id="code" autocomplete="off" placeholder="XXXX-XXXX" required>
<p class="muted">PC에 KW Campus 에이전트를 설치하고 <code>kw-mcp code</code>를 실행하면 나오는 코드입니다(15분, 1회용).
에이전트가 없다면 <a href="/setup" target="_blank">설치 안내</a>를 보세요.</p>
<button>승인</button></form>"""


def _authorize_blocks() -> dict[str, str]:
    if _password_mode():
        return {"password_block": _PASSWORD_FORM,
                "agent_block": f"<details><summary>PC 에이전트로 연결 (비밀번호가 PC 밖으로 나가지 않음)</summary>{_AGENT_FORM}</details>"}
    return {"password_block": "",
            "agent_block": "<p class=\"muted\">데이터는 내 PC의 KW Campus 에이전트가 내 KLAS 로그인으로 읽어 옵니다. "
                           "비밀번호는 PC 밖으로 나가지 않고, 서버에는 저장하지 않습니다.</p>" + _AGENT_FORM}


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def authorize(request: Request) -> Response:
    q = request.query_params
    client = store.get_client(store.table(), q.get("client_id", ""))
    redirect_uri = q.get("redirect_uri", "")
    if not client or redirect_uri not in client["redirect_uris"]:
        return HTMLResponse("<p>알 수 없는 클라이언트이거나 redirect_uri가 등록되지 않았습니다.</p>", status_code=400)
    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        return HTMLResponse("<p>PKCE(S256)를 쓰는 authorization code 요청만 받습니다.</p>", status_code=400)
    nonce = crypto.random_token(24)
    store.put_authreq(store.table(), nonce, {
        "client_id": client["PK"][len("CLIENT#"):], "redirect_uri": redirect_uri, "state": q.get("state", ""),
        "code_challenge": q["code_challenge"], "resource": q.get("resource", RESOURCE),
    })
    return HTMLResponse(_AUTHORIZE_PAGE.format(nonce_json=json.dumps(nonce), client=html.escape(client["client_name"]),
                                               redirect_host=html.escape(urlparse(redirect_uri).hostname or ""),
                                               **_authorize_blocks()),
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "frame-ancestors 'none'"})


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch in CODE_ALPHABET)


def _invite_token(text: str) -> str:
    """Invites come as the raw token or as the kwc_... string `kw-mcp link` takes."""
    text = text.strip()
    if text.startswith("kwc_"):
        try:
            return str(json.loads(crypto.b64u_decode(text[4:]))["t"])
        except (ValueError, KeyError, TypeError):
            return ""
    return text


def _with_seal(token: str, sealed: str | None) -> str:
    return f"{token}.{sealed}" if sealed else token


def _sealed_part(token: str) -> str | None:
    return token.split(".", 1)[1] if "." in token else None


_APPROVE_ERRORS = {
    "klas_bad_password": "KLAS가 학번 또는 비밀번호를 거부했습니다. 여러 번 틀리면 KLAS가 보안문자를 요구하니 확인한 뒤 입력해 주세요.",
    "klas_blocked": "KLAS가 자동 로그인을 막았습니다(보안문자 또는 2차 인증).",
    "klas_error": "KLAS에 로그인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
}


def _refuse(t, nonce: str, message: str, reason: str) -> Response:
    log("approve_refused", reason=reason)
    if store.count_attempt(t, nonce) >= store.MAX_CODE_ATTEMPTS:
        store.take_authreq(t, nonce)
        return _oauth_error("access_denied", "여러 번 실패해 이 승인 요청을 닫았습니다. Claude/ChatGPT에서 다시 연결해 주세요.", 403)
    return _oauth_error("access_denied", message, 403)


def _grant(t, nonce: str, uid: str, login: password.Login | None = None) -> Response:
    req = store.take_authreq(t, nonce)
    if not req:
        return _oauth_error("invalid_request", "이미 처리된 승인 요청입니다.")
    sealed = password.seal(login, req["client_id"]) if login else None
    code = _with_seal(crypto.random_token(32), sealed)
    store.put_code(t, crypto.sha256_hex(code), {"uid": uid, "client_id": req["client_id"],
                                                "redirect_uri": req["redirect_uri"],
                                                "code_challenge": req["code_challenge"]})
    if not uid.startswith("p_"):
        _notify_paired(t, uid, req)
    log("approve_granted", mode="password" if login else "code")
    query = urlencode({"code": code, **({"state": req["state"]} if req.get("state") else {}), "iss": ISSUER})
    sep = "&" if "?" in req["redirect_uri"] else "?"
    return _json({"redirect_to": f"{req['redirect_uri']}{sep}{query}"})


def _notify_paired(t, uid: str, req: dict) -> None:
    """Let the PC agent log which app its code just connected (a leaked code shows up there)."""
    conn = store.latest_connection(t, uid)
    client = store.get_client(t, req["client_id"]) or {}
    if conn:
        try:
            _post_to_connection(conn, {"type": "paired", "client_name": str(client.get("client_name", ""))[:100],
                                       "redirect_host": urlparse(req["redirect_uri"]).hostname or ""})
        except Exception:  # best effort; the grant itself already succeeded
            log("paired_notice_failed")


@mcp.custom_route("/oauth/approve", methods=["POST"])
async def approve(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        return _oauth_error("invalid_request", "JSON body required")
    nonce = str(body.get("nonce", ""))
    t = store.table()
    if not store.peek_authreq(t, nonce):
        return _oauth_error("invalid_request", "승인 요청이 만료됐습니다. Claude/ChatGPT에서 다시 연결해 주세요.")
    if body.get("mode") == "password":
        if not _password_mode():
            return _oauth_error("access_denied", "간편 연결은 지금 쓸 수 없습니다. PC 에이전트 연결 코드를 쓰세요.", 403)
        return await _approve_password(t, nonce, body)
    code = _normalize_code(str(body.get("code", "")))
    uid = store.take_pair(t, crypto.sha256_hex(code)) if len(code) == 8 else None
    if not uid or not store.device_key(t, uid):
        return _refuse(t, nonce, "연결 코드가 맞지 않거나 만료됐습니다. PC에서 `kw-mcp code`로 새 코드를 받으세요.", "bad_code")
    return _grant(t, nonce, uid)


async def _approve_password(t, nonce: str, body: dict) -> Response:
    # The invite is checked before anything reaches KLAS, so the page can't be used to test logins.
    if not _invite_ok(_invite_token(str(body.get("invite", "")))):
        return _refuse(t, nonce, "초대 코드가 올바르지 않습니다.", "bad_invite")
    login_id, secret = str(body.get("login_id", "")).strip(), str(body.get("password", ""))
    if not (0 < len(login_id) <= 32 and 0 < len(secret) <= 128):
        return _oauth_error("invalid_request", "학번과 비밀번호를 입력해 주세요.")
    login = password.Login(crypto.random_token(12), login_id, secret)
    problem = await password.check(login)
    if problem:
        detail = str(problem.get("message") or "")[:200]
        return _refuse(t, nonce, " ".join(x for x in (detail, _APPROVE_ERRORS.get(problem["error"], _APPROVE_ERRORS["klas_error"])) if x),
                       problem["error"])
    return _grant(t, nonce, f"p_{login.cid}", login)


def _issue_tokens(uid: str, client_id: str, sealed: str | None = None) -> dict:
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": uid, "aud": RESOURCE, "client_id": client_id, "scope": SCOPE,
              "iat": now, "exp": now + ACCESS_TTL, **({"cred": sealed} if sealed else {})}
    access = crypto.sign_jwt(_jwt_secret(), claims)
    refresh = _with_seal(crypto.random_token(32), sealed)
    store.put_refresh(store.table(), crypto.sha256_hex(refresh), {"uid": uid, "client_id": client_id})
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": refresh, "scope": SCOPE}


@mcp.custom_route("/oauth/token", methods=["POST"])
async def token(request: Request) -> Response:
    form = await request.form()
    grant = form.get("grant_type")
    t = store.table()
    if grant == "authorization_code":
        raw = str(form.get("code", ""))
        code = store.take_code(t, crypto.sha256_hex(raw))
        if not code or code["client_id"] != form.get("client_id") or code["redirect_uri"] != form.get("redirect_uri"):
            return _oauth_error("invalid_grant", "code is invalid, used, or expired")
        if not hmac.compare_digest(crypto.pkce_s256(str(form.get("code_verifier", ""))), code["code_challenge"]):
            return _oauth_error("invalid_grant", "PKCE verification failed")
        return _json(_issue_tokens(code["uid"], code["client_id"], _sealed_part(raw)))
    if grant == "refresh_token":
        raw = str(form.get("refresh_token", ""))
        old = store.take_refresh(t, crypto.sha256_hex(raw))
        if not old or old["client_id"] != form.get("client_id") or not _grant_alive(old["uid"]):
            return _oauth_error("invalid_grant", "refresh token is invalid or expired")
        return _json(_issue_tokens(old["uid"], old["client_id"], _sealed_part(raw)))
    return _oauth_error("unsupported_grant_type", "authorization_code or refresh_token")


# --- device endpoints -----------------------------------------------------------------

def _invite_ok(invite: str) -> bool:
    known = json.loads(params.get(INVITES_PARAM, ttl_s=0) or "[]")
    digest = hashlib.sha256(invite.encode()).hexdigest()
    return any(hmac.compare_digest(digest, k) for k in known)


@mcp.custom_route("/device/register", methods=["POST"])
async def device_register(request: Request) -> Response:
    body = await request.json()
    if not _invite_ok(str(body.get("invite", ""))):
        log("device_register_refused")
        return _json({"error": "invalid_invite"}, 403)
    jwk = body.get("public_key") or {}
    try:
        crypto.public_key_from_jwk(jwk)
    except (ValueError, KeyError, TypeError):
        return _json({"error": "invalid_public_key"}, 400)
    uid = "u_" + crypto.random_token(16)
    store.put_device(store.table(), uid, jwk)
    return _json({"uid": uid, "ws_url": WS_URL, "mcp_url": RESOURCE}, 201)


def _signed_device(body: dict, purpose: str) -> str | None:
    """uid if body carries a fresh device signature over 'kw-<purpose>:<uid>:<ts>'."""
    uid, ts, sig = str(body.get("uid", "")), str(body.get("ts", "")), str(body.get("sig", ""))
    try:
        fresh = abs(time.time() - int(ts)) <= DEVICE_SKEW_S
    except ValueError:
        return None
    t = store.table()
    jwk = store.device_key(t, uid) if fresh else None
    if not jwk or not crypto.verify_device_signature(jwk, f"kw-{purpose}:{uid}:{ts}", sig):
        log("device_signature_refused", purpose=purpose, fresh=fresh, known_device=bool(jwk))
        return None
    if not store.use_once(t, crypto.sha256_hex(sig), DEVICE_SKEW_S * 2):
        log("device_signature_replayed", purpose=purpose)
        return None
    return uid


@mcp.custom_route("/device/pair", methods=["POST"])
async def device_pair(request: Request) -> Response:
    uid = _signed_device(await request.json(), "pair")
    if not uid:
        return _json({"error": "invalid_signature"}, 403)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    store.put_pair(store.table(), crypto.sha256_hex(code), uid)
    return _json({"code": f"{code[:4]}-{code[4:]}", "expires_in": store.PAIR_TTL, "mcp_url": RESOURCE})


@mcp.custom_route("/device/unlink", methods=["POST"])
async def device_unlink(request: Request) -> Response:
    uid = _signed_device(await request.json(), "unlink")
    if not uid:
        return _json({"error": "invalid_signature"}, 403)
    store.delete_device(store.table(), uid)
    return _json({"ok": True})


@mcp.custom_route("/device/result", methods=["POST"])
async def device_result(request: Request) -> Response:
    body = await request.json()
    request_id, iv, ct = str(body.get("request_id", "")), str(body.get("iv", "")), str(body.get("ct", ""))
    if not request_id or not iv or not ct or len(ct) > store.MAX_RESULT_CHARS:
        return _json({"error": "bad_request"}, 400)
    stored = store.put_result(store.table(), request_id, iv, ct)
    return _json({"ok": stored}, 202 if stored else 409)


for _path, _handler in install.routes(BASE_URL):
    mcp.custom_route(_path, methods=["GET"])(_handler)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


# The public JSON API (/c/<token>/api/...) on this host too, so one invite code covers both the
# personal connector and the public data the Claude Desktop extension (.mcpb) reads.
_public_api = public_http.build_app()


def build_app():
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/c/"):
            if scope["path"].rstrip("/").endswith("/mcp"):  # the public MCP stays on its own API
                await PlainTextResponse("not found", status_code=404)(scope, receive, send)
                return
            await _public_api(scope, receive, send)
            return
        # No long-lived SSE streams on Lambda: the MCP path only takes POST.
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp" and scope["method"] != "POST":
            await PlainTextResponse("method not allowed", status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return
        await inner(scope, receive, send)

    return app


app = build_app()

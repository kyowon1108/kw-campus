"""Bridge server: OAuth for Claude/ChatGPT connectors + MCP tools answered by the user's PC agent.

Flow for a personal tool call:
  connector --Bearer--> /mcp --(WebSocket)--> user's PC agent --(own KLAS login)--> KLAS
  PC agent --AES-GCM ciphertext--> /device/result --> this request decrypts in memory --> answer

Connecting, on /oauth/authorize when the user adds the connector: `kw-mcp link` registers the PC
(public key only) and `kw-mcp code` prints a one-time connection code; the password never leaves
the PC. Each approval is a grant (store.GRANT_TTL, about one semester) that the PC can list
(/device/grants) and end (/device/revoke); unlinking the PC ends all of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

import boto3
from kw_mcp.combined import ACADEMIC_DEADLINE_WORDS, CAMPUS_ALERT_WORDS, HOLIDAY_WORDS
from kw_mcp.service import CLASS_CHANGE_WORDS, term_in_session
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from kwcampus import params, public_data, public_http
from kwcampus.bridge import crypto, install, shapes, signing, store
from kwcampus.config import KST, NOTICE_SOURCE
from kwcampus.obs import log

BASE_URL = os.environ.get("KW_BRIDGE_BASE_URL", "http://localhost:8080").rstrip("/")
ISSUER = str(AnyHttpUrl(BASE_URL))  # pydantic adds the trailing slash; use the same string everywhere
RESOURCE = f"{BASE_URL}/mcp"
_ORIGIN = "{0.scheme}://{0.netloc}".format(urlparse(BASE_URL))
WS_MGMT_URL = os.environ.get("KW_BRIDGE_WS_MGMT", "")
WS_URL = os.environ.get("KW_BRIDGE_WS_URL", "")
JWT_SECRET_PARAM = "/kw/bridge/jwt-secret"
INVITES_PARAM = "/kw/mcp/tokens"  # an MCP access token doubles as an invite code
SCOPE = "klas.read"
ACCESS_TTL = 3600
REFRESH_GRACE_S = 60  # a just-used refresh token still works this long (lost response, parallel refresh)
SNAPSHOT_WAIT_S = 22
SNAPSHOT_CACHE_S = 60
POLL_FIRST_S, POLL_MAX_S = 0.25, 1.0  # result polling backs off: each poll is a DynamoDB write
MAX_INFLIGHT = 3  # agent requests one PC may have open at once
MAX_CALLS_PER_MINUTE = 30  # agent requests one PC may get per minute (each can mean dozens of KLAS reads)
CALL_MIN_VERSION = 2  # PC agents from before single-item reads answer only "snapshot"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no I/L/O/U/0/1

GRANT_ID = re.compile(r"g_[A-Za-z0-9_-]{12}")
CLIENT_ID = re.compile(r"c_[A-Za-z0-9_-]{22}")
NONCE = re.compile(r"[A-Za-z0-9_-]{32}")
CODE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")  # S256: base64url of 32 bytes, no padding
CODE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")  # RFC 7636 4.1
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
IV = re.compile(r"[A-Za-z0-9_-]{1,24}")
MAX_JSON_BYTES = 16_384
MAX_RESULT_BYTES = store.MAX_RESULT_CHARS + 4096

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

INSTRUCTIONS = """\
광운대학교 KLAS 개인 정보(과제·퀴즈·온라인강의·팀플·시간표·과목 게시판)와 학교 공개 정보(공지·학식·학사일정)를 조회합니다.
- 개인 정보는 요청할 때마다 사용자 PC의 KW Campus 에이전트가 사용자 본인의 KLAS 로그인으로 읽어 옵니다. PC가 켜져 있어야 합니다(브라우저는 필요 없음).
- "이번 주 할 일"은 this_week, "오늘 뭐 있어"는 today_briefing, "새 공지"는 my_notices를 먼저 쓰세요.
- 수강정정·포기, 장학 신청, 성적 조회, 수업평가 같은 학사 기한은 my_academic_deadlines를 쓰세요.
- 결과에 incomplete가 있으면 그 부분을 읽지 못한 것이니 사용자에게 알리세요.
- 응답의 data_as_of는 KLAS에서 읽은 시각입니다. 읽기 전용이며 제출·수정은 할 수 없습니다. 비공식 도구입니다.
"""


def _jwt_secret() -> bytes:
    secret = params.get(JWT_SECRET_PARAM, decrypt=True, ttl_s=600)
    if not secret:
        raise RuntimeError("bridge JWT secret is not configured")
    return secret.encode()


def _invite_alive(row: dict) -> bool:
    """False once the invite this device (or grant) came in with is withdrawn from the list.
    Rows from before invites were recorded have none and stay alive."""
    invite_hash = row.get("invite_hash")
    if not invite_hash:
        return True
    known = json.loads(params.get(INVITES_PARAM, ttl_s=60) or "[]")
    return any(hmac.compare_digest(str(invite_hash), k) for k in known)


def _token_alive(claims: dict) -> bool:
    uid, gid = str(claims.get("sub", "")), claims.get("gid")
    if not signing.UID.fullmatch(uid):
        return False  # includes the removed 간편 연결's p_ subjects
    t = store.table()
    if gid is None:  # issued before grants existed; such tokens run out within ACCESS_TTL of the upgrade
        device = store.get_device(t, uid)
        return bool(device) and _invite_alive(device)
    grant = store.get_grant(t, uid, str(gid))
    return bool(grant) and grant.get("client_id") == claims.get("client_id") and _invite_alive(grant)


class BridgeTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        claims = crypto.verify_jwt(_jwt_secret(), token, RESOURCE)
        if not claims or not _token_alive(claims):
            return None  # bad token, revoked grant or unlinked PC: the client re-authorizes
        return AccessToken(token=token, client_id=claims["client_id"], scopes=claims.get("scope", "").split(),
                           expires_at=int(claims["exp"]), resource=RESOURCE, subject=claims["sub"])


mcp = MCPServer(
    "kw-campus",
    instructions=INSTRUCTIONS,
    token_verifier=BridgeTokenVerifier(),
    auth=AuthSettings(issuer_url=ISSUER, resource_server_url=RESOURCE, required_scopes=[SCOPE],
                      validate_token_resource=True),
    log_level="WARNING",
)
# Failed tool calls are logged at INFO with their text, which carries course lists and KLAS login
# hints; none of that may reach CloudWatch. (public_http sets this too: it is imported first.)
logging.getLogger("mcp").setLevel(logging.WARNING)


# --- answers from the user's PC agent ---------------------------------------------------

_SNAP: dict[str, tuple[float, dict]] = {}


def _sweep_snap() -> None:
    """Drop decrypted snapshots past the cache window. Runs on every request, so a warm instance
    holds one no longer than /privacy says even when no snapshot tool is called again."""
    now = time.monotonic()
    for stale in [k for k, (at, _) in _SNAP.items() if now - at >= SNAPSHOT_CACHE_S]:
        del _SNAP[stale]


def _post_to_connection(conn: str, message: dict) -> bool:
    client = boto3.client("apigatewaymanagementapi", endpoint_url=WS_MGMT_URL)
    try:
        client.post_to_connection(ConnectionId=conn, Data=json.dumps(message).encode())
        return True
    except client.exceptions.GoneException:
        return False


_ERRORS = {
    "klas_login_required": "PC 에이전트가 KLAS에 로그인하지 못했습니다. PC에서 `kw-mcp setup`으로 학번·비밀번호를 다시 저장해 주세요.",
    "klas_blocked": "KLAS가 자동 로그인을 막았습니다(보안문자 또는 2차 인증).",
    "klas_error": "KLAS에서 데이터를 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
    "too_large": "결과가 너무 큽니다.",
}


_SNAP_PENDING: dict[str, asyncio.Task] = {}  # one snapshot request per PC at a time


async def snapshot_for(uid: str) -> dict:
    _sweep_snap()
    hit = _SNAP.get(uid)
    if hit:
        return hit[1]
    pending = _SNAP_PENDING.get(uid)
    if pending is None:
        pending = asyncio.ensure_future(_agent_request(uid, {"type": "snapshot"}))
        _SNAP_PENDING[uid] = pending
        pending.add_done_callback(lambda _: _SNAP_PENDING.pop(uid, None))
    data = await asyncio.shield(pending)
    _raise_for(data)
    _SNAP[uid] = (time.monotonic(), data)
    return data


def _raise_for(data: dict) -> None:
    """The agent's error as a ToolError for the model. Its message is user-facing text only: the
    SDK logs ToolErrors at INFO, which is off, and nothing here logs or stores it."""
    if not data.get("error"):
        return
    detail = str(data.get("message") or "")[:300]
    if data["error"] == "not_found":
        raise ToolError(detail or "찾지 못했습니다.")
    if data["error"] == "unsupported":
        raise ToolError(_UPDATE_AGENT)
    raise ToolError(" ".join(x for x in (_ERRORS.get(data["error"], _ERRORS["klas_error"]), detail) if x))


_UPDATE_AGENT = "PC 에이전트가 옛 버전입니다. PC에서 처음 설치할 때 쓴 명령을 한 번 더 실행해 업데이트해 주세요(연결은 그대로 유지됩니다)."
_UNREADABLE = "PC 에이전트의 응답을 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요."


async def agent_call(op: str, **args: str):
    """One read-only item (assignment detail, syllabus, post) from the caller's PC agent."""
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("인증이 필요합니다.")
    data = await _agent_request(token.subject, {"type": "call", "op": op, "args": args}, CALL_MIN_VERSION)
    _raise_for(data)
    return data.get("result")


async def _agent_request(uid: str, payload: dict, need_version: int = 1) -> dict:
    t = store.table()
    info = store.latest_connection_info(t, uid)
    if info and info[1] < need_version:
        raise ToolError(_UPDATE_AGENT)
    conn = info[0] if info else None
    if not conn:
        raise ToolError("PC 에이전트가 연결돼 있지 않습니다. PC가 켜져 있는지 확인해 주세요(로그인하면 에이전트가 자동으로 연결됩니다).")
    if not store.count_request(t, uid, MAX_CALLS_PER_MINUTE):
        raise ToolError(f"이 PC에 1분 동안 보낼 수 있는 요청({MAX_CALLS_PER_MINUTE}개)을 넘었습니다. 1분 뒤 다시 시도해 주세요.")
    if not store.open_request(t, uid, MAX_INFLIGHT):
        raise ToolError(f"이 PC에서 이미 요청 {MAX_INFLIGHT}개를 처리하고 있습니다. 앞 요청이 끝난 뒤 다시 시도해 주세요.")
    try:
        request_id, key = crypto.random_token(16), crypto.new_result_key()
        store.open_result(t, request_id)  # /device/result only fills slots opened here
        command = {**payload, "request_id": request_id, "key": crypto.b64u(key),
                   "result_url": f"{BASE_URL}/device/result"}
        if not await asyncio.to_thread(_post_to_connection, conn, command):
            store.remove_connection(t, conn)
            raise ToolError("PC 에이전트 연결이 방금 끊겼습니다. 잠시 뒤 다시 시도해 주세요.")
        deadline, delay, item = time.monotonic() + SNAPSHOT_WAIT_S, POLL_FIRST_S, None
        while item is None and (left := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(delay, left))
            delay = min(delay * 1.5, POLL_MAX_S)
            item = store.take_result(t, request_id)
        if item is None:  # one last look that also shuts the slot, so nothing lands unread after it
            item = store.take_result(t, request_id, close=True)
        if item is None:
            raise ToolError("PC 에이전트가 제때 응답하지 않았습니다. PC가 잠자기 상태이거나 KLAS 응답이 느린 것일 수 있습니다. "
                            "1분 안에 다시 물어보면 PC에 남은 결과로 더 빨리 답합니다.")
    finally:
        store.close_request(t, uid)
    try:
        data = crypto.decrypt_result(key, item["iv"], item["ct"], request_id)
    except (ValueError, KeyError):
        raise ToolError(_UNREADABLE) from None
    if not isinstance(data, dict):
        raise ToolError(_UNREADABLE)
    return data


async def _snapshot() -> dict:
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("인증이 필요합니다.")
    return await snapshot_for(token.subject)


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
    """내 과목 게시판 최근 글(KLAS 홈 화면에 뜨는 몇 개만). 오래된 공지나 특정 주제(시험·휴강 등)는 my_course_board로 찾으세요."""
    return shapes.course_posts(await _snapshot(), course)


# What a snapshot can't tell: an agent from before 0.8 has no single "deadlines" request.
_OLD_AGENT_DEADLINES = {"missing": ["online_test", "survey", "discussion"],
                        "message": "PC 에이전트가 옛 버전이라 온라인시험·설문·토론 마감이 빠졌습니다. " + _UPDATE_AGENT}


def _clamp_days(days: int) -> int:
    return max(0, min(int(days), 180))


async def _deadlines(days: int) -> dict:
    """The PC agent's own deadline list, which also covers online exams, surveys and discussions.
    Agents from before 0.8 don't know that request, so their snapshot is shaped here instead,
    marked incomplete."""
    token = get_access_token()
    uid = token.subject if token is not None else None
    info = store.latest_connection_info(store.table(), uid) if uid else None
    if info and info[1] >= CALL_MIN_VERSION:
        data = await _agent_request(uid, {"type": "call", "op": "deadlines", "args": {"days": str(days)}},
                                    CALL_MIN_VERSION)
        if data.get("error") != "unsupported":
            _raise_for(data)
            return data.get("result") or {}
    return {**shapes.deadlines(await _snapshot(), _now(), days), "incomplete": _OLD_AGENT_DEADLINES}


@mcp.tool(annotations=READ_ONLY)
async def my_deadlines(days: int = 7) -> dict[str, Any]:
    """현재 학기에서 앞으로 days일(최대 180) 안에 마감되는 안 한 일: 과제·퀴즈·온라인시험(재응시 포함)·팀플·설문,
    진도 100% 미만 온라인 강의, 그 안에 끝나는 토론(참여 여부는 KLAS가 알려 주지 않음).
    마감이 지났지만 지각 제출 기간인 과제·팀플은 late_submission=true로 포함합니다(이때 due는 과거).
    incomplete가 있으면 일부 목록을 읽지 못한 것이니 사용자에게 알리세요."""
    return await _deadlines(_clamp_days(days))


@mcp.tool(annotations=READ_ONLY)
async def this_week(days: int = 7) -> dict[str, Any]:
    """이번 주 할 일(현재 학기): 마감 목록(지각 제출 가능 항목은 late_submission=true)과 같은 기간의 학사일정."""
    now, days = _now(), _clamp_days(days)
    deadlines = await _deadlines(days)
    span = int(deadlines.get("days", days))  # the calendar follows the agent's own clamp
    return {**deadlines,
            "academic_events": public_http.academic_calendar(
                now.date().isoformat(), (now.date() + timedelta(days=span)).isoformat())}


def _due_of(item: dict) -> str | None:
    return item.get("late_due") if item.get("late_submission") else item.get("effective_due") or item.get("due")


@mcp.tool(annotations=READ_ONLY)
async def today_briefing() -> dict[str, Any]:
    """오늘 브리핑: 오늘 수업(교시·강의실), 오늘·내일 마감, 오늘 학식, 최근 과목 게시판 글과 학교 공지.
    PC에서 일부를 받지 못하면 나머지로 답하고 빠진 부분을 incomplete(또는 deadlines_incomplete)에 적습니다."""
    # Both asked of the PC at once (one after the other could outlast the Lambda's 30 s). Each may
    # fail alone; gather waits for both, so neither is left running after this returns.
    snap, upcoming = await asyncio.gather(_snapshot(), _deadlines(2), return_exceptions=True)
    for outcome in (snap, upcoming):
        if isinstance(outcome, BaseException) and not isinstance(outcome, ToolError):
            raise outcome  # a bug or a cancellation, not the PC failing to answer
    now = _now()
    missing: list[str] = []
    problem = ""
    if isinstance(snap, ToolError):
        missing, problem, snap = ["classes", "new_course_posts"], str(snap), None
    if isinstance(upcoming, ToolError):
        if snap is not None:  # the snapshot still has assignments, quizzes, lectures and team projects
            upcoming = {**shapes.deadlines(snap, now, 2), "incomplete": {
                "missing": ["online_test", "survey", "discussion"],
                "message": f"PC 에이전트의 마감 목록을 받지 못해 온라인시험·설문·토론 마감이 빠졌습니다({upcoming})."}}
        else:
            missing.append("due_today_or_tomorrow")
            problem = problem or str(upcoming)
            upcoming = {}
    end_tomorrow = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    # Same rule as kw-mcp: a due that doesn't parse is left out, not counted as due today.
    soon = [i for i in upcoming.get("items", []) if (due := shapes.parse_due(_due_of(i))) and due <= end_tomorrow]
    since = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    in_session = snap is not None and term_in_session(snap.get("semester"), now)
    new_posts = [p for p in shapes.course_posts(snap) if (p["posted"] or "") >= since] if snap else []
    alerts = _campus_alerts(now)
    return {
        "date": now.strftime("%Y-%m-%d"), "weekday": shapes.WEEKDAYS[now.weekday()],
        # Same rule as kw-mcp: no timetable outside the semester's class period.
        "classes": shapes.classes_on(snap, now) if in_session else [],
        **({"classes_note": f"{snap.get('semester')} 학기 수업 기간이 아니라 시간표를 비웠습니다."}
           if snap is not None and not in_session else {}),
        "due_today_or_tomorrow": soon,
        **({"deadlines_incomplete": upcoming["incomplete"]} if upcoming.get("incomplete") else {}),
        "cafeteria": public_http.cafeteria_menu(now.date().isoformat()),
        "new_course_posts": new_posts,
        "possible_class_changes": [p for p in new_posts if CLASS_CHANGE_WORDS.search(p.get("title") or "")],
        # Same as kw-mcp: yesterday on, no pinned posts; latest() leaves the FAQ out on its own.
        "new_school_notices": public_data.latest(public_http._table(), None, 10, include_pinned=False,
                                                 since=now.date() - timedelta(days=1)),
        **({"campus_alerts": alerts} if alerts["notices"] or alerts["today"] else {}),
        **({"incomplete": {"missing": missing, "message": problem}} if missing else {}),
        "data_as_of": snap.get("fetched_at") if snap else upcoming.get("data_as_of"),
    }


def _campus_alerts(now: datetime) -> dict:
    """Same rule as kw_mcp.combined.campus_alerts: recent closures/outages/holidays and today's days off.
    Main board only (a department's 휴강 or a job post naming 기상청 is not a campus alert), selected
    by public_data before its limit; three days of the main board stay well under 50 posts."""
    today = now.date()
    recent = public_data.latest(public_http._table(), None, 50, include_pinned=True, since=today - timedelta(days=3),
                                sources=(NOTICE_SOURCE,))
    return {"notices": [n for n in recent if CAMPUS_ALERT_WORDS.search(n.get("title") or "")],
            "today": [e for e in public_http.academic_calendar(today.isoformat(), today.isoformat())
                      if HOLIDAY_WORDS.search(e.get("title") or "")]}


@mcp.tool(annotations=READ_ONLY)
async def my_notices(days: int = 7, department: str | None = None) -> dict[str, Any]:
    """내 공지함: 최근 days일(1~180)의 학교 학사·장학 공지 + 내 과목 게시판 새 글(KLAS 홈에 뜨는 최근 몇 개만).
    department에 내 학과·단과대 이름(예: "전자공학과", "인공지능융합대학")을 주면 그 학과 공지도 넣습니다.
    학과를 모르면 my_grades의 학과를 쓰거나 사용자에게 물어보세요. 오래된 과목 공지나 특정 주제는 my_course_board로 찾으세요."""
    days = max(1, min(int(days), 180))
    snap = await _snapshot()
    now = _now()
    since_dt = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    since = (now.date() - timedelta(days=days)).isoformat()
    out = {"days": days,
           "course_posts": [p for p in shapes.course_posts(snap) if (p["posted"] or "") >= since_dt],
           "school_academic": public_http.search_notices("", "학사", since, 20),
           "school_scholarship": public_http.search_notices("", "장학", since, 20),
           "data_as_of": snap.get("fetched_at")}
    if department and department.strip():
        try:
            out["department"] = public_http.search_notices("", department.strip(), since, 20)
        except ToolError as e:  # an unknown name: say so and keep the rest
            out["department_error"] = str(e)
    return out


@mcp.tool(annotations=READ_ONLY)
async def my_assignment_detail(course: str, title: str) -> dict[str, Any]:
    """과제 하나의 상세: 무엇을 내야 하는지(본문), 제출 형식, 첨부 파일 이름, 기간, 제출 상태.
    course는 과목명 일부, title은 과제 제목 일부(my_assignments 결과의 이름을 쓰세요)."""
    return await agent_call("assignment_detail", course=course, title=title)


@mcp.tool(annotations=READ_ONLY)
async def my_syllabus(course: str) -> dict[str, Any]:
    """내 수강 과목의 강의계획서: 개요·목표, 주차별 계획, 평가 비율, 교재. course는 과목명 일부."""
    return await agent_call("syllabus", course=course)


@mcp.tool(annotations=READ_ONLY)
async def my_course_board(course: str, keyword: str = "") -> dict[str, Any]:
    """한 과목의 강의 공지사항·자료실 전체에서 글 찾기(최신순, 게시판마다 10개, total은 전체 개수).
    keyword는 제목·본문 검색어(예: "시험", "휴강", "기말"). 본문은 my_course_post로 여세요."""
    return await agent_call("search_course_posts", course=course, keyword=keyword)


@mcp.tool(annotations=READ_ONLY)
async def my_course_post(course: str, title: str) -> dict[str, Any]:
    """과목 게시판(공지·자료실 등) 글 하나의 본문과 첨부 파일 이름.
    course는 과목명 일부, title은 글 제목 일부(my_course_notices 결과의 제목을 쓰세요)."""
    return await agent_call("course_post", course=course, title=title)


@mcp.tool(annotations=READ_ONLY)
async def my_attendance(course: str | None = None) -> list[dict[str, Any]]:
    """출석 현황(KLAS 전자출석): 과목별 출석·결석·지각·조퇴·공결 횟수와 주차·회차별 기록. course는 과목명 일부(생략하면 전체)."""
    return await agent_call("attendance", course=course or "")


@mcp.tool(annotations=READ_ONLY)
async def my_exams(course: str | None = None) -> list[dict[str, Any]]:
    """시험 정보 모아 보기: 과목별 강의계획서의 시험 관련 주차·평가 비율과, 게시판에서 '시험'이 들어간 글(최신 6개).
    "시험 범위", "기말 언제" 같은 질문에 먼저 쓰고, 범위·장소는 my_course_post로 글을 열어 확인하세요."""
    return await agent_call("exam_info", course=course or "")


@mcp.tool(annotations=READ_ONLY)
async def my_class_changes(days: int = 14) -> dict[str, Any]:
    """휴강·보강 공지: 모든 수강 과목의 강의 공지사항에서 최근 days일 안의 '휴강'·'보강' 글. "오늘 휴강이야?"에 쓰세요."""
    return await agent_call("class_changes", days=str(days))


@mcp.tool(annotations=READ_ONLY)
async def my_graduation() -> dict[str, Any]:
    """졸업 이수 현황: 취득 학점(전공·교양·기타), 필수교양(광운인되기)과 균형교양 8개 영역별 이수 학점,
    입학 연도에 맞는 교양 규칙 충족 여부, 학교 졸업 사정 결과(official_check: 명단에 오른 경우 졸업 가능 여부와 불가 사유).
    "균형교양 더 들어야 해?", "졸업 가능해?"에 쓰세요."""
    return await agent_call("graduation_progress")


@mcp.tool(annotations=READ_ONLY)
async def my_grades() -> dict[str, Any]:
    """성적·학점: 학기별 과목 성적과 학기 평점(4.5 만점, 계산값), KLAS 평량평균(학적부·성적증명서 기준),
    전공·교양·기타 신청/취득/삭제 학점, 학과·학년·학적 상태, 학기별 석차·학사경고(standing), 이번 학기 성적 조회 기간,
    summary(전공·전공 외 평점, F 제외 평점, 재수강하지 않은 F; 계산값). 이름·학번·이메일·지도교수는 포함하지 않습니다."""
    return await agent_call("grades")


@mcp.tool(annotations=READ_ONLY)
async def my_online_tests(course: str | None = None) -> list[dict[str, Any]]:
    """온라인시험: 응시 기간, 응시 여부(in_progress는 응시 중), 교수가 연 재응시 기간(retry)."""
    return await agent_call("online_tests", course=course or "")


@mcp.tool(annotations=READ_ONLY)
async def my_surveys(course: str | None = None) -> list[dict[str, Any]]:
    """과목 설문: 기간과 참여 여부."""
    return await agent_call("surveys", course=course or "")


@mcp.tool(annotations=READ_ONLY)
async def my_discussions(course: str | None = None) -> list[dict[str, Any]]:
    """과목 토론 주제: 시작·종료와 글 수. 내가 참여했는지는 KLAS 목록에 없어 토론실에서 확인해야 합니다."""
    return await agent_call("discussions", course=course or "")


@mcp.tool(annotations=READ_ONLY)
async def my_course_overview(course: str | None = None) -> list[dict[str, Any]]:
    """과목별 한눈에 보기(KLAS 과목 홈과 같은 숫자): 온라인강의 완료/전체, 과제·퀴즈·온라인시험·팀플·설문 제출/전체,
    토론 참여/전체, 새 글 수, 자료실 글 수, 출석·결석·지각·조퇴·공결 횟수. "이 과목 뭐 남았어?"에 먼저 쓰세요."""
    return await agent_call("course_overview", course=course or "")


@mcp.tool(annotations=READ_ONLY)
async def my_course_evaluation() -> dict[str, Any]:
    """수업평가: 지금 평가 기간인지(중간/기말)와 아직 평가하지 않은 과목. 평가는 KLAS에서 직접 해야 합니다."""
    return await agent_call("course_evaluation")


@mcp.tool(annotations=READ_ONLY)
async def my_academic_deadlines(days: int = 30) -> dict[str, Any]:
    """학사 기한 모음: KLAS가 알려 주는 장학 신청·취득학점포기·성적 조회·수업평가 기간과, 앞으로 days일(최대 180)
    학사일정 중 수강신청·정정·포기, 등록, 휴·복학, 성적·이의신청, 계절학기, 졸업 관련 일정."""
    days = max(1, min(int(days), 180))
    today = _now().date()
    end = today + timedelta(days=days)
    try:
        windows = await agent_call("application_windows")
    except ToolError as e:  # an older agent, or the PC is off: the school calendar still answers
        windows = {"error": str(e)}
    events = public_http.academic_calendar(today.isoformat(), end.isoformat())
    return {"from": today.isoformat(), "to": end.isoformat(), "klas": windows,
            "calendar": [e for e in events if ACADEMIC_DEADLINE_WORDS.search(e.get("title") or "")],
            "note": "klas는 KLAS가 지금 열려 있는지 직접 알려 준 값, calendar는 학교 학사일정입니다. 학과별 일정은 학과 공지를 확인하세요."}


@mcp.tool(annotations=READ_ONLY)
async def my_attachment(course: str, title: str, file_name: str = "", part: int = 1,
                        source: str = "post") -> dict[str, Any]:
    """첨부 파일 내용을 텍스트로 읽기(PDF·PPTX·DOCX·XLSX·HWP·HWPX·ipynb·코드/텍스트, zip은 파일 목록).
    source="post"면 과목 게시판 글(title=글 제목, my_course_board/my_course_post 결과), "assignment"면 과제의 첨부.
    file_name은 파일 이름 일부(첨부가 하나면 생략). 길면 part=2, 3…으로 이어서 읽으세요(total_parts 참고)."""
    return await agent_call("read_attachment", course=course, title=title, file_name=file_name,
                            part=str(part), source=source)


# public tools: same functions as the public MCP, so one connector covers everything
for _fn in (public_http.list_notice_categories, public_http.latest_notices, public_http.search_notices,
            public_http.get_notice, public_http.cafeteria_menu, public_http.academic_calendar):
    mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False))(_fn)


# --- request helpers ----------------------------------------------------------------------

def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return _json({"error": error, "error_description": description}, status)


async def _json_object(request: Request, max_bytes: int = MAX_JSON_BYTES) -> dict | None:
    """The body as a JSON object, or None (not JSON, not an object, or too big)."""
    try:
        if int(request.headers.get("content-length") or 0) > max_bytes:
            return None
        raw = await request.body()
        data = json.loads(raw) if len(raw) <= max_bytes else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean_name(value: object) -> str:
    """A client-chosen name, safe to show and log: no control or format characters (newlines that
    forge log lines, bidi overrides), whitespace squeezed, at most 100 characters."""
    text = "".join(ch if not unicodedata.category(ch).startswith("C") else " " if ch.isspace() else ""
                   for ch in str(value or ""))
    return " ".join(text.split())[:100] or "MCP client"


def _app_label(host: str) -> str:
    """Who the approval really goes to, judged by the redirect host (client_name is self-chosen)."""
    if host in ("claude.ai", "claude.com"):
        return "Claude"
    if host == "chatgpt.com":
        return "ChatGPT"
    if host in ("localhost", "127.0.0.1"):
        return "이 PC의 앱(Claude Code, MCP Inspector 등)"
    return host


# --- OAuth authorization server -------------------------------------------------------

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
        "authorization_response_iss_parameter_supported": True,
    })


# Only the apps this connector is for may receive codes; otherwise anyone could register a client
# that redirects to their own site and phish a connection through the approval page. Exact URLs,
# not host checks: urlparse and browsers disagree on inputs like "https://evil.example\\@claude.ai/".
REDIRECT_URIS = frozenset({
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
})
# ChatGPT's newer connectors each get their own callback (Apps SDK auth docs). Matched on the raw
# string with a tight charset, so there is nothing for a URL parser to disagree about.
CHATGPT_CALLBACK = re.compile(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,128}")
MAX_REDIRECT_URIS = 5


def _redirect_ok(uri: str) -> bool:
    if len(uri) > 300:
        return False
    if uri in REDIRECT_URIS or CHATGPT_CALLBACK.fullmatch(uri):
        return True
    # Claude Code and MCP Inspector listen on the user's own PC.
    if any(ch in uri for ch in "\\@ \t\r\n#"):
        return False
    parsed = urlparse(uri)
    return parsed.scheme == "http" and parsed.netloc.split(":")[0] in ("localhost", "127.0.0.1")


@mcp.custom_route("/oauth/register", methods=["POST"])
async def register_client(request: Request) -> Response:
    body = await _json_object(request)
    if body is None:
        return _oauth_error("invalid_client_metadata", "a JSON object of at most 16 KB is required")
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not 0 < len(uris) <= MAX_REDIRECT_URIS \
            or not all(isinstance(u, str) and _redirect_ok(u) for u in uris):
        return _oauth_error("invalid_redirect_uri", "only Claude, ChatGPT or localhost callback URLs are accepted")
    client_id = "c_" + crypto.random_token(16)
    name = _clean_name(body.get("client_name") if isinstance(body.get("client_name"), str) else None)
    store.put_client(store.table(), client_id, name, uris)
    return _json({"client_id": client_id, "client_name": name, "redirect_uris": uris,
                  "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"], "client_id_issued_at": int(time.time())}, 201)


_AUTHORIZE_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>KW Campus 연결</title>
<style>body{{font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;padding:0 16px;color:#222;line-height:1.5}}
label{{display:block;margin:10px 0 2px;font-size:14px}} input{{font-size:16px;padding:8px 10px;width:100%;box-sizing:border-box}}
button{{font-size:16px;padding:10px 18px;border-radius:8px;border:0;background:#8b1d2c;color:#fff;cursor:pointer;margin-top:12px}}
.muted{{color:#666;font-size:13px}} #status{{margin-top:16px;font-weight:600}}</style></head><body>
<h2>KW Campus 연결</h2>
<p><b>{app}</b>이(가) 내 KLAS 과제·퀴즈·시험·성적·출석·졸업 이수·시간표·과목 게시판과 첨부 파일을 <b>읽기 전용</b>으로 조회하려고 합니다.</p>
<p class="muted">승인하면 <b>{redirect_host}</b>(으)로 돌아갑니다. 앱이 스스로 밝힌 이름: “{client}”.
직접 Claude·ChatGPT에서 커넥터를 추가하다 열린 페이지가 아니라면 코드를 입력하지 마세요. 남이 보낸 링크에서 승인하면 그 사람이 내 정보를 보게 됩니다.</p>
<p class="muted">데이터는 내 PC의 KW Campus 에이전트가 내 KLAS 로그인으로 읽어 옵니다. 비밀번호는 PC 밖으로 나가지 않고, 서버에는 저장하지 않습니다.
연결은 {grant_days}일 동안 유지되고, PC 에이전트에서 연결된 앱을 보고 하나씩 끊을 수 있습니다.</p>
<form id="pc"><label for="code">연결 코드</label><input id="code" autocomplete="off" placeholder="XXXX-XXXX" required>
<p class="muted">PC에 KW Campus 에이전트를 설치하고 <code>kw-mcp code</code>를 실행하면 나오는 코드입니다(15분, 1회용).
에이전트가 없다면 <a href="/setup" target="_blank" rel="noopener">설치 안내</a>를 보세요.</p>
<button>승인</button></form>
<p id="status"></p>
<p class="muted">광운대학교가 만들거나 승인한 도구가 아닙니다. 연결한 사람 본인의 정보만 조회합니다.</p>
<script nonce="{script_nonce}">
const nonce = {nonce_json};
const status = document.getElementById('status');
document.getElementById('pc').onsubmit = async (e) => {{
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  status.textContent = '확인 중…';
  try {{
    const r = await fetch('/oauth/approve', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{nonce, code: document.getElementById('code').value}})}});
    const j = await r.json();
    if (j.redirect_to) {{ status.textContent = '연결됐습니다. 돌아가는 중…'; location.href = j.redirect_to; return; }}
    status.textContent = j.error_description || j.error;
  }} catch (err) {{ status.textContent = '서버에 연결하지 못했습니다. 다시 시도해 주세요.'; }}
  button.disabled = false;
}};
</script></body></html>"""


def _page(message: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><p>{html.escape(message)}</p>', status_code=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})


@mcp.custom_route("/oauth/authorize", methods=["GET"])
async def authorize(request: Request) -> Response:
    q = request.query_params
    t = store.table()
    client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
    client = store.get_client(t, client_id) if CLIENT_ID.fullmatch(client_id) else None
    if not client or redirect_uri not in client["redirect_uris"]:
        # Never redirect to an unverified URI. A client row that expired lands here: say how to recover.
        return _page("이 커넥터의 등록 정보가 없거나 만료됐습니다. Claude·ChatGPT에서 이 커넥터를 지우고 다시 추가해 주세요.")

    def back(error: str, description: str) -> Response:  # RFC 6749 4.1.2.1: errors go to the client
        query = urlencode({"error": error, "error_description": description,
                           **({"state": q["state"]} if q.get("state") else {}), "iss": ISSUER})
        return RedirectResponse(f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{query}", status_code=302)

    if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" \
            or not CODE_CHALLENGE.fullmatch(q.get("code_challenge", "")):
        return back("invalid_request", "only authorization code requests with PKCE S256 are accepted")
    resource = q.get("resource")
    if resource is not None and resource.rstrip("/") != RESOURCE:  # RFC 8707
        return back("invalid_target", f"the only resource here is {RESOURCE}")
    nonce = crypto.random_token(24)
    store.put_authreq(t, nonce, {"client_id": client_id, "redirect_uri": redirect_uri,
                                 "state": q.get("state", "")[:500], "code_challenge": q["code_challenge"]})
    host = urlparse(redirect_uri).hostname or ""
    script_nonce = crypto.random_token(16)
    page = _AUTHORIZE_PAGE.format(nonce_json=json.dumps(nonce), script_nonce=script_nonce,
                                  app=html.escape(_app_label(host)), redirect_host=html.escape(host),
                                  client=html.escape(_clean_name(client.get("client_name"))),
                                  grant_days=store.GRANT_TTL // 86400)
    return HTMLResponse(page, headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (f"default-src 'none'; script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; "
                                    "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"),
    })


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch in CODE_ALPHABET)


def _same_site(request: Request) -> bool:
    """Approvals come only from our own page's fetch: JSON (a cross-site form or no-cors fetch can't
    send that without a preflight we never grant), and the browser's Origin/Sec-Fetch-Site agree."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return False
    origin = request.headers.get("origin")
    if origin is not None and origin != _ORIGIN:
        return False
    return request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")


def _refuse(t, nonce: str, message: str, reason: str) -> Response:
    log("approve_refused", reason=reason)
    if store.count_attempt(t, nonce) >= store.MAX_CODE_ATTEMPTS:
        store.take_authreq(t, nonce)
        return _oauth_error("access_denied", "여러 번 실패해 이 승인 요청을 닫았습니다. Claude/ChatGPT에서 다시 연결해 주세요.", 403)
    return _oauth_error("access_denied", message, 403)


def _grant(t, nonce: str, uid: str, device: dict) -> Response:
    req = store.take_authreq(t, nonce)
    if not req:
        return _oauth_error("invalid_request", "이미 처리된 승인 요청입니다.")
    client = store.get_client(t, req["client_id"])
    if not client:
        return _oauth_error("invalid_request", "커넥터 등록이 만료됐습니다. Claude/ChatGPT에서 다시 연결해 주세요.")
    host = urlparse(req["redirect_uri"]).hostname or ""
    name = _clean_name(client.get("client_name"))
    gid = "g_" + crypto.random_token(9)
    store.put_grant(t, uid, gid, req["client_id"], name, host, device.get("invite_hash"))
    code = crypto.random_token(32)
    store.put_code(t, crypto.sha256_hex(code), {"uid": uid, "client_id": req["client_id"], "grant_id": gid,
                                                "redirect_uri": req["redirect_uri"],
                                                "code_challenge": req["code_challenge"]})
    _notify_paired(t, uid, gid, name, host)
    log("approve_granted")
    query = urlencode({"code": code, **({"state": req["state"]} if req.get("state") else {}), "iss": ISSUER})
    sep = "&" if "?" in req["redirect_uri"] else "?"
    return _json({"redirect_to": f"{req['redirect_uri']}{sep}{query}"})


def _notify_paired(t, uid: str, gid: str, client_name: str, redirect_host: str) -> None:
    """Tell the PC agent which app its code just connected, so it can show the student (a leaked
    code shows up there) and name the grant to revoke."""
    conn = store.latest_connection(t, uid)
    if conn:
        try:
            _post_to_connection(conn, {"type": "paired", "client_name": client_name, "redirect_host": redirect_host,
                                       "app": _app_label(redirect_host), "grant_id": gid})
        except Exception:  # noqa: BLE001 - best effort; the grant itself already succeeded
            log("paired_notice_failed")


@mcp.custom_route("/oauth/approve", methods=["POST"])
async def approve(request: Request) -> Response:
    if not _same_site(request):
        log("approve_refused", reason="cross_site")
        return _oauth_error("access_denied", "승인은 연결 페이지에서만 할 수 있습니다.", 403)
    body = await _json_object(request)
    if body is None:
        return _oauth_error("invalid_request", "JSON body required")
    nonce, t = body.get("nonce"), store.table()
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce) or not store.peek_authreq(t, nonce):
        return _oauth_error("invalid_request", "승인 요청이 만료됐습니다. Claude/ChatGPT에서 다시 연결해 주세요.")
    code = _normalize_code(str(body.get("code") or "")[:32])
    uid = store.take_pair(t, crypto.sha256_hex(code)) if len(code) == 8 else None
    device = store.get_device(t, uid) if uid else None
    if not device or not _invite_alive(device):
        return _refuse(t, nonce, "연결 코드가 맞지 않거나 만료됐습니다. PC에서 `kw-mcp code`로 새 코드를 받으세요.", "bad_code")
    return _grant(t, nonce, uid, device)


def _issue_tokens(t, grant: dict) -> dict:
    uid, gid, client_id, expires = grant["uid"], grant["grant_id"], grant["client_id"], int(grant["expires"])
    now = int(time.time())
    exp = min(now + ACCESS_TTL, expires)
    access = crypto.sign_jwt(_jwt_secret(), {"iss": ISSUER, "sub": uid, "aud": RESOURCE, "client_id": client_id,
                                             "gid": gid, "scope": SCOPE, "iat": now, "exp": exp})
    refresh = f"{gid}.{crypto.random_token(32)}"  # the grant id finds its family; the rest is the secret
    store.put_refresh(t, gid, crypto.sha256_hex(refresh), uid, client_id, expires)
    store.touch_grant(t, uid, gid)
    store.touch_client(t, client_id)
    return {"access_token": access, "token_type": "Bearer", "expires_in": exp - now,
            "refresh_token": refresh, "scope": SCOPE}


_BAD_REFRESH = "refresh token is invalid or expired"


def _refresh(t, raw: str, client_id: str) -> Response:
    gid, _, secret = raw.partition(".")
    if not (GRANT_ID.fullmatch(gid) and secret):
        return _refresh_legacy(t, raw, client_id)
    token_hash = crypto.sha256_hex(raw)
    row = store.get_refresh(t, gid, token_hash)
    if not row or row.get("client_id") != client_id:
        return _oauth_error("invalid_grant", _BAD_REFRESH)
    grant = store.get_grant(t, str(row["uid"]), gid)
    if not grant or grant.get("client_id") != client_id or not _invite_alive(grant):
        return _oauth_error("invalid_grant", _BAD_REFRESH)
    used_at = row.get("rotated_at")
    if used_at is None:
        used_at = store.mark_refresh_used(t, gid, token_hash)
    if used_at is not None and time.time() - int(used_at) > REFRESH_GRACE_S:
        # A token already exchanged comes back later: one of its holders isn't the client. End the
        # whole grant; the student approves again with a new code.
        store.revoke_grant(t, str(row["uid"]), gid)
        log("refresh_reuse_revoked")
        return _oauth_error("invalid_grant", _BAD_REFRESH)
    return _json(_issue_tokens(t, grant))  # first use, or a retry within the grace window


def _refresh_legacy(t, raw: str, client_id: str) -> Response:
    """A refresh token from before grants: move it into a new grant, once."""
    old = store.take_legacy_refresh(t, crypto.sha256_hex(raw))
    uid = str((old or {}).get("uid", ""))
    if not old or old.get("client_id") != client_id or not signing.UID.fullmatch(uid):
        return _oauth_error("invalid_grant", _BAD_REFRESH)
    device, client = store.get_device(t, uid), store.get_client(t, client_id)
    if not device or not client or not _invite_alive(device):
        return _oauth_error("invalid_grant", _BAD_REFRESH)
    host = urlparse((client.get("redirect_uris") or [""])[0]).hostname or ""
    grant = store.put_grant(t, uid, "g_" + crypto.random_token(9), client_id, _clean_name(client.get("client_name")),
                            host, device.get("invite_hash"))
    log("legacy_refresh_migrated")
    return _json(_issue_tokens(t, grant))


@mcp.custom_route("/oauth/token", methods=["POST"])
async def token(request: Request) -> Response:
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - any unreadable body is the client's mistake, not a 500
        return _oauth_error("invalid_request", "form-encoded body required")
    grant_type, client_id = form.get("grant_type"), str(form.get("client_id") or "")
    if grant_type not in ("authorization_code", "refresh_token"):
        return _oauth_error("unsupported_grant_type", "authorization_code or refresh_token")
    t = store.table()
    if not CLIENT_ID.fullmatch(client_id) or not store.get_client(t, client_id):
        # RFC 6749 5.2: MCP clients register again when they see this
        return _oauth_error("invalid_client", "unknown or expired client; register again", 401)
    if grant_type == "refresh_token":
        return _refresh(t, str(form.get("refresh_token") or "")[:200], client_id)
    raw = str(form.get("code") or "")[:200]
    code = store.take_code(t, crypto.sha256_hex(raw)) if raw else None
    if not code or code["client_id"] != client_id or code["redirect_uri"] != form.get("redirect_uri"):
        return _oauth_error("invalid_grant", "code is invalid, used, or expired")
    verifier = str(form.get("code_verifier") or "")
    if not CODE_VERIFIER.fullmatch(verifier) or not hmac.compare_digest(
            crypto.pkce_s256(verifier).encode(), str(code["code_challenge"]).encode()):
        return _oauth_error("invalid_grant", "PKCE verification failed")
    grant = store.get_grant(t, str(code["uid"]), str(code.get("grant_id", "")))
    if not grant:
        return _oauth_error("invalid_grant", "this approval was revoked or has expired")
    return _json(_issue_tokens(t, grant))


# --- device endpoints -----------------------------------------------------------------

def _invite_ok(invite: str) -> str | None:
    """The invite's hash if it is on the list."""
    known = json.loads(params.get(INVITES_PARAM, ttl_s=0) or "[]")
    digest = hashlib.sha256(invite.encode()).hexdigest()
    return digest if any(hmac.compare_digest(digest, k) for k in known) else None


@mcp.custom_route("/device/register", methods=["POST"])
async def device_register(request: Request) -> Response:
    body = await _json_object(request)
    if body is None:
        return _json({"error": "bad_request"}, 400)
    invite = body.get("invite")
    invite_hash = _invite_ok(invite) if isinstance(invite, str) and len(invite) <= 200 else None
    if not invite_hash:
        log("device_register_refused")
        return _json({"error": "invalid_invite"}, 403)
    jwk = body.get("public_key")
    try:
        crypto.public_key_from_jwk(jwk)
    except (ValueError, KeyError, TypeError):
        return _json({"error": "invalid_public_key"}, 400)
    uid = "u_" + crypto.random_token(16)
    store.put_device(store.table(), uid, jwk, invite_hash)
    return _json({"uid": uid, "ws_url": WS_URL, "mcp_url": RESOURCE}, 201)


async def _signed_device(request: Request, purpose: str) -> tuple[dict, dict | None, Response | None]:
    """(body, device, None) when the body carries a fresh signature over "kw-<purpose>:<uid>:<ts>",
    else (body, None, refusal). Refusals are listed in signing.py; a stale clock also gets server_time."""
    body = await _json_object(request)
    if body is None:
        return {}, None, _json({"error": "bad_request"}, 400)
    uid, ts = body.get("uid"), body.get("ts")
    try:
        device = signing.check(store.table(), uid, ts, body.get("sig"), (f"kw-{purpose}:{uid}:{ts}",))
    except signing.Refused as e:
        log("device_signature_refused", purpose=purpose.split(":")[0], reason=e.error)
        extra = {"server_time": int(time.time())} if e.status == 401 else {}
        return body, None, _json({"error": e.error, **extra}, e.status)
    return body, device, None


@mcp.custom_route("/device/pair", methods=["POST"])
async def device_pair(request: Request) -> Response:
    body, device, refused = await _signed_device(request, "pair")
    if refused:
        return refused
    if not _invite_alive(device):
        return _json({"error": "invite_revoked"}, 403)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    store.put_pair(store.table(), crypto.sha256_hex(code), body["uid"])
    return _json({"code": f"{code[:4]}-{code[4:]}", "expires_in": store.PAIR_TTL, "mcp_url": RESOURCE})


def _kst(epoch: object) -> str | None:
    return datetime.fromtimestamp(int(epoch), KST).isoformat(timespec="seconds") if epoch else None


@mcp.custom_route("/device/grants", methods=["POST"])
async def device_grants(request: Request) -> Response:
    """The apps this PC's codes connected, newest first. Also a signed check that mints nothing."""
    body, _, refused = await _signed_device(request, "grants")
    if refused:
        return refused
    rows = sorted(store.list_grants(store.table(), body["uid"]), key=lambda g: int(g["created"]), reverse=True)
    return _json({"grants": [{"id": g["grant_id"], "client": g.get("client_name", ""),
                              "redirect_host": g.get("redirect_host", ""), "created": _kst(g["created"]),
                              "last_used": _kst(g.get("last_used")), "expires": _kst(g["expires"])} for g in rows]})


@mcp.custom_route("/device/revoke", methods=["POST"])
async def device_revoke(request: Request) -> Response:
    """End one grant now: its access tokens stop at the next call and its refresh tokens are deleted."""
    gid = (await _json_object(request) or {}).get("grant_id")  # Starlette keeps the body for the next read
    if not isinstance(gid, str) or not GRANT_ID.fullmatch(gid):
        return _json({"error": "bad_request"}, 400)
    body, _, refused = await _signed_device(request, f"revoke:{gid}")  # signs "kw-revoke:<gid>:<uid>:<ts>"
    if refused:
        return refused
    if not store.revoke_grant(store.table(), body["uid"], gid):
        return _json({"error": "unknown_grant"}, 404)
    log("grant_revoked")
    return _json({"ok": True})


@mcp.custom_route("/device/unlink", methods=["POST"])
async def device_unlink(request: Request) -> Response:
    """Always allowed for the device itself (even with its invite withdrawn): removes the key,
    connections, every grant and their refresh tokens."""
    body, _, refused = await _signed_device(request, "unlink")
    if refused:
        return refused
    store.delete_device(store.table(), body["uid"])
    _SNAP.pop(body["uid"], None)
    return _json({"ok": True})


@mcp.custom_route("/device/result", methods=["POST"])
async def device_result(request: Request) -> Response:
    """The agent's encrypted answer. Posting the same body again is fine (202): agents retry."""
    body = await _json_object(request, MAX_RESULT_BYTES)
    request_id, iv, ct = (body or {}).get("request_id"), (body or {}).get("iv"), (body or {}).get("ct")
    if not (isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id) and isinstance(iv, str)
            and IV.fullmatch(iv) and isinstance(ct, str) and 0 < len(ct) <= store.MAX_RESULT_CHARS):
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
        if scope["type"] == "http":
            _sweep_snap()
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

"""Public MCP server + small JSON API, served by uvicorn behind the Lambda Web Adapter.

Routes (everything else is 404):
  GET  /healthz                      readiness check for the adapter
  POST /c/<token>/mcp                MCP Streamable HTTP, stateless, JSON responses
  GET  /c/<token>/api/notices        JSON for the local kw-mcp (latest or ?q= search; ?source= boards)
  GET  /c/<token>/api/menu           cafeteria menu (?date=YYYY-MM-DD, default today KST)
  GET  /c/<token>/api/calendar       academic calendar (?from=&to=, default next 30 days)

<token> is a per-person access token; SSM /kw/mcp/tokens holds only their SHA-256 hashes.
GET/DELETE on the MCP path return 405 so no long-lived SSE stream can hold a Lambda.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import boto3
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from kwcampus import campus, params, public_data
from kwcampus.config import KST, PUBLIC_TABLE

TOKENS_PARAM = "/kw/mcp/tokens"  # String: JSON list of sha256 hex digests
_PATH = re.compile(r"^/c/([A-Za-z0-9_-]{20,80})(/mcp|/api/notices|/api/menu|/api/calendar)/?$")

INSTRUCTIONS = """\
광운대학교 공개 공지(www.kw.ac.kr 메인 공지·취업게시판·종합 FAQ, 학과 게시판)를 조회합니다. 비공식 도구이며 제목·날짜·분류·부서·링크만 있습니다.
- 분류는 일반, 학사, 학생, 봉사, 등록/장학, 입학, 시설, 병무, 외부, 국제교류, 국제학생(메인 공지), 취업(취업게시판), FAQ, 학과 이름(인공지능융합대학, 전자정보공과대학, 전자공학과 등)입니다. "장학금", "채용", "국제", "학과"(모든 학과), 이름 일부("건축")도 받습니다. 없는 분류는 unknown_category 오류와 쓸 수 있는 분류 목록을 돌려줍니다.
- 분류 없이 받은 최신 목록에는 FAQ가 빠집니다. 여러 게시판이 같은 제목으로 올린 공지는 하나로 묶고 나머지 post_id를 also에, 그 게시판을 also_boards에 넣습니다.
- 본문은 없으니 자세한 내용은 url 링크를 안내하세요. 날짜는 한국 시간 기준입니다.
- 학식(함지마루 주간 식단)과 학사일정(학교 학사일정 페이지)도 조회할 수 있습니다.
"""
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

mcp = MCPServer("kw-campus-public", instructions=INSTRUCTIONS, log_level="WARNING")
# The SDK logs every failed tool call at INFO with its arguments' error text. Set explicitly: only
# the first MCPServer's log_level reaches basicConfig, and this module is imported first.
logging.getLogger("mcp").setLevel(logging.WARNING)

# Years of calendar the store keeps around today; asking for more never reads more.
CALENDAR_YEARS_AROUND = 1


def _table():
    return boto3.resource("dynamodb").Table(PUBLIC_TABLE)


class BadDate(ToolError):
    """A date that was given but isn't YYYY-MM-DD (never silently treated as "no date")."""


def _date(value: str | None, name: str = "date") -> date | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
            return date.fromisoformat(v)
    except ValueError:
        pass
    raise BadDate(f"{name}는 YYYY-MM-DD 형식이어야 합니다(예: 2026-11-01).")


@mcp.tool(annotations=READ_ONLY)
def list_notice_categories() -> list[dict[str, Any]]:
    """공지 분류 목록과 분류별로 저장된 공지 수."""
    return public_data.categories(_table())


@mcp.tool(annotations=READ_ONLY)
def latest_notices(category: str | None = None, limit: int = 10, include_pinned: bool = True) -> list[dict[str, Any]]:
    """최신 공지(게시일 역순). category로 분류를 거르고(예: "학사", "장학"), include_pinned=False면 상단 고정 글을 뺍니다."""
    try:
        return public_data.latest(_table(), category, limit, include_pinned)
    except public_data.UnknownCategory as e:  # the model should see which labels exist
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
def search_notices(query: str, category: str | None = None, since: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """제목·부서에서 검색합니다. 띄어쓴 단어가 모두 들어간 공지만 찾습니다. since는 YYYY-MM-DD(그날 이후 게시)."""
    try:
        return public_data.search(_table(), query, category, _date(since, "since"), limit)
    except public_data.UnknownCategory as e:
        raise ToolError(str(e)) from None


@mcp.tool(annotations=READ_ONLY)
def get_notice(post_id: str) -> dict[str, Any]:
    """post_id로 공지 하나를 봅니다(제목·분류·부서·날짜·링크)."""
    return public_data.get(_table(), post_id) or {"error": "not_found", "post_id": post_id}


_WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}


def _resolve_day(value: str | None) -> date:
    today = datetime.now(UTC).astimezone(KST).date()
    v = (value or "").strip()
    if not v or v in ("오늘", "today"):
        return today
    if v in ("내일", "tomorrow"):
        return today + timedelta(days=1)
    if m := re.fullmatch(r"([월화수목금토일])(?:요일)?", v):  # the next such day, today included
        return today + timedelta(days=(_WEEKDAYS[m.group(1)] - today.weekday()) % 7)
    return _date(v, "day")  # not empty here, so a date or BadDate


def _events_between(start: date, end: date, query: str | None = None) -> list[dict[str, Any]]:
    """Events overlapping start..end. One stored item per year, and only years around today are
    read, so a span like 0001-01-01..9999-12-31 costs three reads, not ten thousand."""
    table = _table()
    this_year = datetime.now(UTC).astimezone(KST).year
    events = []
    for year in range(max(start.year, this_year - CALENDAR_YEARS_AROUND),
                      min(end.year, this_year + CALENDAR_YEARS_AROUND) + 1):
        events.extend(campus.calendar_events(table, year))
    terms = [t.casefold() for t in (query or "").split()]
    return [e for e in events
            if e["end"] >= start.isoformat() and e["start"] <= end.isoformat()
            and all(t in e["title"].casefold() for t in terms)]


@mcp.tool(annotations=READ_ONLY)
def cafeteria_menu(day: str | None = None) -> dict[str, Any]:
    """함지마루 학식 메뉴. day는 "오늘"(기본), "내일", "월"~"금"(오늘부터 가장 가까운 그 요일), 또는 YYYY-MM-DD.
    이번 주(또는 저장된 주) 메뉴만 있습니다."""
    target = _resolve_day(day)
    found = campus.menu_for(_table(), target)
    return found or {"date": target.isoformat(), "meals": [], "note": "이 날짜의 식단이 없습니다(주말·공휴일이거나 아직 수집 전)."}


@mcp.tool(annotations=READ_ONLY)
def academic_calendar(start: str | None = None, end: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
    """학사일정. start~end(YYYY-MM-DD, 기본: 오늘부터 30일) 사이에 걸친 일정을 돌려줍니다. query로 제목을 거릅니다(예: "수강신청", "기말")."""
    s = _date(start, "start") or datetime.now(UTC).astimezone(KST).date()
    e = _date(end, "end") or s + timedelta(days=30)
    return _events_between(s, e, query)


# (loaded_at, hashes); None until the first load. monotonic() can be near 0 in a fresh
# Lambda microVM, so "older than 300 s" must not be the only trigger for the first load.
_tokens: tuple[float, frozenset[str]] | None = None


def _token_ok(token: str) -> bool:
    global _tokens
    if _tokens is None or time.monotonic() - _tokens[0] > 300:
        _tokens = (time.monotonic(), frozenset(json.loads(params.get(TOKENS_PARAM, ttl_s=0) or "[]")))
    digest = hashlib.sha256(token.encode()).hexdigest()
    return any(hmac.compare_digest(digest, known) for known in _tokens[1])


_BAD_DATE = {"error": "bad_date", "expected": "YYYY-MM-DD"}


def _campus_api(kind: str, request: Request) -> JSONResponse:
    q = request.query_params
    try:
        if kind == "/api/menu":
            return JSONResponse(cafeteria_menu(q.get("date")))
        return JSONResponse({"events": academic_calendar(q.get("from"), q.get("to"), q.get("q"))})
    except BadDate:
        return JSONResponse(_BAD_DATE, status_code=400)


def _notices_api(request: Request) -> JSONResponse:
    q = request.query_params
    table = _table()
    raw_limit = q.get("limit", "")
    # ASCII digits only: str.isdigit() also passes "²", which int() rejects (a 500 before).
    limit = int(raw_limit) if re.fullmatch(r"[0-9]{1,3}", raw_limit) else 20
    # ?source=kw-notice (repeatable) keeps only those boards, before the limit (campus alerts want the main board)
    sources = tuple(q.getlist("source")) or None
    if sources and not set(sources) <= set(public_data.SOURCES):
        return JSONResponse({"error": "unknown_source", "valid": list(public_data.SOURCES)}, status_code=400)
    try:
        # kw-mcp's my_notices asks for every department board and keeps the student's own on the PC,
        # so those boards get the larger cap (the department never comes here).
        cap = public_data.DEPARTMENT_LIMIT if public_data.only_departments(q.get("category")) else public_data.MAX_LIMIT
        if q.get("q"):
            rows = public_data.search(table, q["q"], q.get("category"), _date(q.get("since"), "since"), limit,
                                      sources=sources, max_limit=cap)
        else:  # `since` must hold here too: kw-mcp's "new notices" ask without a query
            rows = public_data.latest(table, q.get("category"), limit, q.get("pinned", "true") != "false",
                                      _date(q.get("since"), "since"), sources=sources, max_limit=cap)
    except public_data.UnknownCategory as e:
        return JSONResponse(e.as_dict(), status_code=400)
    except BadDate:
        return JSONResponse(_BAD_DATE, status_code=400)
    # max_limit: the cap this answer was cut at, so kw-mcp says "older posts may be missing" only when
    # the rows actually reached it (it can't tell 50 posts from a cut at 50 otherwise).
    return JSONResponse({"notices": rows, "max_limit": cap, "source": "광운대 공개 공지(메인·학과·취업 게시판)",
                         "unofficial": True})


def build_app():
    """A fresh ASGI app. The MCP session manager can start only once per instance, so tests
    build one per client; uvicorn imports the module-level ``app`` once per container."""
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        # Behind API Gateway the Host header is the execute-api domain; access is gated by the path token.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await inner(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]
        if path == "/healthz":
            await PlainTextResponse("ok")(scope, receive, send)
            return
        match = _PATH.match(path)
        if not match or not _token_ok(match.group(1)):
            await PlainTextResponse("not found", status_code=404)(scope, receive, send)
            return
        if match.group(2) == "/mcp":
            if method != "POST":
                await PlainTextResponse("method not allowed", status_code=405, headers={"Allow": "POST"})(scope, receive, send)
                return
            await inner({**scope, "path": "/mcp", "raw_path": b"/mcp"}, receive, send)
            return
        if method != "GET":
            await PlainTextResponse("method not allowed", status_code=405, headers={"Allow": "GET"})(scope, receive, send)
            return
        request = Request(scope, receive)
        response = _notices_api(request) if match.group(2) == "/api/notices" else _campus_api(match.group(2), request)
        await response(scope, receive, send)

    return app


app = build_app()


if os.environ.get("KW_LOCAL_TOKEN"):  # local runs only: accept one token without SSM
    _tokens = (float("inf"), frozenset({hashlib.sha256(os.environ["KW_LOCAL_TOKEN"].encode()).hexdigest()}))

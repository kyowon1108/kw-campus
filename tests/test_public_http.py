import hashlib
import json

import pytest
from starlette.testclient import TestClient

from kwcampus import public_data, public_http

TOKEN = "friend-token-0123456789abcdef"
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client(aws, monkeypatch):
    aws["ssm"].put_parameter(Name="/kw/mcp/tokens", Type="String",
                             Value=json.dumps([hashlib.sha256(TOKEN.encode()).hexdigest()]))
    monkeypatch.setattr(public_http, "_tokens", None)
    public_data.clear_cache()
    table = aws["public"]
    for i, (cat, title, posted) in enumerate([
        ("academic", "2026학년도 동계 계절학기 수강신청 안내", "2026-11-04"),
        ("registration-scholarship", "국가장학금 2차 신청 안내", "2026-11-03"),
        ("general", "도서관 휴관 안내", "2026-10-30"),
    ]):
        table.put_item(Item={"PK": "SRC#kw-notice", "SK": f"N#{100 + i}", "post_id": str(100 + i), "title": title,
                             "category_id": cat, "department": "교육지원팀", "posted_date": posted,
                             "updated_date": posted, "pinned": False, "seed": True, "content_hash": "x",
                             "url": f"https://www.kw.ac.kr/ko/life/notice.jsp?BoardMode=view&DUID={100 + i}"})
    with TestClient(public_http.build_app()) as c:
        yield c


def rpc(client, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}
    r = client.post(f"/c/{TOKEN}/mcp", json=body, headers={**H, "MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_access_rules(client):
    assert client.get("/healthz").status_code == 200
    assert client.post("/c/not-a-real-token-123456789/mcp", json={}).status_code == 404
    assert client.get(f"/c/{TOKEN}/mcp").status_code == 405
    assert client.delete(f"/c/{TOKEN}/mcp").status_code == 405
    assert client.get("/mcp").status_code == 404


def test_mcp_tools_list_and_search(client):
    init = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "1"}})
    assert "광운대학교" in init["instructions"]
    names = {t["name"] for t in rpc(client, "tools/list")["tools"]}
    assert names == {"list_notice_categories", "latest_notices", "search_notices", "get_notice",
                     "cafeteria_menu", "academic_calendar"}
    result = rpc(client, "tools/call", {"name": "search_notices", "arguments": {"query": "장학금", "category": "장학"}})
    rows = result["structuredContent"]["result"]
    assert [r["title"] for r in rows] == ["국가장학금 2차 신청 안내"]
    assert set(rows[0]) == {"post_id", "title", "category", "category_id", "department",
                            "posted_date", "updated_date", "pinned", "url"}  # no seed/hash/ttl


def test_json_api_for_local_kw_mcp(client):
    data = client.get(f"/c/{TOKEN}/api/notices?category=학사&limit=5").json()
    assert [n["post_id"] for n in data["notices"]] == ["100"]
    assert client.get(f"/c/{TOKEN}/api/menu?date=2026-11-05").json()["meals"] == []
    assert client.get(f"/c/{TOKEN}/api/calendar?from=2026-11-01&to=2026-11-30").json() == {"events": []}


def test_calendar_reads_only_the_years_around_today(client, monkeypatch):
    from datetime import UTC, datetime

    from kwcampus import campus

    years = []
    monkeypatch.setattr(campus, "calendar_events", lambda table, year: years.append(year) or [
        {"title": f"{year} 개강", "start": f"{year}-03-02", "end": f"{year}-03-02"}])
    this_year = datetime.now(UTC).astimezone(public_http.KST).year
    events = public_http.academic_calendar("0001-01-01", "9999-12-31")
    assert years == [this_year - 1, this_year, this_year + 1] and len(events) == 3
    years.clear()
    assert client.get(f"/c/{TOKEN}/api/calendar?from=0001-01-01&to=9999-12-31").status_code == 200
    assert len(years) == 3
    years.clear()
    assert public_http.academic_calendar("1990-01-01", "1990-12-31") == [] and years == []


def test_malformed_dates_and_limits_are_errors_not_silent_defaults(client):
    for bad in ("2026/11/01", "어제", "2026-13-40"):
        with pytest.raises(public_http.BadDate, match="YYYY-MM-DD"):
            public_http.search_notices("", None, bad)
        with pytest.raises(public_http.BadDate):
            public_http.academic_calendar(bad)
    with pytest.raises(public_http.BadDate):
        public_http.cafeteria_menu("다음 주 월요일")
    assert client.get(f"/c/{TOKEN}/api/notices?since=2026/11/01").json() == {"error": "bad_date", "expected": "YYYY-MM-DD"}
    assert client.get(f"/c/{TOKEN}/api/menu?date=nope").status_code == 400
    assert client.get(f"/c/{TOKEN}/api/calendar?from=x").status_code == 400
    for limit in ("%C2%B2", "9" * 40, "-1", ""):  # '²' passed isdigit() and crashed int()
        assert client.get(f"/c/{TOKEN}/api/notices?limit={limit}").status_code == 200
    result = rpc(client, "tools/call", {"name": "search_notices", "arguments": {"query": "", "since": "11/01"}})
    assert result["isError"] and "YYYY-MM-DD" in result["content"][0]["text"]


def test_json_api_filters_by_source_before_the_limit(client, aws):
    for i in range(30):
        aws["public"].put_item(Item={"PK": "SRC#dept-ee", "SK": f"N#{i}", "post_id": str(i), "source": "dept-ee",
                                     "title": f"학과 공지 {i}", "category_id": "dept-ee", "department": "",
                                     "posted_date": "2026-11-05", "updated_date": "2026-11-05", "pinned": False,
                                     "url": "u"})
    public_data.clear_cache()
    main = client.get(f"/c/{TOKEN}/api/notices?source=kw-notice&limit=3").json()["notices"]
    assert [n["post_id"] for n in main] == ["100", "101", "102"]
    assert client.get(f"/c/{TOKEN}/api/notices?source=nope").json()["error"] == "unknown_source"


def test_weekday_names_mean_the_next_such_day(monkeypatch):
    from datetime import date, datetime

    class Sunday(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 8, 10, 0, tzinfo=tz)

    monkeypatch.setattr(public_http, "datetime", Sunday)
    assert public_http._resolve_day("월") == date(2026, 11, 9)
    assert public_http._resolve_day("일요일") == date(2026, 11, 8)
    assert public_http._resolve_day("내일") == date(2026, 11, 9)


def test_tool_failures_are_not_logged_at_info():
    import logging

    assert logging.getLogger("mcp").level == logging.WARNING
    assert not logging.getLogger("mcp.server.mcpserver.server").isEnabledFor(logging.INFO)


def test_tokens_load_in_a_fresh_container_where_monotonic_is_near_zero(aws, monkeypatch):
    aws["ssm"].put_parameter(Name="/kw/mcp/tokens", Type="String",
                             Value=json.dumps([hashlib.sha256(TOKEN.encode()).hexdigest()]))
    monkeypatch.setattr(public_http, "_tokens", None)
    monkeypatch.setattr(public_http.time, "monotonic", lambda: 1.0)
    assert public_http._token_ok(TOKEN)


def test_unknown_category_is_an_error_with_the_valid_labels(client):
    r = client.get(f"/c/{TOKEN}/api/notices?category=급식")
    assert r.status_code == 400 and r.json()["error"] == "unknown_category" and "학사" in r.json()["valid"]
    assert [n["post_id"] for n in client.get(f"/c/{TOKEN}/api/notices?category=장학금").json()["notices"]] == ["101"]
    result = rpc(client, "tools/call", {"name": "latest_notices", "arguments": {"category": "급식"}})
    assert result["isError"] is True and "unknown_category" in result["content"][0]["text"]

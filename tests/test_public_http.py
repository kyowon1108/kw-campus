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


def test_tokens_load_in_a_fresh_container_where_monotonic_is_near_zero(aws, monkeypatch):
    aws["ssm"].put_parameter(Name="/kw/mcp/tokens", Type="String",
                             Value=json.dumps([hashlib.sha256(TOKEN.encode()).hexdigest()]))
    monkeypatch.setattr(public_http, "_tokens", None)
    monkeypatch.setattr(public_http.time, "monotonic", lambda: 1.0)
    assert public_http._token_ok(TOKEN)

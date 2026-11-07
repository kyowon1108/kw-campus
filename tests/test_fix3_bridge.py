"""1.0 review fixes in the public data and the bridge: cross-post folding, the student's own boards
in my_notices, holidays, agent outcome counters, update hints, the per-invite PC cap.

Made-up titles, departments and ids only; no KLAS, school servers or AWS (moto)."""

import asyncio
import copy
import json
import time
from datetime import date, datetime, timedelta

import httpx
import pytest
import test_bridge
from kw_mcp.service import KlasService
from mcp.server.mcpserver.exceptions import ToolError
from test_bridge import (
    INVITE,
    SNAPSHOT,
    FakeDevice,
    mcp_call,
    oauth,
    register,
    signed,
    tool,
)

from kwcampus import public_data
from kwcampus.bridge import app as bridge
from kwcampus.bridge import crypto, shapes, store
from kwcampus.config import KST
from kwcampus.routing import CATEGORY_LABEL

env = test_bridge.env


def put(table, source, post_id, title, category, posted="2026-11-05", department=None, **extra):
    table.put_item(Item={"PK": f"SRC#{source}", "SK": f"N#{post_id}", "source": source, "post_id": str(post_id),
                         "title": title, "category_id": category,
                         "department": CATEGORY_LABEL.get(category, "") if department is None else department,
                         "posted_date": posted, "updated_date": posted, "pinned": False, "first_seen": 1,
                         "url": f"https://example.kw.ac.kr/{source}/{post_id}", **extra})


def ids(rows):
    return [r["post_id"] for r in rows]


@pytest.fixture
def table(aws):
    public_data.clear_cache()
    yield aws["public"]
    public_data.clear_cache()


# --- B5: what folds ------------------------------------------------------------------------------

def test_only_the_very_same_title_from_other_boards_folds(table):
    put(table, "dept-ee", 1, "2026 취업박람회 안내 (학부)", "dept-ee", "2026-11-05")
    put(table, "dept-law", 2, "2026 취업박람회 안내 (대학원)", "dept-law", "2026-11-05")
    put(table, "dept-ee", 3, "[1차] 졸업사정 안내", "dept-ee", "2026-11-04")
    put(table, "dept-law", 4, "[2차] 졸업사정 안내", "dept-law", "2026-11-04")
    put(table, "dept-ee", 5, "[학부공지] 장학 안내", "dept-ee", "2026-11-03")
    put(table, "dept-law", 6, "[대학원] 장학 안내", "dept-law", "2026-11-03")
    rows = public_data.latest(table, "학과", 50)
    assert sorted(ids(rows)) == [f"{s}-{n}" for s, n in (("dept-ee", 1), ("dept-ee", 3), ("dept-ee", 5),
                                                          ("dept-law", 2), ("dept-law", 4), ("dept-law", 6))]
    assert not any("also" in r for r in rows)


def test_masked_titles_and_one_boards_own_posts_never_fold(table):
    # Two professors' talks: the names are masked, so the titles look alike but are not one notice.
    put(table, "dept-ee", 1, "[REDACTED_NAME] 교수 초청 특강", "dept-ee", "2026-11-05")
    put(table, "dept-law", 2, "[REDACTED_NAME] 교수 초청 특강", "dept-law", "2026-11-05")
    # aicon posts one title per school: one source, so separate notices.
    for n, school in enumerate(("컴퓨터정보공학부", "소프트웨어학부"), start=3):
        put(table, "aicon", n, "2학기 졸업작품 발표회 안내", "dept-aicon", "2026-11-04", department=school)
    rows = public_data.latest(table, "학과", 50)
    assert len(rows) == 4 and not any("also" in r for r in rows)


def test_a_copy_on_my_board_survives_the_fold(table):
    """The fixture of the review: the same job-fair title on the job board and two department boards."""
    from kw_mcp.combined import department_notices

    title = "[대학혁신지원사업] 2026 광운대학교 취업박람회 개최 안내"
    put(table, "kw-job", 53606, title, "job", "2026-11-04")
    put(table, "dept-ee", 646, title, "dept-ee", "2026-11-04")
    put(table, "dept-law", 519, title, "dept-law", "2026-11-03")
    rows = public_data.latest(table, "학과", 50)
    assert ids(rows) == ["dept-ee-646"] and rows[0]["also"] == ["dept-law-519"]
    law = department_notices(rows, "법학부", "정책법학대학")
    assert [(r["post_id"], r["category"], r["url"]) for r in law] == [
        ("dept-law-519", "법학부", "https://example.kw.ac.kr/dept-law/519")]
    assert ids(department_notices(rows, "전자공학과", "전자정보공과대학")) == ["dept-ee-646"]
    assert department_notices(rows, "행정학과", "정책법학대학") == []


def test_a_college_notice_reposted_by_one_department_reaches_the_others(table):
    from kw_mcp.combined import department_notices

    put(table, "dept-ee", 700, "2026-2 전자정보공과대학 캡스톤 발표회", "dept-ee", "2026-11-05")
    put(table, "dept-ei", 300, "2026-2 전자정보공과대학 캡스톤 발표회", "dept-ei", "2026-11-04")
    rows = public_data.latest(table, "학과", 50)
    assert ids(rows) == ["dept-ee-700"]
    assert ids(department_notices(rows, "전기공학과", "전자정보공과대학")) == ["dept-ei-300"]


# --- S56: category words --------------------------------------------------------------------------

def test_category_parts_match_at_word_starts_only():
    r = public_data.resolve_category
    assert r("건축") == {"dept-archi", "dept-architecture"} and r("학사공지") == {"academic"}
    assert r("전자공학과 공지") == {"dept-ee"} and r("등록") == {"registration-scholarship"}
    assert r("인문") == {"dept-chss"} and r("스포츠") == {"dept-kwsports", "dept-sports"}
    for wrong in ("공지", "공과대학", "학생 장학금", "공학"):
        with pytest.raises(public_data.UnknownCategory):
            r(wrong)


# --- S09: the cap for department boards ------------------------------------------------------------

def test_the_json_api_gives_department_boards_the_larger_cap(table, monkeypatch):
    from starlette.testclient import TestClient

    from kwcampus import public_http

    for n in range(public_data.DEPARTMENT_LIMIT + 20):
        put(table, "dept-ee", 1000 + n, f"전자공학과 공지 {n}", "dept-ee", first_seen=n)
        put(table, "kw-notice", 1000 + n, f"메인 공지 {n}", "general", first_seen=n)
    monkeypatch.setattr(public_http, "_token_ok", lambda token: True)
    monkeypatch.setattr(public_http, "_table", lambda: table)
    with TestClient(public_http.build_app()) as c:
        base = "/c/" + "t" * 24 + "/api/notices"
        dept = c.get(base, params={"category": "학과", "limit": 999}).json()
        main = c.get(base, params={"category": "일반", "limit": 999}).json()
        anything = c.get(base, params={"limit": 999}).json()
        searched = c.get(base, params={"category": "학과", "q": "공지", "limit": 999}).json()
    assert len(dept["notices"]) == public_data.DEPARTMENT_LIMIT
    assert len(main["notices"]) == len(anything["notices"]) == public_data.MAX_LIMIT
    # The cap each answer was cut at, for kw-mcp's "older posts may be missing"
    assert dept["max_limit"] == searched["max_limit"] == public_data.DEPARTMENT_LIMIT
    assert main["max_limit"] == anything["max_limit"] == public_data.MAX_LIMIT


@pytest.mark.parametrize("count, truncated", [(public_data.MAX_LIMIT, False), (public_data.DEPARTMENT_LIMIT, True)])
def test_the_extension_reads_the_cap_from_this_server(table, monkeypatch, count, truncated):
    """kw-mcp's my_notices against this JSON API: 50 department posts under the cap of 200 are all
    there were, not a cut (the 1.0 review's false "older posts may be missing")."""
    from kw_mcp import combined
    from kw_mcp.public_api import PublicApi

    from kwcampus import public_http

    for n in range(count):
        put(table, "dept-ee", 3000 + n, f"전자공학과 공지 {n}", "dept-ee", first_seen=n)
    monkeypatch.setattr(public_http, "_token_ok", lambda token: True)
    monkeypatch.setattr(public_http, "_table", lambda: table)

    async def read():
        api = PublicApi("http://testserver/c/" + "t" * 24, transport=httpx.ASGITransport(app=public_http.build_app()))
        try:
            return await combined.department_rows(api, "2026-10-01")
        finally:
            await api.aclose()

    rows, cap = asyncio.run(read())
    assert len(rows) == count and cap == public_data.DEPARTMENT_LIMIT
    scope = combined.department_section(rows, "전자공학과", cap)["department_scope"]
    assert scope.get("truncated", False) is truncated


def test_the_extension_lists_the_boards_kwcampus_collects():
    from kw_mcp import combined

    # 자율전공학부's board is collected ahead of the released extension (kw-mcp 1.1.0): its rows reach my_notices
    # (a row whose category is my department is mine), only the scope note still calls it uncollected. Add it
    # to kw-mcp's DEPARTMENT_BOARDS in the next release, then drop it here.
    ahead: set[str] = set()  # boards collected ahead of the released extension; empty since kw-mcp 1.2
    assert combined.DEPARTMENT_BOARDS == {CATEGORY_LABEL[cid] for cid in public_data.DEPARTMENT_IDS} - ahead
    assert not ahead & combined.DEPARTMENT_BOARDS
    assert combined.DEPARTMENT_LIMIT == public_data.DEPARTMENT_LIMIT and combined.LEGACY_LIMIT == public_data.MAX_LIMIT


# --- my_notices on the bridge ------------------------------------------------------------------------

def _connected(env, version=2):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1", version)
    _, tokens = oauth(c, device)
    return c, device, tokens["access_token"]


def test_my_notices_reads_my_boards_before_the_limit(env, monkeypatch):
    c, _, token = _connected(env)
    monkeypatch.setattr(bridge, "_now", lambda: datetime(2026, 11, 6, 9, 0, tzinfo=KST))
    pub = env["public"]
    put(pub, "dept-law", 519, "[대학혁신지원사업] 2026 취업박람회 개최 안내", "dept-law", "2026-11-02")
    put(pub, "dept-ee", 646, "[대학혁신지원사업] 2026 취업박람회 개최 안내", "dept-ee", "2026-11-03")
    for n in range(80):  # newer posts on other boards: 50 rows of "every board" would end before mine
        put(pub, "dept-chss", 100 + n, f"인사대 공지 {n}", "dept-chss", "2026-11-05", first_seen=n)
    public_data.clear_cache()
    env["answers"]["department"] = {"result": "법학부"}
    out = tool(c, token, "my_notices")["structuredContent"]
    assert [(n["post_id"], n["category"]) for n in out["department"]] == [("dept-law-519", "법학부")]
    scope = out["department_scope"]
    assert scope["boards"] == ["법학부"] and "truncated" not in scope
    assert "단과대(정책법학대학) 게시판은 아직 모으지 않아" in scope["note"]


def test_my_notices_says_how_far_back_a_full_answer_reached(env, monkeypatch):
    c, _, token = _connected(env)
    monkeypatch.setattr(bridge, "_now", lambda: datetime(2026, 11, 6, 9, 0, tzinfo=KST))
    start = date(2026, 9, 1)
    for n in range(public_data.DEPARTMENT_LIMIT + 5):
        put(env["public"], "dept-ee", 2000 + n, f"전자공학과 공지 {n}", "dept-ee",
            (start + timedelta(days=n // 3)).isoformat(), first_seen=n)
    public_data.clear_cache()
    env["answers"]["department"] = {"result": "전자공학과"}
    out = tool(c, token, "my_notices", days=90)["structuredContent"]
    scope = out["department_scope"]
    assert len(out["department"]) == public_data.DEPARTMENT_LIMIT and scope["truncated"] is True
    assert scope["covered_from"] == min(n["posted_date"] for n in out["department"])
    assert f"{scope['covered_from']} 이후 글만 확인했습니다" in scope["note"]


def test_search_notices_by_a_department_without_a_board(env):
    """정보융합학부 posts on the 인공지능융합대학 board (department set). The connector's search_notices and
    latest_notices take the department's name: its posts and the college-wide ones, and of a notice posted
    for every school (aicon-3522/3523/3524/3526) only its own copy, never another school's."""
    c, _, token = _connected(env)
    table, title = env["public"], "2026-2 졸업작품 발표회 안내"
    for no, school in ((3522, "컴퓨터정보공학부"), (3523, "소프트웨어학부"), (3524, "정보융합학부"), (3526, "로봇학부")):
        put(table, "aicon", no, title, "dept-aicon", "2026-11-04", department=school)
    put(table, "aicon", 3519, title, "dept-aicon", "2026-11-03", department="인공지능융합대학")  # also college-wide
    put(table, "aicon", 3510, "2026-2 학생회 선거 안내", "dept-aicon", "2026-11-02", department="인공지능융합대학")
    put(table, "aicon", 3509, "로봇학부 캡스톤 안내", "dept-aicon", "2026-11-02", department="로봇학부")
    put(table, "dept-ee", 700, "전자공학과 졸업작품 발표회 안내", "dept-ee", "2026-11-04")
    public_data.clear_cache()

    found = tool(c, token, "search_notices", query="", category="정보융합학부")["structuredContent"]["result"]
    assert ids(found) == ["aicon-3524", "aicon-3510"]
    assert [r["department"] for r in found] == ["정보융합학부", "인공지능융합대학"]
    assert [r["category"] for r in found] == ["인공지능융합대학"] * 2
    words = tool(c, token, "search_notices", query="졸업작품", category="정보 융합 학부")["structuredContent"]["result"]
    assert ids(words) == ["aicon-3524"]
    latest = tool(c, token, "latest_notices", category="정보융합학부")["structuredContent"]["result"]
    assert ids(latest) == ["aicon-3524", "aicon-3510"]
    robots = tool(c, token, "search_notices", query="졸업작품", category="로봇학부")["structuredContent"]["result"]
    assert ids(robots) == ["aicon-3526"]
    # the college itself still gives every school's copy: one board posting one title per school is not folded
    college = tool(c, token, "search_notices", query="졸업작품", category="인공지능융합대학")["structuredContent"]["result"]
    assert sorted(ids(college)) == ["aicon-3519", "aicon-3522", "aicon-3523", "aicon-3524", "aicon-3526"]


def test_my_notices_names_a_department_without_a_board(env, monkeypatch):
    c, _, token = _connected(env)
    monkeypatch.setattr(bridge, "_now", lambda: datetime(2026, 11, 6, 9, 0, tzinfo=KST))
    put(env["public"], "aicon", 1, "컴정 졸업작품 안내", "dept-aicon", department="컴퓨터정보공학부")
    public_data.clear_cache()
    env["answers"]["department"] = {"result": "컴퓨터정보공학부"}
    scope = tool(c, token, "my_notices")["structuredContent"]["department_scope"]
    assert scope["boards"] == ["인공지능융합대학"] and "내 학과 게시판은 따로 모으지 않아" in scope["note"]
    env["answers"]["department"] = {"result": "국제통상학부"}  # 경영대학's board is collected
    assert "경영대학 게시판의 글만" in tool(c, token, "my_notices")["structuredContent"]["department_scope"]["note"]
    env["answers"]["department"] = {"result": "없는학과"}
    out = tool(c, token, "my_notices")["structuredContent"]
    assert out["department"] == [] and "확인하지 못했습니다" in out["department_scope"]["note"]


def _inflight(env, uid):
    item = env["table"].get_item(Key={"PK": f"INFLIGHT#{uid}", "SK": "META"}).get("Item")
    return int(item["n"]) if item else 0


def test_my_notices_waits_for_a_slow_department_after_the_snapshot_fails(env, monkeypatch):
    c, device, token = _connected(env)
    env["snapshots"][device.uid] = {"error": "klas_error", "message": "KLAS 점검 중입니다. 잠시 뒤 다시 시도하세요."}
    env["answers"]["department"] = {"result": "전자공학과"}
    fast = bridge._post_to_connection

    def slow_department(conn, message):
        if message.get("op") == "department":
            time.sleep(0.6)  # the snapshot has failed long before this answers
        return fast(conn, message)

    monkeypatch.setattr(bridge, "_post_to_connection", slow_department)
    out = tool(c, token, "my_notices")
    assert not out.get("isError")
    result = out["structuredContent"]
    assert result["incomplete"] == {"missing": ["course_posts"], "message": "KLAS 점검 중입니다. 잠시 뒤 다시 시도하세요."}
    assert result["course_posts"] == [] and result["department_scope"]["department"] == "전자공학과"
    assert result["data_as_of"] is None
    assert _inflight(env, device.uid) == 0  # nothing left polling or holding a slot


def test_my_notices_still_answers_when_the_pc_is_busy(env):
    c, device, token = _connected(env)
    env["answers"]["department"] = {"result": "전자공학과"}
    for _ in range(bridge.MAX_INFLIGHT - 1):  # a parallel today_briefing holds two slots
        assert store.open_request(env["table"], device.uid, bridge.MAX_INFLIGHT)
    out = tool(c, token, "my_notices")
    assert not out.get("isError")
    result = out["structuredContent"]
    assert "incomplete" in result or "department_note" in result  # one of the two got the last slot
    assert _inflight(env, device.uid) == bridge.MAX_INFLIGHT - 1


# --- S38: holidays, the same as the extension --------------------------------------------------------

def _monday_snapshot():
    snap = copy.deepcopy(SNAPSHOT)
    snap["semester"] = "2027,1"
    snap["courses"][0]["lctrumSchdulInfo"] = "월 3교시/새빛101"
    return snap


class _Svc:
    """What kw_mcp.combined.today_briefing asks of a KlasService, with the bridge fixture's course."""

    def __init__(self, now):
        self.now = now

    def _now(self):
        return self.now

    async def current_semester(self):
        return "2027,1"

    async def courses(self):
        return [{"course": "데이터마이닝", "professor": "김교수", "schedule": "월 3교시/새빛101"}]

    async def deadlines(self, days):
        return {"items": []}

    async def notices(self):
        return []


def _extension_briefing(now):
    from kw_mcp import combined
    from kw_mcp.public_api import PublicApi

    def empty(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/menu"):
            return httpx.Response(200, json={"meals": []})
        return httpx.Response(200, json={"notices": [], "events": []})

    api = PublicApi("https://api.example/c/token", transport=httpx.MockTransport(empty))
    return asyncio.run(combined.today_briefing(_Svc(now), api))


@pytest.mark.parametrize("day", [datetime(2027, 3, 1, 9, 0, tzinfo=KST), datetime(2027, 3, 8, 9, 0, tzinfo=KST),
                                 datetime(2028, 3, 6, 9, 0, tzinfo=KST)])
def test_briefing_holidays_match_the_extension(env, monkeypatch, day):
    c, device, token = _connected(env, version=1)  # snapshot only: deadlines come from it
    env["snapshots"][device.uid] = _monday_snapshot()
    monkeypatch.setattr(bridge, "_now", lambda: day)
    monkeypatch.setattr(bridge, "term_in_session", lambda semester, now: True)
    monkeypatch.setattr("kw_mcp.combined.term_in_session", lambda semester, now: True)
    bridged = tool(c, token, "today_briefing")["structuredContent"]
    local = _extension_briefing(day)
    assert bridged["classes"] == local["classes"] and bridged.get("classes_note") == local.get("classes_note")
    assert bridged.get("campus_alerts") == local.get("campus_alerts")
    assert bridged.get("holiday_check") == local.get("holiday_check")
    if day.date() == date(2027, 3, 1):  # 삼일절, a Monday: the class is there, marked as a day off
        assert bridged["classes"][0]["holiday"] == "삼일절" and "공휴일(삼일절)" in bridged["classes_note"]
        assert bridged["campus_alerts"]["today"][0] == {"start": "2027-03-01", "end": "2027-03-01", "title": "삼일절",
                                                        "source": "법정 공휴일"}
    elif day.year == 2028:
        assert bridged["holiday_check"] == "2028년 공휴일 정보 없음"
    else:
        assert "holiday" not in bridged["classes"][0] and "classes_note" not in bridged


def test_briefing_without_a_snapshot_still_names_the_holiday(env, monkeypatch):
    c, device, token = _connected(env)
    env["snapshots"][device.uid] = {"error": "klas_error"}
    env["answers"]["deadlines"] = {"result": {"now": "2027-12-27 09:00", "items": []}}
    monkeypatch.setattr(bridge, "_now", lambda: datetime(2027, 12, 27, 9, 0, tzinfo=KST))
    out = tool(c, token, "today_briefing")["structuredContent"]
    assert "공휴일(대체공휴일(성탄절))" in out["classes_note"] and out["classes"] == []
    assert out["data_as_of"] == "2027-12-27T09:00:00+09:00"


# --- S45: outcome counters ----------------------------------------------------------------------------

def _metric_names(captured: str) -> list[str]:
    names = []
    for line in captured.splitlines():
        if line.startswith("{") and '"_aws"' in line:
            doc = json.loads(line)
            names += [m["Name"] for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
            assert doc["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "KwCampus"
            assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]
    return names


def test_agent_outcomes_are_counted_without_data(env, monkeypatch, capsys):
    c, _, token = _connected(env)
    capsys.readouterr()
    tool(c, token, "my_grades")
    env["answers"]["grades"] = {"error": "klas_error", "message": "KLAS 점검 중"}
    tool(c, token, "my_grades")
    env["answers"]["grades"] = {"error": "klas_blocked"}
    tool(c, token, "my_grades")
    env["answers"]["grades"] = {"error": "klas_login_required"}
    tool(c, token, "my_grades")
    names = _metric_names(capsys.readouterr().out)
    assert names == ["AgentOutcomeOk", "AgentOutcomeKlasError", "AgentOutcomeKlasBlocked", "AgentOutcomeLoginRequired"]


def test_timeouts_unreadable_answers_and_crashes_are_counted(env, monkeypatch, capsys):
    c, _, token = _connected(env)
    monkeypatch.setattr(bridge, "SNAPSHOT_WAIT_S", 0.3)
    monkeypatch.setattr(bridge, "_post_to_connection", lambda conn, message: True)  # never answers
    capsys.readouterr()
    out = tool(c, token, "my_grades")
    assert out["isError"] and "제때 응답하지" in out["content"][0]["text"]

    def garbage(conn, message):
        store.put_result(env["table"], message["request_id"], crypto.b64u(b"\0" * 12), crypto.b64u(b"x" * 32))
        return True

    monkeypatch.setattr(bridge, "_post_to_connection", garbage)
    assert tool(c, token, "my_grades")["isError"]

    async def boom():
        raise RuntimeError("a bug")

    monkeypatch.setattr(bridge, "_snapshot", boom)
    assert tool(c, token, "my_courses")["isError"]
    assert _metric_names(capsys.readouterr().out) == ["AgentOutcomeTimeout", "AgentOutcomeDecryptFail",
                                                      "AgentOutcomeUnexpected"]


# --- S33: update hints (every version: reinstall the extension) ------------------------------------

def test_update_hint_points_at_the_extension_for_every_agent_version(env, monkeypatch):
    c, device, token = _connected(env, version=3)
    env["answers"]["grades"] = {"error": "unsupported"}
    text = tool(c, token, "my_grades")["content"][0]["text"]
    assert "옛 버전이라 이 기능을 못 써요" in text and "KW CAMPUS 확장을 다시 설치" in text
    assert "kw-mcp" not in text and "/setup" not in text  # no terminal commands for students
    env["answers"]["assignment_detail"] = {"error": "not_found", "message": "그런 과제가 없습니다."}
    picked = tool(c, token, "my_assignment_detail", course="데이터", title="HW3", id="7")["content"][0]["text"]
    assert picked.endswith(": 그런 과제가 없습니다.")  # a 1.0 agent knows ids: no update hint on top

    store.remove_connection(env["table"], "conn-1")
    store.add_connection(env["table"], device.uid, "conn-2", 2)  # the PC went back to 0.9.0
    assert tool(c, token, "my_grades")["content"][0]["text"] == text
    picked = tool(c, token, "my_assignment_detail", course="데이터", title="HW3", id="7")["content"][0]["text"]
    assert "그런 과제가 없습니다. 1.0보다 옛 PC 에이전트는 id·file_index를 모르고" in picked
    assert "KW CAMPUS 확장을 다시 설치" in picked and "kw-mcp" not in picked

    store.remove_connection(env["table"], "conn-2")
    store.add_connection(env["table"], device.uid, "conn-3", 1)  # before calls
    text = tool(c, token, "my_grades")["content"][0]["text"]
    assert "옛 버전" in text and "KW CAMPUS 확장을 다시 설치" in text and "kw-mcp" not in text


def test_raise_for_unsupported_gives_the_extension_hint():
    with pytest.raises(ToolError, match="KW CAMPUS 확장을 다시 설치"):
        bridge._raise_for({"error": "unsupported"})
    with pytest.raises(ToolError, match="KLAS에서 데이터를 읽지"):
        bridge._raise_for({"error": ["not", "a", "code"]})


# --- S44: PCs per invite ----------------------------------------------------------------------------

def test_one_invite_registers_a_few_pcs_and_an_unlinked_one_frees_its_place(env):
    c = env["client"]
    devices = [register(c, FakeDevice()) for _ in range(bridge.MAX_DEVICES_PER_INVITE)]
    extra = FakeDevice()
    refused = c.post("/device/register", json={"invite": INVITE, "public_key": extra.jwk})
    assert refused.status_code == 409 and refused.json()["error"] == "too_many_devices"
    message = refused.json()["message"]
    assert message == ("이 초대 코드로 연결한 PC가 이미 5대예요. 안 쓰는 PC에서 KW CAMPUS 확장을 지우거나, "
                       "디스코드 #설치-안내에서 새 초대 코드를 받아 주세요.")
    assert c.post("/device/unlink", json=signed(devices[0], "unlink")).status_code == 200
    assert c.post("/device/register", json={"invite": INVITE, "public_key": extra.jwk}).status_code == 201


# --- S35 / S23: snapshot shapes ------------------------------------------------------------------------

def _tasks(*rows):
    snap = copy.deepcopy(SNAPSHOT)
    snap["sections"]["U2026A"]["tasks"] = list(rows)
    return snap


def test_deadlines_say_when_klas_changed_its_due_format():
    now = datetime(2099, 11, 6, 10, 0, tzinfo=KST)
    task = SNAPSHOT["sections"]["U2026A"]["tasks"][0]
    renamed = {k: v for k, v in task.items() if k != "expiredate"} | {"deadline": "2099-11-07 23:59:59"}
    for snap in (_tasks(renamed), _tasks({**task, "expiredate": "2099.11.07 오후 11:59"})):
        out = shapes.deadlines(snap, now, 7)
        assert out["items"] == []
        assert out["incomplete"] == {
            "missing": ["assignment"], "unreadable_due": ["assignment"],
            "message": "KLAS에서 일부 목록을 읽지 못해 빠진 마감이 있을 수 있습니다. 과제 목록은 마감 시각을 읽지 "
                       "못했습니다(KLAS 형식이 바뀐 것 같습니다). KLAS에서 직접 확인하세요."}
    assert "incomplete" not in shapes.deadlines(SNAPSHOT, now, 7)
    assert "incomplete" not in shapes.deadlines(_tasks(), now, 7)  # no rows: nothing to judge
    # a late due that parses is a readable row; a submitted assignment isn't judged (kw-mcp reads pending ones)
    late = {**task, "expiredate": "2099.11.01", "reexpiredate": "2099-11-08 23:59:59"}
    assert "incomplete" not in shapes.deadlines(_tasks(late), now, 7)
    done = {**task, "expiredate": "2099.11.07 오후 11:59", "submityn": "Y"}
    assert "incomplete" not in shapes.deadlines(_tasks(done), now, 7)


class _RowsService(KlasService):
    """kw_mcp's KlasService with its list reads answered by the rows the bridge shapes from a snapshot:
    deadlines() then runs kw-mcp's own rule on the same rows (a snapshot has no online tests, surveys
    or discussions)."""

    def __init__(self, snap, now):
        super().__init__(None, now=lambda: now)
        self.snap = snap

    async def current_semester(self):
        return self.snap["semester"]

    async def assignments(self, semester=None, course=None, pending_only=False):
        return shapes.assignments(self.snap, course, pending_only, partial=True)

    async def online_lectures(self, semester=None, course=None, incomplete_only=False):
        return shapes.online_lectures(self.snap, course, incomplete_only, partial=True)

    async def quizzes(self, semester=None, course=None, pending_only=False):
        return shapes.quizzes(self.snap, course, pending_only, partial=True)

    async def projects(self, semester=None, course=None):
        return shapes.projects(self.snap, course, partial=True)

    async def online_tests(self, semester=None, course=None, pending_only=False):
        return []

    async def surveys(self, semester=None, course=None, pending_only=False):
        return []

    async def discussions(self, semester=None, course=None):
        return []


def test_snapshot_deadlines_judge_unreadable_dues_like_kw_mcp():
    now = datetime(2099, 11, 6, 10, 0, tzinfo=KST)
    task = SNAPSHOT["sections"]["U2026A"]["tasks"][0]
    bad = "2099.11.07 오후 11:59"
    cases = [
        SNAPSHOT,
        _tasks({**task, "expiredate": bad}),
        _tasks({**task, "expiredate": bad, "reexpiredate": "2099-11-08 23:59:59"}),  # the late due reads
        _tasks({**task, "expiredate": bad, "submityn": "Y"}),  # done: kw-mcp doesn't read it
        _tasks({**task, "expiredate": bad}, {**task, "title": "HW4"}),  # one row reads: the kind is fine
    ]
    both = copy.deepcopy(SNAPSHOT)
    both["sections"]["U2026A"]["tasks"] = [{**task, "expiredate": bad}]
    both["sections"]["U2026A"]["projects"] = [{"title": "팀플", "purpose": "", "startdate": None, "expiredate": bad,
                                               "reexpiredate": None, "submityn": "N", "teamno": "1"}]
    cases.append(both)
    for snap in cases:
        bridged = shapes.deadlines(snap, now, 7)
        local = asyncio.run(_RowsService(snap, now).deadlines(7))
        assert bridged["items"] == local["items"]
        assert bridged.get("incomplete") == local.get("incomplete")
    assert local["incomplete"]["unreadable_due"] == ["assignment", "team_project"]
    assert "과제·팀 프로젝트 목록은 마감 시각을 읽지 못했습니다" in bridged["incomplete"]["message"]


def test_text_from_klas_rows_is_cleaned():
    snap = _tasks({**SNAPSHOT["sections"]["U2026A"]["tasks"][0], "title": "HW3\u200b \u202e제출\u2066"})
    snap["sections"]["U2026A"]["projects"] = [{"title": "팀\u200d플", "purpose": "목표\x07", "startdate": None,
                                               "expiredate": None, "reexpiredate": None, "submityn": "N"}]
    assert shapes.assignments(snap)[0]["title"] == "HW3 제출"
    project = shapes.projects(snap)[0]
    assert project["title"] == "팀플" and project["purpose"] == "목표"


# --- S39: malformed input ------------------------------------------------------------------------------

def test_deep_json_bad_bearer_and_bad_redirect_are_refused_not_crashes(env):
    c = env["client"]
    nested = c.post("/oauth/register", content=b"[" * 16_000, headers={"Content-Type": "application/json"})
    assert nested.status_code == 400
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
               headers={"Authorization": "Bearer a.b.\xe9\xe9".encode("latin-1"),
                        "Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert crypto.verify_jwt(b"k", "a.b.한글", "aud") is None
    bad = c.post("/oauth/register", json={"client_name": "x", "redirect_uris": ["http://[::1"]})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"


# --- S11 / S36: descriptions and the briefing's school notices ---------------------------------------------

def test_class_change_description_names_the_wider_words(env):
    c, _, token = _connected(env)
    listed = {t["name"]: t for t in mcp_call(c, token, "tools/list").json()["result"]["tools"]}
    text = listed["my_class_changes"]["description"]
    assert all(word in text for word in ("결강", "보강", "비대면", "수업 장소/시간 변경"))


def test_bridge_uses_the_extensions_alert_rule():
    from kw_mcp import combined

    assert bridge.campus_alert_notices is combined.campus_alert_notices
    assert bridge.holiday is combined.holiday and bridge.classes_note is combined.classes_note

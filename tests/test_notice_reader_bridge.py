"""The connector's read_notice (bridge/notice_reader.py over kw_mcp.notices).

Synthetic pages shaped like the real boards; made-up titles and text, and the only "names" are
made up to check that they are masked. The school sites are an httpx MockTransport (no network);
DynamoDB is moto."""

import asyncio
import base64
import json
import struct
import zlib
from datetime import UTC, datetime

import httpx
import pytest
import test_bridge
from kw_mcp import notices
from mcp.server.mcpserver.exceptions import ToolError
from test_bridge import FakeDevice, mcp_call, oauth, register, tool

from kwcampus import aicon, deptboards, politeness, public_data, wixfeeds, wwwboards
from kwcampus.bridge import app as bridge
from kwcampus.bridge import notice_reader, store
from kwcampus.config import KST
from kwcampus.kwnotice.source import build_detail_url
from kwcampus.kwnotice.values import DUID

env = test_bridge.env  # the bridge app, its kw-bridge table and kw-public, under moto

PAGE_URL = "https://electric.kw.ac.kr/community/s_notice.php?BoardMode=view&UID=950"
POSTER = "https://www.kw.ac.kr/KWData/webeditor/2026/poster.png"
PAGE = """<html><head><meta charset="UTF-8"><title>광운대학교 전기공학과</title></head><body>
<div id="contentArea" class="list_view">
<div class="view_top"><h3>2026 테스트 취업 특강 안내</h3><ul><li><span>관리자</span></li><li><span>2026-11-04</span></li>
<li class="hit"><span>10</span></li></ul></div>
<div class="view_file"><span><a href='/LIB/PHP_FUNC/Download.php?param=f1'><img src='/Board/img/icon_download.png'> 신청서.txt</a></span></div>
<div class="view_con"><p>특강을 엽니다. 문의는 홍길동 조교(010-1234-5678)에게 하세요.</p>
<p><img src="https://www.kw.ac.kr/KWData/webeditor/2026/poster.png" alt="특강 포스터"></p></div>
</div></body></html>"""
WIX_URL = "https://www.kwmedia.info/post/test-notice"
WIX_PAGE = """<html><head><meta property="article:published_time" content="2026-11-01T01:00:00Z"></head><body>
<article><h1 data-hook="post-title">테스트 공지</h1><div data-hook="post-description"><p>본문입니다.</p>
<img src="https://static.wixstatic.com/media/abc~mv2.jpg/v1/fill/w_800,h_600/abc~mv2.jpg"></div></article></body></html>"""


def png(width: int, height: int, pad: int = 0) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    extra = chunk(b"tEXt", b"pad\x00" + b"x" * pad) if pad else b""
    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class School:
    """The school sites: url -> response; every request is recorded."""

    def __init__(self, pages: dict[str, httpx.Response]):
        self.pages, self.requests = pages, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.pages.get(str(request.url)) or httpx.Response(404)


def html(text: str) -> httpx.Response:
    return httpx.Response(200, content=text.encode(), headers={"Content-Type": "text/html; charset=UTF-8"})


@pytest.fixture
def school(env, monkeypatch):
    notices.clear_cache()
    site = School({PAGE_URL: html(PAGE), POSTER: httpx.Response(200, content=png(600, 800)),
                   "https://electric.kw.ac.kr/LIB/PHP_FUNC/Download.php?param=f1": httpx.Response(
                       200, content="신청자: 김철수 학생\n제출 기한: 11월 10일".encode())})
    monkeypatch.setattr(notice_reader, "_transport", httpx.MockTransport(site))
    monkeypatch.setattr(notice_reader, "MIN_INTERVAL_S", 0.0)  # the interval has its own test
    yield site
    notices.clear_cache()


def _connected(env):
    c, device = env["client"], FakeDevice()
    register(c, device)
    store.add_connection(env["table"], device.uid, "conn-1", 3)
    _, tokens = oauth(c, device)
    return c, tokens["access_token"]


def _day() -> str:
    return datetime.now(UTC).astimezone(KST).strftime("%Y-%m-%d")


def _count(env, host: str) -> dict:
    return env["table"].get_item(Key={"PK": f"NOTICEHOST#{host}", "SK": f"DAY#{_day()}"}).get("Item") or {}


# --- one list of hosts ----------------------------------------------------------------------------

# Boards kw-campus collects ahead of the released extension (kw-mcp 1.1.0, whose tables the bridge also
# runs): their notices are listed and searched, but read_notice can't open those on new hosts, and their
# post ids don't map to a URL, until kw-mcp's DEPT_BOARDS lists them. Add them there in the next kw-mcp
# release, then empty this set.
AHEAD_OF_KW_MCP: frozenset[str] = frozenset()


def test_allowed_hosts_are_the_collectors_hosts():
    known = [b for b in deptboards.BOARDS if b.key not in AHEAD_OF_KW_MCP]
    assert AHEAD_OF_KW_MCP <= {b.key for b in deptboards.BOARDS} and not AHEAD_OF_KW_MCP & set(notices.DEPT_BOARDS)
    assert notices.DEPT_BOARDS == {b.key: (b.name, b.host, b.path, b.group) for b in known}
    assert notices.WIX_SITES == {f.key: (f.name, f.host) for f in wixfeeds.FEEDS}
    assert {notices.WWW_BOARDS[b.source] for b in wwwboards.BOARDS} == {b.path for b in wwwboards.BOARDS}
    assert notices.PAGE_HOSTS == {"www.kw.ac.kr", aicon.HOST, *(b.host for b in known),
                                  *(f.host for f in wixfeeds.FEEDS)}
    assert notices.DEPARTMENT_OF_HOST[aicon.HOST] == aicon.CATEGORY_NAME
    assert all(host in politeness.ROBOTS for host in notices.PAGE_HOSTS)  # every page host was checked by hand
    assert "job.kw.ac.kr" in politeness.ROBOTS and "job.kw.ac.kr" in notices.PAGE_HOSTS  # 경력개발팀 (Crawl-delay 10)


def test_every_collected_notice_link_is_readable():
    """school_notices / latest_notices / search_notices / get_notice rows carry `url`; the reader takes it
    (but for the boards on hosts the released kw-mcp doesn't know yet)."""
    ahead = [b for b in deptboards.BOARDS if b.host not in notices.PAGE_HOSTS]
    assert {b.key for b in ahead} <= AHEAD_OF_KW_MCP
    for b in ahead:
        with pytest.raises(notices.NoticeError):
            notices.check_url(b.post_url("123"))
    links = [b.post_url("123") for b in deptboards.BOARDS if b not in ahead] + [b.post_url("456") for b in wwwboards.BOARDS]
    links += [str(build_detail_url(DUID("53151"))), f"https://{aicon.HOST}/administration/notice.php?admin_mode=read&no=3525"]
    links += [f"https://{f.host}/post/some-post" for f in wixfeeds.FEEDS]
    for link in links:
        assert notices.check_url(link) == link
    for source, raw in [("kw-notice", "53151"), ("dept-ee", "646"), ("dept-chemng", "78"), ("aicon", "3525"), ("kw-job", "9")]:
        url = (str(build_detail_url(DUID(raw))) if source == "kw-notice" else
               f"https://{aicon.HOST}/administration/notice.php?admin_mode=read&no={raw}" if source == "aicon" else
               next(b for b in wwwboards.BOARDS if b.source == source).post_url(raw) if source.startswith("kw-") else
               next(b for b in deptboards.BOARDS if b.source == source).post_url(raw))
        row = public_data.public_view({"SK": f"N#{raw}", "post_id": raw, "source": source, "url": url})
        assert row["url"] == url and notices.url_for_post_id(row["post_id"]) == url


# --- through the connector ------------------------------------------------------------------------

def test_read_notice_through_the_connector(env, school):
    c, token = _connected(env)
    listed = {t["name"]: t for t in mcp_call(c, token, "tools/list").json()["result"]["tools"]}
    assert "read_notice" in listed and "outputSchema" not in listed["read_notice"]
    assert "KLAS_CONTENT" in listed["read_notice"]["description"]
    result = tool(c, token, "read_notice", url=PAGE_URL, images=True)
    assert not result.get("isError"), result
    blocks = result["content"]
    assert [b["type"] for b in blocks] == ["text", "text", "image"]
    data = json.loads(blocks[0]["text"])
    assert (data["title"], data["date"], data["department"]) == ("2026 테스트 취업 특강 안내", "2026-11-04", "전기공학과")
    assert "홍길동" not in data["body"] and "[REDACTED_NAME]" in data["body"]
    assert "010-1234-5678" not in data["body"] and data["body"].startswith("<<<KLAS_CONTENT")
    assert base64.b64decode(blocks[2]["data"]) == png(600, 800) and blocks[2]["mimeType"] == "image/png"
    assert [str(r.url) for r in school.requests] == [PAGE_URL, POSTER]
    assert all(r.headers["user-agent"].startswith("kw-mcp/") for r in school.requests)
    page_count, image_count = _count(env, "electric.kw.ac.kr"), _count(env, "www.kw.ac.kr")
    assert page_count["n"] == 1 and image_count["n"] == 1
    assert int(page_count["ttl"]) > datetime.now(UTC).timestamp() + 86400


def test_attachment_text_is_masked_too(env, school):
    blocks = asyncio.run(notice_reader.read_notice(url=PAGE_URL, attachment=1))
    data = json.loads(blocks[0].text)
    assert data["file"] == "신청서.txt" and "김철수" not in data["text"] and "제출 기한" in data["text"]


def test_post_id_uses_the_stored_link(env, school):
    hex_id = "0123456789abcdef01234567"
    env["public"].put_item(Item={
        "PK": "SRC#dept-media", "SK": f"N#{hex_id}", "source": "dept-media", "post_id": hex_id, "title": "테스트 공지",
        "category_id": "dept-media", "department": "미디어커뮤니케이션학부", "posted_date": "2026-11-01",
        "updated_date": "2026-11-01", "url": WIX_URL, "pinned": False, "ttl": int(datetime.now(UTC).timestamp()) + 86400})
    public_data.clear_cache()
    school.pages[WIX_URL] = html(WIX_PAGE)
    blocks = asyncio.run(notice_reader.read_notice(post_id=f"dept-media-{hex_id}"))
    data = json.loads(blocks[0].text)
    assert (data["title"], data["source_url"]) == ("테스트 공지", WIX_URL)
    # Wix's file host has no robots record: listed, never requested from the connector
    shown = asyncio.run(notice_reader.read_notice(url=WIX_URL, images=True))
    image = json.loads(shown[0].text)["images"][0]
    assert "robots" in image["skipped"] and len(shown) == 1
    assert not any(r.url.host == "static.wixstatic.com" for r in school.requests)


def test_post_id_of_an_expired_notice_is_built_from_the_id(env, school):
    public_data.clear_cache()
    blocks = asyncio.run(notice_reader.read_notice(post_id="dept-electric-950"))
    assert json.loads(blocks[0].text)["source_url"] == PAGE_URL


def test_off_list_url_is_refused_without_a_request(env, school):
    with pytest.raises(ToolError, match="광운대 공지 사이트"):
        asyncio.run(notice_reader.read_notice(url="https://www.other-university.ac.kr/notice/1"))
    with pytest.raises(ToolError, match="robots"):
        asyncio.run(notice_reader.read_notice(url=WIX_URL + "?lightbox=1"))  # its robots.txt disallows this
    assert school.requests == []


# --- politeness -------------------------------------------------------------------------------------

def test_daily_cap_per_host(env, school):
    env["table"].put_item(Item={"PK": "NOTICEHOST#electric.kw.ac.kr", "SK": f"DAY#{_day()}",
                                "n": notice_reader.DAILY_CAP, "next_at": 0, "ttl": 2**31 - 1})
    with pytest.raises(ToolError, match=f"오늘 이 연결 서버가 electric.kw.ac.kr에서 읽을 수 있는 횟수\\({notice_reader.DAILY_CAP}번\\)"):
        asyncio.run(notice_reader.read_notice(url=PAGE_URL))
    assert school.requests == []
    assert _count(env, "electric.kw.ac.kr")["n"] == notice_reader.DAILY_CAP  # a refusal doesn't count


def test_daily_cap_on_an_image_host_skips_the_image(env, school):
    env["table"].put_item(Item={"PK": "NOTICEHOST#www.kw.ac.kr", "SK": f"DAY#{_day()}",
                                "n": notice_reader.DAILY_CAP, "next_at": 0, "ttl": 2**31 - 1})
    blocks = asyncio.run(notice_reader.read_notice(url=PAGE_URL, images=True))
    assert len(blocks) == 1 and "횟수" in json.loads(blocks[0].text)["images"][0]["skipped"]


def test_interval_is_shared_by_every_instance(env):
    now, waits = [1_800_000_000.0], []

    async def sleep(seconds):
        waits.append(round(seconds, 2))
        now[0] += seconds

    first = notice_reader.BridgeGate(env["table"], clock=lambda: now[0], sleep=sleep)
    second = notice_reader.BridgeGate(env["table"], clock=lambda: now[0], sleep=sleep)  # another Lambda instance
    asyncio.run(first.before(PAGE_URL))
    asyncio.run(second.before(PAGE_URL))
    asyncio.run(first.before("https://ee.kw.ac.kr/x"))  # another host: no wait
    assert waits == [notice_reader.MIN_INTERVAL_S]
    day = datetime.fromtimestamp(now[0], UTC).astimezone(KST).strftime("%Y-%m-%d")
    item = env["table"].get_item(Key={"PK": "NOTICEHOST#electric.kw.ac.kr", "SK": f"DAY#{day}"})["Item"]
    assert item["n"] == 2


def test_waiting_past_the_deadline_gives_up(env):
    import time

    gate = notice_reader.BridgeGate(env["table"], deadline=time.monotonic() + 1)
    env["table"].put_item(Item={"PK": "NOTICEHOST#ee.kw.ac.kr", "SK": f"DAY#{_day()}", "n": 1,
                                "next_at": int(datetime.now(UTC).timestamp() * 1000) + 60_000, "ttl": 2**31 - 1})
    with pytest.raises(notices.Refused, match="요청이 몰려"):
        asyncio.run(gate.before("https://ee.kw.ac.kr/x"))


def test_answer_stays_under_the_payload_limit(env, school):
    posters = "".join(f'<p><img src="https://www.kw.ac.kr/KWData/webeditor/2026/p{i}.png"></p>' for i in range(5))
    school.pages[PAGE_URL] = html(PAGE.replace("</div>\n</div></body>", posters + "</div>\n</div></body>"))
    for i in range(5):
        school.pages[f"https://www.kw.ac.kr/KWData/webeditor/2026/p{i}.png"] = httpx.Response(
            200, content=png(900, 1200, pad=1400 * 1024))
    blocks = asyncio.run(notice_reader.read_notice(url=PAGE_URL, images=True))
    images = [b for b in blocks if b.type == "image"]
    assert len(images) == 3  # the small poster and two of 1.4 MB: a third would pass LIMITS.images_total (3 MB)
    assert sum(len(base64.b64decode(b.data)) for b in images) <= notice_reader.LIMITS.images_total
    answer = json.dumps({"content": [b.model_dump(by_alias=True, exclude_none=True) for b in blocks]})
    assert len(answer.encode()) < notice_reader.MAX_RESPONSE_BYTES < 6 * 1024 * 1024


def test_instructions_point_school_questions_here():
    line = next(x for x in bridge.INSTRUCTIONS.splitlines() if "read_notice" in x)
    assert "search_notices" in line and "학칙" in line and "장학" in line
    assert "웹 검색" in bridge.INSTRUCTIONS

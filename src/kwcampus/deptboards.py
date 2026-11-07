"""Department and college boards, and school offices' boards, on two shared PHP board engines.

A group: list `<page>.php?BoardMode=list&BOARD_CODE=<code>&CURRENT_PAGE=1` (just `<page>.php` for a
board without a BOARD_CODE: 경력개발팀 pages with ?tpage=), post `?BoardMode=view&UID=<n>`, rows in
`table.board_table`, pinned rows `tr.notice_tr`.
B group (the aicon-style engine): list `<page>.php`, post `?admin_mode=read&no=<n>`, rows in
`table.board_list` or `table.board-list`, pinned rows without a number.
Page 1 shows every pinned post plus the newest normal ones, and some departments post new items
mostly as pinned, so pinned rows are read too. A post not stored before is new when its number is
above the highest on the last page read, or when it was posted in the last 14 days (a board that
renumbered, a row that failed to parse once); an older one turning up (a post sliding back onto
page 1, or one re-added after the TTL) is stored quietly. A page whose highest number went down is
logged (UidRegressed): a deleted newest post or a renumbered board.

The school sites sit on four outside servers, many host names each, so the politeness gate is per
server: one request per collector run, to the server's board checked least recently (round-robin).
E3 also serves the 인공지능융합대학 widget (aicon.py); the widget and E3's boards take turns.
A board's own trouble (404, a redirect, a changed page) backs off only that board; 403/429/a
challenge page and connection failures or 5xx count for the server (politeness.py).

Each board is its own source ("SRC#dept-<key>"): ids stay distinct and seeding is per board.
A department's or college's board has the category "dept-<key>" (routing: the 학과 channel and its
college's topic); a school office's or funded program's board on the same servers (사업단, 센터,
경력개발팀, 국제처, 일반대학원) has "office-<key>", and routing.CATEGORY_TO_CHANNEL gives each its channel.
The 전자정보공과대학 notice is mirrored on every department site; it is collected once, from ei.
To add a board, append a row to BOARDS. Department names live only there (they change in 2028).

Request budget per server and weekday (31 list runs: every 20 min 09:00-18:40, and 21:00). A board
adds no request, only a longer wait for each board on its server:
  E1  11 boards                 31 requests, each board every 11 runs (3 h 40 min)
  E2  12 boards                 31 requests, each board every 12 runs (4 h)
  E3   9 boards + aicon widget  31 requests, the widget every other run, each board every 18 runs (6 h)
  E4   2 boards                 31 requests, each board every 2 runs (40 min)
So DAILY_CAP stays 32 a server (31 + a spare), and the page runs' reserve (extract._reserve: one
request for each list run still to come) stays the same; pages get what the cap has left over.
job.kw.ac.kr asks for Crawl-delay: 10: list requests are a minute apart (politeness.MIN_INTERVAL), and a
page run's images and files on that host wait 10 s instead of ASSET_GAP_S (politeness.crawl_delay).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from kwcampus import politeness, store
from kwcampus.config import KST, NEW_POST_WINDOW
from kwcampus.fetch import WORST_CASE_S, empty_reason, fetch_list_page
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.redaction import redact_text
from kwcampus.kwnotice.values import DUID, SourceURL
from kwcampus.obs import log, metrics

SERVER_E1 = "61.111.8.4"  # ei, ee, elcomm, radiowave, electric, snme, semicon, law, job, startup.kw.ac.kr
SERVER_E2 = "61.111.6.200"  # archi, chss, korean, english, psy, dnaci, kwpa, biz, kwiie, humanrights, oia, grad
# aicon (ce, cs, ic, cni), kwlife, kwlaw, kwgame, kwsports, ingenium, semi, huss, inno, liberal
SERVER_E3 = "211.233.72.248"
SERVER_E4 = "114.203.87.122"  # chemng, sports
# The collector runs 30 times a weekday (every 20 min, 09:00-18:40) and at 21:00, one request per
# server each; the cap keeps manual or overlapping runs from adding more. Each board is read every
# <boards on its server> runs (the budget in the module docstring).
DAILY_CAP = 32  # one request per server per run: 30 daytime weekday runs + 21:00 (+ a spare)
BOARD_BACKOFF = timedelta(hours=2)  # a board's own failures: 2 h, 4 h, ... up to 24 h


@dataclass(frozen=True)
class Board:
    key: str  # source "dept-<key>"; lowercase letters only (kw-mcp reads post ids "dept-<key>-<n>")
    name: str  # what users see as the category and department
    host: str
    path: str  # the list page
    code: str  # BOARD_CODE ("" for the B group, and for an A board without one)
    encoding: str = "utf-8"  # "cp949" for EUC-KR sites (english.kw.ac.kr)
    server: str = SERVER_E1
    group: str = "A"
    office: bool = False  # a school office's or program's board, not a department's: category "office-<key>"

    @property
    def source(self) -> str:
        return f"dept-{self.key}"

    @property
    def category_id(self) -> str:
        return f"office-{self.key}" if self.office else f"dept-{self.key}"

    @property
    def list_url(self) -> str:
        if self.group == "B" or not self.code:
            return f"https://{self.host}{self.path}"
        return f"https://{self.host}{self.path}?BoardMode=list&BOARD_CODE={self.code}&CURRENT_PAGE=1"

    def post_url(self, uid: str) -> str:
        if self.group == "B":
            return f"https://{self.host}{self.path}?admin_mode=read&no={uid}"
        return f"https://{self.host}{self.path}?BoardMode=view&UID={uid}"


BOARDS: tuple[Board, ...] = (
    Board("ei", "전자정보공과대학", "ei.kw.ac.kr", "/community/notice.php", "notice"),
    Board("ee", "전자공학과", "ee.kw.ac.kr", "/HTML/department/undergrad_info.php", "bachelor"),
    Board("elcomm", "전자통신공학과", "elcomm.kw.ac.kr", "/department/office_notice.php", "officenotice"),
    Board("radiowave", "전자융합공학과", "radiowave.kw.ac.kr", "/community/s_notice.php", "notice"),
    Board("electric", "전기공학과", "electric.kw.ac.kr", "/community/s_notice.php", "notice"),
    Board("snme", "전자재료공학과", "snme.kw.ac.kr", "/college/college_notice.php", "college"),
    Board("semicon", "반도체시스템공학부", "semicon.kw.ac.kr", "/HTML/community/notice.php", "notice"),
    Board("law", "법학부", "law.kw.ac.kr", "/bulletin/notice.php", "notice"),  # 100 rows a page
    # 취업정보·대외활동: the department's own second board, so the same name (a student's my_notices finds it)
    Board("electricjob", "전기공학과", "electric.kw.ac.kr", "/autonomy_active/employment_field.php", "job"),
    Board("career", "경력개발팀", "job.kw.ac.kr", "/community/notice.php", "", office=True),  # robots: Crawl-delay 10
    Board("startup", "창업지원단", "startup.kw.ac.kr", "/communication/notice.php", "notice", office=True),
    Board("archi", "건축공학과", "archi.kw.ac.kr", "/community/notice.php", "note", server=SERVER_E2),
    Board("chss", "인문사회과학대학", "chss.kw.ac.kr", "/notice/news.php", "notice", server=SERVER_E2),
    Board("korean", "국어국문학과", "korean.kw.ac.kr", "/community/notice.php", "notice", server=SERVER_E2),
    Board("english", "영어산업학과", "english.kw.ac.kr", "/new/bulletin/notice.php", "notice", "cp949",
          server=SERVER_E2),
    Board("psy", "산업심리학과", "psy.kw.ac.kr", "/community/notice.php", "notice", server=SERVER_E2),
    Board("dnaci", "동북아문화산업학부", "dnaci.kw.ac.kr", "/notice/news.php", "notice", server=SERVER_E2),
    Board("kwpa", "행정학과", "kwpa.kw.ac.kr", "/notice/faculty.php", "faculty", server=SERVER_E2),
    Board("biz", "경영대학", "biz.kw.ac.kr", "/community/notice.php", "note", server=SERVER_E2),  # 65 pinned
    Board("kwiie", "한국어교육센터", "kwiie.kw.ac.kr", "/community/notice.php", "notice", server=SERVER_E2, office=True),
    Board("humanrights", "인권센터", "humanrights.kw.ac.kr", "/community/notice.php", "notice", server=SERVER_E2,
          office=True),
    Board("oia", "국제처 유학생", "oia.kw.ac.kr", "/campus/notice.php", "note", server=SERVER_E2, office=True),
    Board("grad", "일반대학원", "grad.kw.ac.kr", "/notice/news.php", "notice", server=SERVER_E2, office=True),
    Board("kwlife", "참빛인재대학", "kwlife.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("kwlaw", "금융부동산법무학과", "kwlaw.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("kwgame", "게임콘텐츠학과", "kwgame.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("kwsports", "스포츠상담재활학과", "kwsports.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("ingenium", "인제니움학부대학", "ingenium.kw.ac.kr", "/inform/notice.php", "notice", server=SERVER_E3),
    Board("liberal", "자율전공학부", "liberal.kw.ac.kr", "/bulletin/notice.php", "notice", server=SERVER_E3),
    Board("semi", "반도체특성화대학사업단", "semi.kw.ac.kr", "/communication/notice.php", "notice", server=SERVER_E3,
          office=True),
    Board("huss", "HUSS 사업단", "huss.kw.ac.kr", "/bulletin/notice.php", "notice", server=SERVER_E3, office=True),
    Board("inno", "대학혁신사업단", "inno.kw.ac.kr", "/bulletin/notice.php", "notice", server=SERVER_E3, office=True),
    Board("chemng", "화학공학과", "chemng.kw.ac.kr", "/community/notice.php", "", server=SERVER_E4, group="B"),
    Board("sports", "스포츠융합과학과", "sports.kw.ac.kr", "/community/notice.php", "", server=SERVER_E4, group="B"),
)

_TITLE_HEADS = {"제목", "subject", "title"}
_DATE_HEADS = {"등록일", "작성일", "날짜", "date"}
_NUMBER_HEADS = {"no", "번호"}
_TABLE = {"A": "table.board_table", "B": "table.board_list, table.board-list"}
_POST = {"A": re.compile(r"[?&]UID=(\d{1,12})(?:&|$)"),
         "B": re.compile(r"[?&]admin_mode=read&no=(\d{1,12})(?:&|$)")}
_DATE = re.compile(r"(\d{4}|\d{2})[-./](\d{1,2})[-./](\d{1,2})")


def _column(heads: list[str], names: set[str]) -> int | None:
    return next((i for i, head in enumerate(heads) if head in names), None)


def _rows(body, width: int) -> list[tuple[list, bool]]:
    """(cells, has notice_tr) per row. 법학부 serves its pinned rows without <tr>, so loose cells
    are grouped by the header width."""
    rows, loose = [], []
    for node in body.find_all(["tr", "td"], recursive=False):
        if node.name == "tr":
            rows.append((node.find_all("td", recursive=False), "notice_tr" in (node.get("class") or [])))
            continue
        loose.append(node)
        if len(loose) == width:
            rows.append((loose, False))
            loose = []
    return rows


def parse_board(html: str, board: Board) -> list[NoticeSummary]:
    """Pinned and normal rows of page 1. Columns are found by header text (법학부 puts the date
    before the writer); a pinned row has notice_tr or no number ("2,157" is one) in the number column."""
    table = BeautifulSoup(html, "html.parser").select_one(_TABLE[board.group])
    if table is None:
        return []
    heads = [" ".join(th.get_text(" ", strip=True).split()).casefold().strip(" .") for th in table.select("thead th")]
    title_at, date_at, number_at = (_column(heads, names) for names in (_TITLE_HEADS, _DATE_HEADS, _NUMBER_HEADS))
    if title_at is None or date_at is None:
        return []
    seen, out = set(), []
    for cells, flagged in _rows(table.find("tbody") or table, len(heads)):
        if len(cells) <= max(title_at, date_at):
            continue
        link = cells[title_at].find("a", href=_POST[board.group])
        when = _DATE.search(cells[date_at].get_text(" ", strip=True))
        if link is None or when is None:
            continue
        uid = _POST[board.group].search(link["href"]).group(1)
        title = redact_text(" ".join(link.get_text(" ").replace("\ufffd", "").split()))[:300]
        year = int(when.group(1))
        try:
            posted = date(year + 2000 if year < 100 else year, int(when.group(2)), int(when.group(3)))
        except ValueError:
            continue
        if not title or uid in seen:
            continue
        seen.add(uid)
        number = cells[number_at].get_text(strip=True) if number_at is not None and number_at < len(cells) else "0"
        out.append(NoticeSummary(
            duid=DUID(uid), title=title, category_id=CategoryId(board.category_id),
            category_name=CategoryName(board.name), posted_date=posted, updated_date=posted,
            department=board.name, source_url=SourceURL(board.post_url(uid)), attachments_present=False,
            pinned=flagged or not number.replace(",", "").isdigit()))
    return out


def _state_key(board: Board) -> dict[str, str]:
    return {"PK": store.source_pk(board.source), "SK": "STATE"}


def round_size(server: str) -> int:
    """Requests in one full round of a server: its boards, plus the aicon widget on E3."""
    return sum(b.server == server for b in BOARDS) + (1 if server == SERVER_E3 else 0)


def _empty_streak(table, server: str, step: int | None) -> int:
    """Consecutive answers from `server` that parsed to no rows; step None resets it."""
    key = {"PK": f"DEPTSERVER#{server}", "SK": "STATE"}
    if step is None:
        table.put_item(Item={**key, "empty_streak": 0})
        return 0
    item = table.update_item(Key=key, UpdateExpression="ADD empty_streak :one",
                             ExpressionAttributeValues={":one": step}, ReturnValues="UPDATED_NEW")
    return int(item["Attributes"]["empty_streak"])


def note_empty(table, server: str, now: datetime) -> int:
    """One more empty answer from `server` (a board or the aicon widget). A whole round of them is
    likely a block page we don't recognise, so the server backs off (a transient failure)."""
    streak = _empty_streak(table, server, +1)
    if streak >= round_size(server):
        _empty_streak(table, server, None)
        politeness.record_block(table, server, now, "no_rows_everywhere")
    return streak


def note_answer(table, server: str) -> None:
    _empty_streak(table, server, None)


def collect(now: datetime, table, fetch=fetch_list_page, clock: Callable[[], datetime] | None = None,
            time_left: Callable[[], float] | None = None) -> dict[str, object]:
    """One request per server: {server: result}. clock gives the time of each request (the run's
    start by default); time_left (Lambda seconds) skips a server rather than be cut off mid-request."""
    clock = clock or (lambda: now)
    out: dict[str, object] = {}
    for server in dict.fromkeys(b.server for b in BOARDS):
        try:
            out[server] = _collect_server(server, now, table, fetch, clock, time_left)
        except Exception as e:  # noqa: BLE001 - one server's trouble must not cost the others their turn
            log("dept_error", server=server, error=e.__class__.__name__)
            out[server] = {"status": "error"}
    return out


def _collect_server(server: str, now: datetime, table, fetch, clock, time_left) -> dict[str, object]:
    if time_left is not None and time_left() < WORST_CASE_S:
        return {"status": "skipped", "reason": "no_time"}
    boards = [b for b in BOARDS if b.server == server]
    states = {b.key: table.get_item(Key=_state_key(b), ConsistentRead=True).get("Item") or {} for b in boards}
    for b in boards:
        if states[b.key].get("disabled"):
            politeness.source_skipped(b.source, states[b.key])
    allowed = [b for b in boards if politeness.robots_allows(b.list_url)]
    for b in set(boards) - set(allowed):
        politeness.robots_skip(b.source, b.list_url)
    ready = [b for b in allowed if not politeness.source_waiting(states[b.key], now)]
    if not ready:
        return {"status": "skipped", "reason": "no_board_due"}
    permit = politeness.acquire(table, server, clock(), daily_cap=DAILY_CAP, by="board")
    if not permit.granted:
        log("dept_skip", server=server, reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}
    board = min(ready, key=lambda b: int(states[b.key].get("checked_at", 0)))  # ties: list order
    state = states[board.key]
    table.update_item(Key=_state_key(board), UpdateExpression="SET checked_at = :now",
                      ExpressionAttributeValues={":now": int(now.timestamp())})

    result = fetch(board.list_url, encoding=board.encoding, errors="replace")
    records = parse_board(result.html, board) if result.html else []
    reason = result.block_reason or (None if records else empty_reason(result.html))
    if reason in politeness.HARD or (reason and not politeness.is_server_signal(reason)):
        failed = politeness.source_failed(table, _state_key(board), state, now, reason, BOARD_BACKOFF)
        log("dept_board_failed", board=board.key, reason=reason, status=result.status, **failed)
    if reason in politeness.HARD or reason in ("transport_failure", "server_error", "slow"):
        blocked = politeness.record_block(table, server, clock(), reason, result.retry_after)
        log("dept_blocked", board=board.key, reason=reason, status=result.status, **blocked)
        metrics(DeptFetched=1, Blocked=int(reason in politeness.HARD), Transient=int(reason not in politeness.HARD))
        return {"status": "blocked", "board": board.key, "reason": reason}
    if reason == "no_rows":  # this board's page changed; the server answered, so its other boards go on
        streak = note_empty(table, server, clock())
        log("dept_no_rows", board=board.key, status=result.status, streak=streak)
        metrics(DeptFetched=1, ParseEmpty=1)
        return {"status": "no_rows", "board": board.key}
    if reason:  # 404, a redirect, an unexpected page: only this board waits (source_failed above)
        metrics(DeptFetched=1)
        return {"status": "failed", "board": board.key, "reason": reason}
    note_answer(table, server)

    seeded = bool(state.get("seeded_at"))
    high = int(state.get("max_uid", 0))
    today = now.astimezone(KST).date()
    page_max = max(int(r.duid) for r in records)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    quiet_inserts = 0
    for record in records:
        quiet = not seeded or (int(record.duid) <= high and today - record.posted_date > NEW_POST_WINDOW)
        outcome = store.upsert(table, store.notice_item(board.source, record, now, seed=quiet))
        counts[outcome] += 1
        quiet_inserts += int(outcome == "inserted" and quiet and seeded)
    if seeded and page_max < high:  # the newest post is gone, or the board renumbered: start from this page
        log("dept_uid_regressed", board=board.key, page_max=page_max, high=high)
        metrics(UidRegressed=1)
    table.update_item(
        Key=_state_key(board),
        UpdateExpression="SET seeded_at = if_not_exists(seeded_at, :now), max_uid = :max",
        ExpressionAttributeValues={":now": int(now.timestamp()), ":max": page_max if seeded else max(high, page_max)},
    )
    politeness.record_success(table, server)
    politeness.source_ok(table, _state_key(board), state)
    log("dept_ok", board=board.key, rows=len(records), seeded_now=not seeded, quiet_inserts=quiet_inserts, **counts)
    metrics(DeptFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "board": board.key, "rows": len(records), "seed_run": not seeded, **counts}

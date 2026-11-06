"""Department and college boards on two shared PHP board engines.

A group: list `<page>.php?BoardMode=list&BOARD_CODE=<code>&CURRENT_PAGE=1`, post
`?BoardMode=view&UID=<n>`, rows in `table.board_table`, pinned rows `tr.notice_tr`.
B group (the aicon-style engine): list `<page>.php`, post `?admin_mode=read&no=<n>`, rows in
`table.board_list` or `table.board-list`, pinned rows without a number.
Page 1 shows every pinned post plus the newest normal ones, and some departments post new items
mostly as pinned, so pinned rows are read too and newness is decided by the post number: above the
highest seen before means new; a lower one that turns up (a post sliding back onto page 1, or one
re-added after the TTL) is stored quietly.

The school sites sit on four outside servers, many host names each, so the politeness gate is per
server: one request per collector run, to the server's board checked least recently (round-robin).
E3 also serves the 인공지능융합대학 widget (aicon.py), which goes first and takes every other run.

Each board is its own source ("SRC#dept-<key>"): ids stay distinct and seeding is per board.
The 전자정보공과대학 notice is mirrored on every department site; it is collected once, from ei.
To add a board, append a row to BOARDS. Department names live only there (they change in 2028).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

from kwcampus import politeness, store
from kwcampus.fetch import fetch_list_page
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.redaction import redact_text
from kwcampus.kwnotice.values import DUID, SourceURL
from kwcampus.obs import log, metrics

SERVER_E1 = "61.111.8.4"  # ei, ee, elcomm, radiowave, electric, snme, semicon, law (and job).kw.ac.kr
SERVER_E2 = "61.111.6.200"  # archi, chss, korean, english, psy, dnaci, kwpa, biz
SERVER_E3 = "211.233.72.248"  # aicon (ce, cs, ic, cni), kwlife, kwlaw, kwgame, kwsports, ingenium
SERVER_E4 = "114.203.87.122"  # chemng, sports
# The collector runs 30 times a weekday (every 20 min, 09:00-18:40), one request per server each;
# the cap keeps manual or overlapping runs from adding more. Each board is read every
# <boards on its server> runs: E1 and E2 (8 boards) every 2h40m, E4 (2) every 40 min, and E3's
# 5 boards share the runs the aicon widget leaves (every other one), so every 3h20m.
DAILY_CAP = 32  # one request per server per run: 30 daytime weekday runs + 21:00 (+ a spare)


@dataclass(frozen=True)
class Board:
    key: str  # source and category id are "dept-<key>"
    name: str  # what users see as the category and department
    host: str
    path: str  # the list page
    code: str  # BOARD_CODE ("" for the B group)
    encoding: str = "utf-8"  # "cp949" for EUC-KR sites (english.kw.ac.kr)
    server: str = SERVER_E1
    group: str = "A"

    @property
    def source(self) -> str:
        return f"dept-{self.key}"

    @property
    def category_id(self) -> str:
        return f"dept-{self.key}"

    @property
    def list_url(self) -> str:
        if self.group == "B":
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
    Board("archi", "건축공학과", "archi.kw.ac.kr", "/community/notice.php", "note", server=SERVER_E2),
    Board("chss", "인문사회과학대학", "chss.kw.ac.kr", "/notice/news.php", "notice", server=SERVER_E2),
    Board("korean", "국어국문학과", "korean.kw.ac.kr", "/community/notice.php", "notice", server=SERVER_E2),
    Board("english", "영어산업학과", "english.kw.ac.kr", "/new/bulletin/notice.php", "notice", "cp949",
          server=SERVER_E2),
    Board("psy", "산업심리학과", "psy.kw.ac.kr", "/community/notice.php", "notice", server=SERVER_E2),
    Board("dnaci", "동북아문화산업학부", "dnaci.kw.ac.kr", "/notice/news.php", "notice", server=SERVER_E2),
    Board("kwpa", "행정학과", "kwpa.kw.ac.kr", "/notice/faculty.php", "faculty", server=SERVER_E2),
    Board("biz", "경영대학", "biz.kw.ac.kr", "/community/notice.php", "note", server=SERVER_E2),  # 65 pinned
    Board("kwlife", "참빛인재대학", "kwlife.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("kwlaw", "금융부동산법무학과", "kwlaw.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("kwgame", "게임콘텐츠학과", "kwgame.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("kwsports", "스포츠상담재활학과", "kwsports.kw.ac.kr", "/notice/notice.php", "notice", server=SERVER_E3),
    Board("ingenium", "인제니움학부대학", "ingenium.kw.ac.kr", "/inform/notice.php", "notice", server=SERVER_E3),
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


def _empty_streak(table, server: str, step: int | None) -> int:
    """Consecutive answers from `server` that parsed to no rows; step None resets it."""
    key = {"PK": f"DEPTSERVER#{server}", "SK": "STATE"}
    if step is None:
        table.put_item(Item={**key, "empty_streak": 0})
        return 0
    item = table.update_item(Key=key, UpdateExpression="ADD empty_streak :one",
                             ExpressionAttributeValues={":one": step}, ReturnValues="UPDATED_NEW")
    return int(item["Attributes"]["empty_streak"])


def collect(now: datetime, table, fetch=fetch_list_page) -> dict[str, object]:
    """One request per server: {server: result}."""
    return {server: _collect_server(server, now, table, fetch) for server in dict.fromkeys(b.server for b in BOARDS)}


def _collect_server(server: str, now: datetime, table, fetch) -> dict[str, object]:
    permit = politeness.acquire(table, server, now, daily_cap=DAILY_CAP)
    if not permit.granted:
        log("dept_skip", server=server, reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}
    boards = [b for b in BOARDS if b.server == server]
    states = {b.key: table.get_item(Key=_state_key(b), ConsistentRead=True).get("Item") or {} for b in boards}
    board = min(boards, key=lambda b: int(states[b.key].get("checked_at", 0)))  # ties: list order
    state = states[board.key]
    table.update_item(Key=_state_key(board), UpdateExpression="SET checked_at = :now",
                      ExpressionAttributeValues={":now": int(now.timestamp())})

    result = fetch(board.list_url, encoding=board.encoding, errors="replace")
    records = parse_board(result.html, board) if result.html else []
    if result.block_reason:
        blocked = politeness.record_block(table, server, now, result.block_reason)
        log("dept_blocked", board=board.key, reason=result.block_reason, status=result.status, **blocked)
        metrics(DeptFetched=1, Blocked=1)
        return {"status": "blocked", "board": board.key, "reason": result.block_reason}
    if not records:  # this board's page changed; the server answered, so its other boards go on
        streak = _empty_streak(table, server, +1)
        log("dept_no_rows", board=board.key, status=result.status, streak=streak)
        metrics(DeptFetched=1, ParseEmpty=1)
        if streak >= len(boards):  # a whole round with nothing: likely a block page we don't recognise
            _empty_streak(table, server, None)
            politeness.record_block(table, server, now, "no_rows_everywhere")
        return {"status": "no_rows", "board": board.key}
    _empty_streak(table, server, None)

    seeded = bool(state.get("seeded_at"))
    high = int(state.get("max_uid", 0))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        quiet = not seeded or int(record.duid) <= high
        counts[store.upsert(table, store.notice_item(board.source, record, now, seed=quiet))] += 1
    table.update_item(
        Key=_state_key(board),
        UpdateExpression="SET seeded_at = if_not_exists(seeded_at, :now), max_uid = :max",
        ExpressionAttributeValues={":now": int(now.timestamp()), ":max": max(high, *(int(r.duid) for r in records))},
    )
    politeness.record_success(table, server)
    log("dept_ok", board=board.key, rows=len(records), seeded_now=not seeded, **counts)
    metrics(DeptFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "board": board.key, "rows": len(records), "seed_run": not seeded, **counts}

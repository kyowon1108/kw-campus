"""Three small outside boards, each on its own server, read on a light schedule: one page per board.

- 총동문회 (www.kwuaa.com, gnuboard board "notice"; the same server and board as http-only
  dongmoon.kw.ac.kr): about one post a month, every 4 hours. Rows are read by gnuboard.py, which dates
  the year-less "MM-DD" rows by wr_id order. The yearly 장학생 선발 call (late August) is a poster with
  hwp/pdf forms, so its notices are read for extraction like a 학과 board's (extract._PAGES: the post's
  body, poster and files, notice_assets). routing sends them to the 학과 channel: channels follow the
  category only, so its 장학 posts can't go to #장학 on their own.
- 총학생회 (www.kwu-studentcouncil.co.kr/notices, Next.js): one GET holds every post as JSON in the
  page's flight data (self.__next_f.push), read here without running any script; each post opens at
  /notices/<id>. Every 2 hours. The posts are short summaries whose details sit in a poster on Supabase
  storage: a new post's text, masked, rides on its page-queue row (council_reading; gone once read), and the
  page run reads it and the poster (notice_assets.read_thumbnail) with no request to this server. 일반 channel.
- 현장실습 (internship.kw.ac.kr, 현장실습지원팀): robots.txt allows /index.do only, so the home page's
  공지사항 widget (the 13 newest titles, links and dates) is all that is read, every 4 hours. Its notice
  pages, images and files are linked for students and never fetched: no _PAGES entry, and
  politeness.robots_allows refuses the rest of the host to every fetcher (the bridge's read_notice and
  the page runs' images and files too). 취업 channel.
행복기숙사 (kw.happydorm.or.kr) is not collected: its list comes from a POST, and the endpoint a 2023
student scraper used (/bbs/getBbsList.kmc) answered 301 to /bbs/getBbsList.do on 2026-11-07.

Each board is its own source ("SRC#<key>", category "office-<key>") and its own gate (its server, at
most DAILY_CAP requests a KST day, the list page and the page runs together). A list check waits its
board's `every` (minus the schedule's jitter): the 09:00, 13:00, 17:00 and 21:00 weekday runs for the
4-hour boards. The first answer only seeds. Then a post not stored before is new when it is recent
(config.NEW_POST_WINDOW) or, on a numbered board, above the highest number seen; others are stored
quietly. Also quiet: a post under a new id with the title and date of one already stored (총학생회's site
has re-issued every id at once before: its posts of 2026 carry ids minted on 2026-09-18), and every row of
a run where more than half the rows (at least 3) are ids never seen (a renumbered board, or one back after
the 120-day TTL). A row already stored keeps its stored dates on 총동문회, whose years are inferred and could
drift as the page scrolls; the page run corrects the posting date from the post's own full date
(extract.Page.dated). Failures follow the other collectors: 403/429/a challenge page, no answer, 5xx and an
empty page count for the server (politeness.record_block); a 404, a redirect or an odd page back off the
board alone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from boto3.dynamodb.conditions import Key
from bs4 import BeautifulSoup, NavigableString

from kwcampus import gnuboard, politeness, store
from kwcampus.config import KST, NEW_POST_WINDOW
from kwcampus.fetch import WORST_CASE_S, empty_reason, fetch_list_page
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.redaction import redact_body, redact_text
from kwcampus.kwnotice.values import DUID, SourceURL
from kwcampus.obs import log, metrics

# The schedule fires every 20 minutes; a few seconds of start jitter must not push a check a slot later.
SLACK = timedelta(minutes=5)
BACKOFF = timedelta(hours=2)  # a board's own failures: 2 h, 4 h, ... up to 24 h
THUMB_HOST = "lqptfesfsgpdjlzwxhbt.supabase.co"  # 총학생회's posters (notice_assets.THUMB_HOST)


@dataclass(frozen=True)
class SmallBoard:
    key: str  # source "<key>", category "office-<key>", public post ids "<key>-<id>"
    name: str  # what users see as the category and department
    host: str
    path: str  # the one page read
    server: str  # the politeness gate's key
    kind: str  # "gnuboard", "nextjs" or "widget": which parser reads the page
    every: timedelta
    daily_cap: int  # requests to the server a KST day: list checks (+ a spare) and, for 총동문회, page runs

    @property
    def source(self) -> str:
        return self.key

    @property
    def category_id(self) -> str:
        return f"office-{self.key}"

    @property
    def list_url(self) -> str:
        return f"https://{self.host}{self.path}"


# 총동문회: 4 list checks a weekday (+ a spare) leave 7 for page runs: a notice's page, 2 files and its poster.
ALUMNI = SmallBoard("alumni", "총동문회", "www.kwuaa.com", "/bbs/board.php?bo_table=notice", "222.122.39.21",
                    "gnuboard", timedelta(hours=4), 12)
COUNCIL = SmallBoard("council", "총학생회", "www.kwu-studentcouncil.co.kr", "/notices", "13.209.121.228",
                     "nextjs", timedelta(hours=2), 7)  # 6 checks a weekday (+ a spare)
INTERNSHIP = SmallBoard("internship", "현장실습", "internship.kw.ac.kr", "/index.do", "223.194.4.216",
                        "widget", timedelta(hours=4), 5)  # 4 checks a weekday (+ a spare); nothing else
BOARDS: tuple[SmallBoard, ...] = (ALUMNI, COUNCIL, INTERNSHIP)


def _summary(board: SmallBoard, duid: str, title: str, posted: date, url: str, pinned: bool = False,
             files: bool = False) -> NoticeSummary:
    return NoticeSummary(duid=DUID(duid), title=title, category_id=CategoryId(board.category_id),
                         category_name=CategoryName(board.name), posted_date=posted, updated_date=posted,
                         department=board.name, source_url=SourceURL(url), attachments_present=files, pinned=pinned)


# --- 총동문회: gnuboard ------------------------------------------------------------------------------

def parse_gnuboard(html: str, board: SmallBoard, today: date) -> list[NoticeSummary]:
    table = (parse_qs(urlsplit(board.path).query).get("bo_table") or [""])[0]
    return [_summary(board, str(r.wr_id), r.title, r.posted, gnuboard.post_url(board.host, table, r.wr_id),
                     r.pinned, r.files)
            for r in gnuboard.parse_list(html, board.host, table, today)]


# --- 총학생회: Next.js flight data ---------------------------------------------------------------------

_PUSH = re.compile(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", re.DOTALL)
_LISTS = re.compile(r'"(?:pinnedNotices|notices)":\[')
_POST_ID = re.compile(r"[a-z0-9]{8,40}")


def _flight(html: str) -> str:
    """The page's flight data stream: the text chunks of every self.__next_f.push, in order (a row may
    be split over two pushes)."""
    parts = []
    for raw in _PUSH.findall(html):
        try:
            chunk = json.loads(raw)
        except ValueError:
            continue
        if isinstance(chunk, list) and len(chunk) == 2 and chunk[0] == 1 and isinstance(chunk[1], str):
            parts.append(chunk[1])
    return "".join(parts)


def _day(value: object) -> date | None:
    """"2026-09-09T00:00:00" (the site's own local time, as it shows it) or React's "$D…" form; a time with
    a zone ("…Z", "+00:00") is taken to KST first."""
    text = str(value or "").removeprefix("$D")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return when.astimezone(KST).date() if when.tzinfo else when.date()


def council_posts(html: str) -> list[dict]:
    """Every post in the pinnedNotices and notices lists, once each, as the site gives it."""
    stream, decoder = _flight(html), json.JSONDecoder()
    seen, out = set(), []
    for match in _LISTS.finditer(stream):
        try:
            posts, _ = decoder.raw_decode(stream, match.end() - 1)
        except ValueError:
            continue
        for post in posts if isinstance(posts, list) else []:
            pid = str(post.get("id") or "") if isinstance(post, dict) else ""
            if _POST_ID.fullmatch(pid) and pid not in seen:
                seen.add(pid)
                out.append(post)
    return out


def parse_council(html: str, board: SmallBoard, today: date | None = None) -> list[NoticeSummary]:
    """id, title, publishedAt (else createdAt), isPinned, attachments of every post. The text, thumbnail and
    links are not stored on the notice (council_reading puts the masked text on its queue row)."""
    out = []
    for post in council_posts(html):
        title = redact_text(" ".join(str(post.get("title") or "").split()))[:300]
        posted = _day(post.get("publishedAt")) or _day(post.get("createdAt"))
        if not title or posted is None:
            continue
        pid = str(post["id"])
        out.append(_summary(board, pid, title, posted, f"https://{board.host}/notices/{pid}",
                            bool(post.get("isPinned")), bool(post.get("attachments"))))
    return out


def council_reading(post: dict) -> dict:
    """What a page run needs to read a 총학생회 post without another request: its text, masked like a page's
    body (names, phone numbers, e-mail addresses, IDs) and cut to extract's length, its poster's address when
    it is on the site's own Supabase storage, and how many links it carries (Google Drive files)."""
    thumb = str(post.get("thumbnailUrl") or "")
    links = post.get("attachments") if isinstance(post.get("attachments"), list) else []
    ours = thumb.startswith(f"https://{THUMB_HOST}/")
    return {"text": redact_body(str(post.get("content") or ""))[:6000], "thumb": thumb if ours else "",
            "other_images": int(bool(thumb) and not ours), "links": len(links)}


# --- 현장실습: the home page's notice widget ------------------------------------------------------------

_INTERNSHIP_LINK = re.compile(r"^/home/notice/view\.do\?bbsSeq=(\d{1,9})(?:&page=\d{1,4})?$")
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_internship(html: str, board: SmallBoard, today: date | None = None) -> list[NoticeSummary]:
    """The 공지사항 box: li > a ("[전체]"/"[학생]" in span > strong, then the title) and span.date (YYYY-MM-DD).
    The link is rebuilt from the number; it is for students to open, never fetched here."""
    box = BeautifulSoup(html, "html.parser").select_one(".notice_txt_w")
    seen, out = set(), []
    for li in box.find_all("li") if box else []:
        link, shown = li.find("a", href=True), li.select_one("span.date")
        match = _INTERNSHIP_LINK.match(link["href"].strip()) if link else None
        when = _ISO_DATE.search(shown.get_text(" ", strip=True)) if shown else None
        if match is None or when is None or match.group(1) in seen:
            continue
        lead = next((c for c in link.children if not (isinstance(c, NavigableString) and not c.strip())), None)
        if getattr(lead, "name", None) == "span" and re.fullmatch(r"\[.+\]", lead.get_text(strip=True)):
            lead.decompose()  # the leading audience label, [전체] or [학생]; any other span stays in the title
        title = redact_text(" ".join(link.get_text(" ").split()))[:300]
        try:
            posted = date(int(when.group(1)), int(when.group(2)), int(when.group(3)))
        except ValueError:
            continue
        if not title:
            continue
        seen.add(match.group(1))
        out.append(_summary(board, match.group(1), title, posted,
                            f"https://{board.host}/home/notice/view.do?bbsSeq={match.group(1)}"))
    return out


_PARSERS: dict[str, Callable[[str, SmallBoard, date], list[NoticeSummary]]] = {
    "gnuboard": parse_gnuboard, "nextjs": parse_council, "widget": parse_internship}


def parse(html: str, board: SmallBoard, today: date) -> list[NoticeSummary]:
    return _PARSERS[board.kind](html, board, today)


# --- the collector ------------------------------------------------------------------------------------

def _state_key(board: SmallBoard) -> dict[str, str]:
    return {"PK": store.source_pk(board.source), "SK": "STATE"}


def collect(now: datetime, table, fetch=fetch_list_page, clock: Callable[[], datetime] | None = None,
            time_left: Callable[[], float] | None = None) -> dict[str, object]:
    """At most one request per board (each on its own server): {board key: result}."""
    clock = clock or (lambda: now)
    out: dict[str, object] = {}
    for board in BOARDS:
        try:
            out[board.key] = _collect_board(board, now, table, fetch, clock, time_left)
        except Exception as e:  # noqa: BLE001 - one board's trouble must not cost the others their turn
            log("small_error", board=board.key, error=e.__class__.__name__)
            metrics(SmallBoardError=1)
            out[board.key] = {"status": "error"}
    return out


def _collect_board(board: SmallBoard, now: datetime, table, fetch, clock, time_left) -> dict[str, object]:
    state = table.get_item(Key=_state_key(board), ConsistentRead=True).get("Item") or {}
    if now.timestamp() - int(state.get("checked_at", 0)) < (board.every - SLACK).total_seconds():
        return {"status": "not_due"}
    if not politeness.robots_allows(board.list_url):
        return politeness.robots_skip(board.source, board.list_url)
    if politeness.source_waiting(state, now):
        return politeness.source_skipped(board.source, state)
    if time_left is not None and time_left() < WORST_CASE_S:
        return {"status": "skipped", "reason": "no_time"}
    permit = politeness.acquire(table, board.server, clock(), daily_cap=board.daily_cap, by=board.key)
    if not permit.granted:
        log("small_skip", board=board.key, reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}
    table.update_item(Key=_state_key(board), UpdateExpression="SET checked_at = :now",
                      ExpressionAttributeValues={":now": int(now.timestamp())})

    today = now.astimezone(KST).date()
    result = fetch(board.list_url)
    records = parse(result.html, board, today) if result.html else []
    reason = result.block_reason or (None if records else empty_reason(result.html))
    if reason in politeness.HARD or (reason and not politeness.is_server_signal(reason)):
        failed = politeness.source_failed(table, _state_key(board), state, now, reason, BACKOFF)
        log("small_board_failed", board=board.key, reason=reason, status=result.status, **failed)
    if reason and politeness.is_server_signal(reason):  # the board's only server: an empty page may be a block page
        blocked = politeness.record_block(table, board.server, clock(), reason, result.retry_after)
        log("small_blocked", board=board.key, reason=reason, status=result.status, **blocked)
        metrics(SmallBoardFetched=1, Blocked=int(reason in politeness.HARD),
                Transient=int(reason not in politeness.HARD), ParseEmpty=int(reason == "no_rows"))
        return {"status": "no_rows" if reason == "no_rows" else "blocked", "reason": reason}
    if reason:  # 404, a redirect, an unexpected page: this board waits (2 h, 4 h, ...)
        metrics(SmallBoardFetched=1)
        return {"status": "failed", "reason": reason}

    seeded = bool(state.get("seeded_at"))
    high = int(state.get("max_uid", 0))
    numbers = [int(r.duid) for r in records if str(r.duid).isdigit()]
    stored = _stored(table, board)
    extras = {str(p["id"]): p for p in council_posts(result.html)} if board.kind == "nextjs" else {}
    known = {(i.get("title_key"), i.get("posted_date")) for i in stored.values()}
    unseen = sum(str(r.duid) not in stored for r in records)
    churn = seeded and unseen >= 3 and 2 * unseen > len(records)
    if churn:  # most ids are new at once: the site re-issued them, or everything came back after the TTL
        log("small_ids_churned", board=board.key, unseen=unseen, rows=len(records))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "twins": 0}
    for record in records:
        old = stored.get(str(record.duid))
        if old and board.kind == "gnuboard":  # the year was inferred: keep what was stored (and maybe corrected)
            record = _with_dates(record, old)
        newer = str(record.duid).isdigit() and int(record.duid) > high
        twin = old is None and (store.title_key(record.title), record.posted_date.isoformat()) in known
        counts["twins"] += twin
        quiet = not seeded or churn or twin or not (newer or today - record.posted_date <= NEW_POST_WINDOW)
        outcome = store.upsert(table, store.notice_item(board.source, record, now, seed=quiet))
        counts[outcome] += 1
        if board.kind == "nextjs" and outcome != "unchanged" and str(record.duid) in extras:
            store.queue_extra(table, store.notice_item(board.source, record, now, seed=quiet),
                              council_reading(extras[str(record.duid)]))
    page_max = max(numbers, default=0)
    if seeded and numbers and page_max < high:  # the newest post is gone, or the board renumbered
        log("small_uid_regressed", board=board.key, page_max=page_max, high=high)
        metrics(UidRegressed=1)
    table.update_item(
        Key=_state_key(board),
        UpdateExpression="SET seeded_at = if_not_exists(seeded_at, :now), max_uid = :max",
        ExpressionAttributeValues={":now": int(now.timestamp()),
                                   ":max": (page_max if seeded and numbers else max(high, page_max))},
    )
    politeness.record_success(table, board.server)
    politeness.source_ok(table, _state_key(board), state)
    log("small_ok", board=board.key, rows=len(records), seeded_now=not seeded, **counts)
    metrics(SmallBoardFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "rows": len(records), "seed_run": not seeded, **counts}


def _stored(table, board: SmallBoard) -> dict[str, dict]:
    """The board's stored notices by post id: their title key and dates only."""
    names = {"#p": "post_id", "#k": "title_key", "#d": "posted_date", "#u": "updated_date"}
    kwargs = {"KeyConditionExpression": Key("PK").eq(store.source_pk(board.source)) & Key("SK").begins_with("N#"),
              "ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}
    out: dict[str, dict] = {}
    while True:
        page = table.query(**kwargs)
        out.update({str(i.get("post_id")): i for i in page["Items"]})
        if "LastEvaluatedKey" not in page:
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _with_dates(record: NoticeSummary, stored: dict) -> NoticeSummary:
    try:
        return replace(record, posted_date=date.fromisoformat(str(stored["posted_date"])),
                       updated_date=date.fromisoformat(str(stored["updated_date"])))
    except (KeyError, ValueError):
        return record

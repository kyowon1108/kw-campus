"""www.kw.ac.kr 취업게시판 (/ko/life/job.jsp) and 종합 FAQ (/ko/life/faq.jsp).

Both use the main notice board's list layout (li rows in .board-list-box, "[분류]" in
strong.category, p.info "조회수 | 작성일 | 수정일 | 부서"), but with their own labels ([취업관련],
team names, sometimes none), which the vendored kwnotice parser rejects as unknown categories.
So the rows are read here with kwnotice's helpers and get this board's category.

They share www's gate and daily cap with the main board, so they are checked rarely: the job board
at most every 2 hours, the FAQ at most once a day, never both in one run. The main board has just
used the run's www request, so the check first waits out the 60 s gap (as the campus Lambda does).
www budget per weekday: main board 30 + job 5 + FAQ 1 + campus <= 3, under the cap of 40.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bs4 import BeautifulSoup

from kwcampus import politeness, store
from kwcampus.fetch import fetch_list_page
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.parsing_shared import attribute, direct_text, parse_date, text
from kwcampus.kwnotice.redaction import redact_human_fields
from kwcampus.kwnotice.source import NOTICE_HOST, NOTICE_ORIGIN, duid_from_href
from kwcampus.kwnotice.values import SourceURL
from kwcampus.obs import log, metrics

DAILY_CAP = 44  # www's budget, shared with the main board and the campus Lambda (collector.DAILY_CAP)
# The schedule fires every 20 minutes; a few seconds of start jitter must not push a check a slot later.
SLACK = timedelta(minutes=5)


@dataclass(frozen=True)
class WwwBoard:
    source: str
    path: str
    category_id: str
    name: str  # the category label users see
    every: timedelta

    @property
    def list_url(self) -> str:
        return f"{NOTICE_ORIGIN}{self.path}"

    def post_url(self, duid: str) -> str:
        return f"{NOTICE_ORIGIN}{self.path}?BoardMode=view&DUID={duid}"


BOARDS: tuple[WwwBoard, ...] = (
    WwwBoard("kw-job", "/ko/life/job.jsp", "job", "취업", timedelta(hours=2)),
    WwwBoard("kw-faq", "/ko/life/faq.jsp", "faq", "FAQ", timedelta(days=1)),
)


def parse_board(html: str, board: WwwBoard) -> list[NoticeSummary]:
    soup = BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for row in soup.select(".board-list-box li"):
        link = row.select_one("a[href*='DUID']")
        duid = duid_from_href(attribute(link, "href"))
        info = [part.strip() for part in text(row.select_one("p.info")).split("|")]
        posted, _ = parse_date(info[1] if len(info) > 1 else "", "posted")
        if duid is None or posted is None or duid in seen:
            continue
        updated, _ = parse_date(info[2] if len(info) > 2 else "", "updated")
        fields = redact_human_fields(title=direct_text(link), category_name=board.name,
                                     department=info[3] if len(info) > 3 else "", body="")
        if not fields.title:
            continue
        seen.add(duid)
        out.append(NoticeSummary(
            duid=duid, title=fields.title, category_id=CategoryId(board.category_id),
            category_name=CategoryName(board.name), posted_date=posted, updated_date=updated or posted,
            department=fields.department, source_url=SourceURL(board.post_url(duid)),
            attachments_present=row.select_one(".ico-file") is not None,
            pinned="notice" in " ".join(row.get("class") or [])))
    return out


def _state_key(board: WwwBoard) -> dict[str, str]:
    return {"PK": store.source_pk(board.source), "SK": "STATE"}


def collect(now: datetime, table, fetch=fetch_list_page, clock: Callable[[], datetime] | None = None,
            sleep: Callable[[float], None] = time.sleep,
            time_left: Callable[[], float] | None = None) -> dict[str, object]:
    """At most one www request: the first board that is due. time_left (seconds of Lambda time)
    lets a run that is already late skip the 60 s wait instead of being cut off mid-request."""
    clock = clock or (lambda: datetime.now(UTC))
    states = {b.source: table.get_item(Key=_state_key(b), ConsistentRead=True).get("Item") or {} for b in BOARDS}
    board = next((b for b in BOARDS if now.timestamp() - int(states[b.source].get("checked_at", 0))
                  >= (b.every - SLACK).total_seconds()), None)
    if board is None:
        return {"status": "not_due"}
    permit = politeness.acquire(table, NOTICE_HOST, clock(), daily_cap=DAILY_CAP)
    if permit.reason == "too_soon":  # the main board's request a moment ago: really wait, never two in a row
        if time_left is not None and time_left() < politeness.MIN_INTERVAL.total_seconds() + 30:
            return {"status": "skipped", "board": board.source, "reason": "no_time"}
        sleep(politeness.MIN_INTERVAL.total_seconds() + 1)
        permit = politeness.acquire(table, NOTICE_HOST, clock(), daily_cap=DAILY_CAP)
    if not permit.granted:
        log("www_skip", board=board.source, reason=permit.reason)
        return {"status": "skipped", "board": board.source, "reason": permit.reason}
    table.update_item(Key=_state_key(board), UpdateExpression="SET checked_at = :now",
                      ExpressionAttributeValues={":now": int(now.timestamp())})

    result = fetch(board.list_url)
    records = parse_board(result.html, board) if result.html else []
    if result.block_reason:
        blocked = politeness.record_block(table, NOTICE_HOST, clock(), result.block_reason)
        log("www_blocked", board=board.source, reason=result.block_reason, status=result.status, **blocked)
        metrics(WwwBoardFetched=1, Blocked=1)
        return {"status": "blocked", "board": board.source, "reason": result.block_reason}
    if not records:  # a layout change on this page must not put the main board (same gate) on hold
        log("www_no_rows", board=board.source, status=result.status)
        metrics(WwwBoardFetched=1, ParseEmpty=1)
        return {"status": "no_rows", "board": board.source}

    seeded = bool(states[board.source].get("seeded_at"))
    # www DUIDs only grow: one at or below the highest seen is an old post coming back (after the
    # 120-day TTL, say), stored quietly instead of announced again.
    high = int(states[board.source].get("max_duid", 0))
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        quiet = not seeded or int(record.duid) <= high
        counts[store.upsert(table, store.notice_item(board.source, record, now, seed=quiet))] += 1
    table.update_item(Key=_state_key(board), UpdateExpression="SET max_duid = :max",
                      ExpressionAttributeValues={":max": max(high, *(int(r.duid) for r in records))})
    if not seeded:
        store.mark_seeded(table, board.source, now)
    politeness.record_success(table, NOTICE_HOST)
    log("www_ok", board=board.source, rows=len(records), seeded_now=not seeded, **counts)
    metrics(WwwBoardFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "board": board.source, "rows": len(records), "seed_run": not seeded, **counts}

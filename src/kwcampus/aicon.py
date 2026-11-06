"""인공지능융합대학 notices: the college home page's "대학 공지" widget.

One GET of https://aicon.kw.ac.kr/main/main.php shows the five newest undergraduate notices of
all four schools (컴퓨터정보공학부, 소프트웨어학부, 정보융합학부, 로봇학부). The schools' own
boards are no better: their first pages are all pinned posts, so new posts only show up here.
ce/cs/ic/cni are CNAMEs of this host, and kwlife, kwlaw, kwgame, kwsports and ingenium sit on the
same outside server (E3), so the politeness gate is that server's, shared with deptboards: one
request per run. The widget goes first and takes every other run; deptboards reads E3's boards in
the runs between.

Items go into kw-public like the main board (PK "SRC#aicon"), so search, the public MCP and the
Discord dispatcher pick them up with no extra wiring.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from kwcampus import deptboards, politeness, store
from kwcampus.fetch import fetch_list_page
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.redaction import redact_text
from kwcampus.kwnotice.values import DUID, SourceURL
from kwcampus.obs import log, metrics

SOURCE = "aicon"
HOST = "aicon.kw.ac.kr"  # the gate's key until E3's boards joined; moved to SERVER on the first run
SERVER = deptboards.SERVER_E3
WIDGET_URL = "https://aicon.kw.ac.kr/main/main.php"
CATEGORY_ID = "dept-aicon"
CATEGORY_NAME = "인공지능융합대학"
SCHOOLS = ("컴퓨터정보공학부", "소프트웨어학부", "정보융합학부", "로봇학부")
# Every other 20-minute run; a few seconds of start jitter must not push a check a run later.
EVERY = timedelta(minutes=40)
SLACK = timedelta(minutes=5)
_NOTICE_LINK = re.compile(r"administration/notice\.php\?admin_mode=read&no=(\d+)")
_DATE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\s*$")


def parse_widget(html: str) -> list[NoticeSummary]:
    """The "대학 공지" tab's rows: department prefix, title, date (YYYY.MM.DD)."""
    soup = BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        match = _NOTICE_LINK.search(a["href"])
        if not match or match.group(1) in seen:
            continue
        row = a.find_parent("li") or a
        text = re.sub(r"\s+", " ", row.get_text(" ")).strip()
        when = _DATE.search(text)
        if not when:
            continue
        text = text[: when.start()].strip()
        school = next((s for s in SCHOOLS if text.startswith(s)), "")
        title = redact_text(text[len(school):].strip() if school else text)[:300]
        if not title:
            continue
        seen.add(match.group(1))
        posted = date(int(when.group(1)), int(when.group(2)), int(when.group(3)))
        out.append(NoticeSummary(
            duid=DUID(match.group(1)), title=title, category_id=CategoryId(CATEGORY_ID),
            category_name=CategoryName(CATEGORY_NAME), posted_date=posted, updated_date=posted,
            department=school or CATEGORY_NAME,
            source_url=SourceURL(urljoin(WIDGET_URL, a["href"])), attachments_present=False, pinned=False))
    return out


def _state_key() -> dict[str, str]:
    return {"PK": store.source_pk(SOURCE), "SK": "STATE"}


def collect(now: datetime, table, fetch=fetch_list_page) -> dict[str, object]:
    """One polite request every other run; same seeding and change detection as the main board."""
    politeness.move(table, HOST, SERVER)
    state = table.get_item(Key=_state_key(), ConsistentRead=True).get("Item") or {}
    if now.timestamp() - int(state.get("checked_at", 0)) < (EVERY - SLACK).total_seconds():
        return {"status": "not_due"}  # this run's E3 request goes to a department board
    permit = politeness.acquire(table, SERVER, now, daily_cap=deptboards.DAILY_CAP)
    if not permit.granted:
        log("aicon_skip", reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}
    table.update_item(Key=_state_key(), UpdateExpression="SET checked_at = :now",
                      ExpressionAttributeValues={":now": int(now.timestamp())})
    result = fetch(WIDGET_URL)
    records = parse_widget(result.html) if result.html else []
    if result.block_reason:
        blocked = politeness.record_block(table, SERVER, now, result.block_reason)
        log("aicon_blocked", reason=result.block_reason, status=result.status, **blocked)
        metrics(AiconFetched=1, Blocked=1)
        return {"status": "blocked", "reason": result.block_reason}
    if not records:  # the widget changed; the server answered, so E3's boards go on
        log("aicon_no_rows", status=result.status)
        metrics(AiconFetched=1, ParseEmpty=1)
        return {"status": "no_rows"}
    seeded = store.is_seeded(table, SOURCE)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        counts[store.upsert(table, store.notice_item(SOURCE, record, now, seed=not seeded))] += 1
    if not seeded:
        store.mark_seeded(table, SOURCE, now)
    politeness.record_success(table, SERVER)
    log("aicon_ok", rows=len(records), seeded_now=not seeded, **counts)
    metrics(AiconFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "rows": len(records), "seed_run": not seeded, **counts}

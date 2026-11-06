"""인공지능융합대학 notices: the college home page's "대학 공지" widget.

One GET of https://aicon.kw.ac.kr/main/main.php shows the five newest undergraduate notices of
all four schools (컴퓨터정보공학부, 소프트웨어학부, 정보융합학부, 로봇학부). The schools' own
boards are no better: their first pages are all pinned posts, so new posts only show up here.
ce/cs/ic/cni are CNAMEs of this host, and kwlife, kwlaw, kwgame, kwsports and ingenium sit on the
same outside server (E3), so the politeness gate is that server's, shared with deptboards: one
request per run. The widget and E3's boards take turns by who asked E3 last (the gate's last_by),
not by the clock, so the 21:00 run alternates too. If the boards let two turns pass (all backing
off, say), the widget goes anyway.

Only server signals (403, 429, a challenge page, no answer, 5xx) count against E3. A broken widget
(404, a redirect, a changed page) backs off the widget alone: 40 min, 80 min, ... up to 24 h.

Items go into kw-public like the main board (PK "SRC#aicon"), so search, the public MCP and the
Discord dispatcher pick them up with no extra wiring.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from kwcampus import deptboards, politeness, store
from kwcampus.fetch import WORST_CASE_S, empty_reason, fetch_list_page
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
# Turns the widget gives E3's boards before it goes anyway (they may all be backing off).
MAX_YIELDS = 2
BACKOFF = timedelta(minutes=40)  # the widget's own failures: 40 min, 80 min, ... up to 24 h
_NOTICE_LINK = re.compile(r"^(?:https://aicon\.kw\.ac\.kr)?/?(?:\.\./)*administration/notice\.php\?admin_mode=read&no=(\d{1,12})$")
_DATE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\s*$")


def parse_widget(html: str) -> list[NoticeSummary]:
    """The "대학 공지" tab's rows: department prefix, title, date (YYYY.MM.DD)."""
    soup = BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        target = urlsplit(urljoin(WIDGET_URL, href))
        match = _NOTICE_LINK.match(href)
        if not match or target.scheme != "https" or target.hostname != HOST or match.group(1) in seen:
            continue  # only links to this site's own notices; the URL is rebuilt from the number
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
        try:
            posted = date(int(when.group(1)), int(when.group(2)), int(when.group(3)))
        except ValueError:  # 2026.02.30
            continue
        seen.add(match.group(1))
        out.append(NoticeSummary(
            duid=DUID(match.group(1)), title=title, category_id=CategoryId(CATEGORY_ID),
            category_name=CategoryName(CATEGORY_NAME), posted_date=posted, updated_date=posted,
            department=school or CATEGORY_NAME,
            source_url=SourceURL(f"https://{HOST}/administration/notice.php?admin_mode=read&no={match.group(1)}"),
            attachments_present=False, pinned=False))
    return out


def _state_key() -> dict[str, str]:
    return {"PK": store.source_pk(SOURCE), "SK": "STATE"}


def collect(now: datetime, table, fetch=fetch_list_page, clock: Callable[[], datetime] | None = None,
            time_left: Callable[[], float] | None = None) -> dict[str, object]:
    """One polite request when it is the widget's turn; same seeding and change detection as the main board."""
    clock = clock or (lambda: now)
    politeness.move(table, HOST, SERVER)
    state = table.get_item(Key=_state_key(), ConsistentRead=True).get("Item") or {}
    if not politeness.robots_allows(WIDGET_URL):
        return politeness.robots_skip(SOURCE, WIDGET_URL)
    if politeness.source_waiting(state, now):
        return politeness.source_skipped(SOURCE, state)
    if politeness.state(table, SERVER).get("last_by") == "aicon" and int(state.get("yielded", 0)) < MAX_YIELDS:
        table.update_item(Key=_state_key(), UpdateExpression="ADD yielded :one", ExpressionAttributeValues={":one": 1})
        return {"status": "not_due"}  # this run's E3 request goes to a department board
    if time_left is not None and time_left() < WORST_CASE_S:
        return {"status": "skipped", "reason": "no_time"}
    permit = politeness.acquire(table, SERVER, clock(), daily_cap=deptboards.DAILY_CAP, by="aicon")
    if not permit.granted:
        log("aicon_skip", reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}
    table.update_item(Key=_state_key(), UpdateExpression="SET checked_at = :now, yielded = :zero",
                      ExpressionAttributeValues={":now": int(now.timestamp()), ":zero": 0})
    result = fetch(WIDGET_URL)
    records = parse_widget(result.html) if result.html else []
    reason = result.block_reason or (None if records else empty_reason(result.html))
    if reason in politeness.HARD or (reason and not politeness.is_server_signal(reason)):
        failed = politeness.source_failed(table, _state_key(), state, now, reason, BACKOFF)
        log("aicon_failed", reason=reason, status=result.status, **failed)
    if reason in politeness.HARD or reason in ("transport_failure", "server_error", "slow"):
        blocked = politeness.record_block(table, SERVER, clock(), reason, result.retry_after)
        log("aicon_blocked", reason=reason, status=result.status, **blocked)
        metrics(AiconFetched=1, Blocked=int(reason in politeness.HARD), Transient=int(reason not in politeness.HARD))
        return {"status": "blocked", "reason": reason}
    if reason == "no_rows":  # the widget changed, or a block page: one more empty answer from E3
        streak = deptboards.note_empty(table, SERVER, clock())
        log("aicon_no_rows", status=result.status, streak=streak)
        metrics(AiconFetched=1, ParseEmpty=1)
        return {"status": "no_rows"}
    if reason:  # 404, a redirect, an unexpected page: the widget waits, E3's boards go on
        metrics(AiconFetched=1, ParseEmpty=1)
        return {"status": "failed", "reason": reason}
    deptboards.note_answer(table, SERVER)
    seeded = store.is_seeded(table, SOURCE)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        counts[store.upsert(table, store.notice_item(SOURCE, record, now, seed=not seeded))] += 1
    if not seeded:
        store.mark_seeded(table, SOURCE, now)
    politeness.record_success(table, SERVER)
    politeness.source_ok(table, _state_key(), state)
    log("aicon_ok", rows=len(records), seeded_now=not seeded, **counts)
    metrics(AiconFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "rows": len(records), "seed_run": not seeded, **counts}

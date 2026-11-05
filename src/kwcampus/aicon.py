"""인공지능융합대학 notices: the college home page's "대학 공지" widget.

One GET of https://aicon.kw.ac.kr/main/main.php shows the five newest undergraduate notices of
all four schools (컴퓨터정보공학부, 소프트웨어학부, 정보융합학부, 로봇학부). The schools' own
boards are no better: their first pages are all pinned posts, so new posts only show up here.
ce/cs/ic/cni are CNAMEs of this host (external hosting), so the politeness gate is per this host.

Items go into kw-public like the main board (PK "SRC#aicon"), so search, the public MCP and the
Discord dispatcher pick them up with no extra wiring.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from kwcampus import politeness, store
from kwcampus.fetch import fetch_list_page
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.redaction import redact_text
from kwcampus.kwnotice.values import DUID, SourceURL
from kwcampus.obs import log, metrics

SOURCE = "aicon"
HOST = "aicon.kw.ac.kr"
WIDGET_URL = "https://aicon.kw.ac.kr/main/main.php"
CATEGORY_ID = "dept-aicon"
CATEGORY_NAME = "인공지능융합대학"
SCHOOLS = ("컴퓨터정보공학부", "소프트웨어학부", "정보융합학부", "로봇학부")
DAILY_CAP = 40
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


def collect(now: datetime, table, fetch=fetch_list_page) -> dict[str, object]:
    """One polite request; same seeding and change detection as the main board."""
    permit = politeness.acquire(table, HOST, now, daily_cap=DAILY_CAP)
    if not permit.granted:
        log("aicon_skip", reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}
    result = fetch(WIDGET_URL)
    records = parse_widget(result.html) if result.html else []
    if result.block_reason or not records:
        reason = result.block_reason or "no_rows"
        state = politeness.record_block(table, HOST, now, reason)
        log("aicon_blocked", reason=reason, status=result.status, **state)
        metrics(AiconFetched=1, Blocked=1)
        return {"status": "blocked", "reason": reason}
    seeded = store.is_seeded(table, SOURCE)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        counts[store.upsert(table, store.notice_item(SOURCE, record, now, seed=not seeded))] += 1
    if not seeded:
        store.mark_seeded(table, SOURCE, now)
    politeness.record_success(table, HOST)
    log("aicon_ok", rows=len(records), seeded_now=not seeded, **counts)
    metrics(AiconFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "rows": len(records), "seed_run": not seeded, **counts}

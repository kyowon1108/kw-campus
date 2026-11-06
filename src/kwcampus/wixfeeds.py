"""Department news from Wix blog RSS feeds: 건축학과 and 미디어커뮤니케이션학부.

Both sites are on Wix, whose CDN addresses are shared by every Wix site, so the gate is per host
name here: one request per host per run, each feed at most every 2 hours (robots.txt allows it).
www.envkw.com (환경공학과) is a Wix site too but has no /blog-feed.xml (404 on 2026-11-06).

An <item> gives the Wix post id (guid, 24 hex digits), title, link and pubDate. The feed lists
the newest 20 posts, oldest from years ago, so newness is by pubDate: published after the newest
item seen before means new; an older one (back after the 120-day TTL, say) is stored quietly.
A pubDate ahead of the run (Wix lets authors edit it) is announced but never moves that mark past
the run's own time, so it cannot silence the real new posts until its date; one more than a day
ahead is logged (wix_future_date).
Each feed is its own source ("SRC#dept-<key>"), and the first fetch only seeds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from kwcampus import politeness, store
from kwcampus.config import KST
from kwcampus.fetch import WORST_CASE_S, fetch_list_page
from kwcampus.kwnotice.domain import CategoryId, CategoryName, NoticeSummary
from kwcampus.kwnotice.redaction import redact_text
from kwcampus.kwnotice.values import DUID, SourceURL
from kwcampus.obs import log, metrics

EVERY = timedelta(hours=2)
# The schedule fires every 20 minutes; a few seconds of start jitter must not push a check a slot later.
SLACK = timedelta(minutes=5)
DAILY_CAP = 7  # every 2 hours over the 09:00-18:40 runs is 5 a day, plus the 21:00 run (+ a spare)
FUTURE_SLACK = timedelta(days=1)
TYPES = ("application/rss+xml", "application/xml", "text/xml")
_GUID = re.compile(r"[0-9a-f]{24}")


@dataclass(frozen=True)
class Feed:
    key: str  # source and category id are "dept-<key>"
    name: str  # what users see as the category and department
    host: str

    @property
    def source(self) -> str:
        return f"dept-{self.key}"

    @property
    def category_id(self) -> str:
        return f"dept-{self.key}"

    @property
    def url(self) -> str:
        return f"https://{self.host}/blog-feed.xml"


FEEDS: tuple[Feed, ...] = (
    Feed("architecture", "건축학과", "www.kwuarchitecture.com"),
    Feed("media", "미디어커뮤니케이션학부", "www.kwmedia.info"),
)


def _published(value: str | None) -> datetime | None:
    try:
        when = parsedate_to_datetime(value or "")
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def parse_feed(xml: str, feed: Feed) -> list[tuple[int, NoticeSummary]]:
    """(published at, epoch seconds; record) per item. The date shown is the KST day."""
    try:
        channel = ElementTree.fromstring(xml).find("channel")
    except ElementTree.ParseError:
        return []
    if channel is None:
        return []
    seen, out = set(), []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        link = (item.findtext("link") or "").strip()
        title = redact_text(" ".join((item.findtext("title") or "").split()))[:300]
        when = _published(item.findtext("pubDate"))
        if not _GUID.fullmatch(guid) or not link.startswith(f"https://{feed.host}/") or not title or when is None:
            continue
        if guid in seen:
            continue
        seen.add(guid)
        posted = when.astimezone(KST).date()
        out.append((int(when.timestamp()), NoticeSummary(
            duid=DUID(guid), title=title, category_id=CategoryId(feed.category_id),
            category_name=CategoryName(feed.name), posted_date=posted, updated_date=posted,
            department=feed.name, source_url=SourceURL(link), attachments_present=False, pinned=False)))
    return out


def _state_key(feed: Feed) -> dict[str, str]:
    return {"PK": store.source_pk(feed.source), "SK": "STATE"}


def collect(now: datetime, table, fetch=fetch_list_page, clock: Callable[[], datetime] | None = None,
            time_left: Callable[[], float] | None = None) -> dict[str, object]:
    """At most one request per feed host: {feed key: result}."""
    clock = clock or (lambda: now)
    out: dict[str, object] = {}
    for feed in FEEDS:
        try:
            out[feed.key] = _collect_feed(feed, now, table, fetch, clock, time_left)
        except Exception as e:  # noqa: BLE001 - one feed's trouble must not cost the other its turn
            log("wix_error", feed=feed.key, error=e.__class__.__name__)
            out[feed.key] = {"status": "error"}
    return out


def _collect_feed(feed: Feed, now: datetime, table, fetch, clock, time_left) -> dict[str, object]:
    state = table.get_item(Key=_state_key(feed), ConsistentRead=True).get("Item") or {}
    if now.timestamp() - int(state.get("checked_at", 0)) < (EVERY - SLACK).total_seconds():
        return {"status": "not_due"}
    if not politeness.robots_allows(feed.url):
        return politeness.robots_skip(feed.source, feed.url)
    if politeness.source_waiting(state, now):
        return politeness.source_skipped(feed.source, state)
    if time_left is not None and time_left() < WORST_CASE_S:
        return {"status": "skipped", "reason": "no_time"}
    permit = politeness.acquire(table, feed.host, clock(), daily_cap=DAILY_CAP)
    if not permit.granted:
        log("wix_skip", feed=feed.key, reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}
    table.update_item(Key=_state_key(feed), UpdateExpression="SET checked_at = :now",
                      ExpressionAttributeValues={":now": int(now.timestamp())})

    result = fetch(feed.url, types=TYPES)
    items = parse_feed(result.html, feed) if result.html else []
    reason = result.block_reason
    if reason in politeness.HARD or (reason and not politeness.is_server_signal(reason)):
        politeness.source_failed(table, _state_key(feed), state, now, reason, EVERY)
    if reason in politeness.HARD or reason in ("transport_failure", "server_error", "slow"):
        blocked = politeness.record_block(table, feed.host, clock(), reason, result.retry_after)
        log("wix_blocked", feed=feed.key, reason=reason, status=result.status, **blocked)
        metrics(WixFetched=1, Blocked=int(reason in politeness.HARD), Transient=int(reason not in politeness.HARD))
        return {"status": "blocked", "reason": reason}
    if reason:  # 404, a redirect, not a feed: this feed waits (2 h, 4 h, ...)
        log("wix_failed", feed=feed.key, reason=reason, status=result.status)
        metrics(WixFetched=1)
        return {"status": "failed", "reason": reason}
    if not items:
        log("wix_no_rows", feed=feed.key, status=result.status)
        metrics(WixFetched=1, ParseEmpty=1)
        return {"status": "no_rows"}

    seeded = bool(state.get("seeded_at"))
    high = int(state.get("max_published", 0))
    cap = int(now.timestamp())
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for published, record in items:
        if published > int((now + FUTURE_SLACK).timestamp()):
            log("wix_future_date", feed=feed.key, post=str(record.duid), days_ahead=(published - int(now.timestamp())) // 86400)
        quiet = not seeded or published <= high
        counts[store.upsert(table, store.notice_item(feed.source, record, now, seed=quiet))] += 1
    table.update_item(
        Key=_state_key(feed),
        UpdateExpression="SET seeded_at = if_not_exists(seeded_at, :now), max_published = :max",
        ExpressionAttributeValues={":now": int(now.timestamp()),
                                   ":max": max(min(high, cap), *(min(p, cap) for p, _ in items))},
    )
    politeness.record_success(table, feed.host)
    politeness.source_ok(table, _state_key(feed), state)
    log("wix_ok", feed=feed.key, rows=len(items), seeded_now=not seeded, **counts)
    metrics(WixFetched=1, Inserted=counts["inserted"])
    return {"status": "ok", "rows": len(items), "seed_run": not seeded, **counts}

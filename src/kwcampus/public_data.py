"""Read-side queries over kw-public for the public MCP tools and JSON API.

All items of one source live in one partition, so a single Query (a few hundred items
at most, thanks to the 120-day TTL) is enough; filtering happens in memory.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import date

from boto3.dynamodb.conditions import Key

from kwcampus.config import NOTICE_SOURCE
from kwcampus.routing import CATEGORY_LABEL

_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_S = 60

_LABEL_TO_ID = {label: cid for cid, label in CATEGORY_LABEL.items()}
_ALIASES = {"장학": "registration-scholarship", "등록": "registration-scholarship", "취업": "external",
            "대외활동": "external", "국제": "international-exchange"}


def category_id(value: str | None) -> str | None:
    """Accept an id ("academic"), a label ("학사") or a common alias ("장학")."""
    if not value:
        return None
    value = value.strip()
    if value in CATEGORY_LABEL:
        return value
    return _LABEL_TO_ID.get(value) or _ALIASES.get(value)


def _query_all(table, pk: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with("N#")}
    while True:
        page = table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def notices(table, source: str = NOTICE_SOURCE) -> list[dict]:
    hit = _CACHE.get(source)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    rows = [public_view(i) for i in _query_all(table, f"SRC#{source}")]
    rows.sort(key=lambda r: (r["posted_date"], r["post_id"]), reverse=True)
    _CACHE[source] = (time.monotonic(), rows)
    return rows


def public_view(item: dict) -> dict:
    """Only fields meant for readers; internal flags (seed, hashes, ttl) stay in the table."""
    cid = str(item.get("category_id", ""))
    return {
        "post_id": str(item.get("post_id") or str(item["SK"])[2:]),
        "title": str(item.get("title", "")),
        "category": CATEGORY_LABEL.get(cid, cid),
        "category_id": cid,
        "department": str(item.get("department", "")),
        "posted_date": str(item.get("posted_date", "")),
        "updated_date": str(item.get("updated_date", "")),
        "pinned": bool(item.get("pinned")),
        "url": str(item.get("url", "")),
    }


def _filter(rows: Iterable[dict], category: str | None, since: date | None) -> list[dict]:
    cid = category_id(category)
    out = []
    for r in rows:
        if cid and r["category_id"] != cid:
            continue
        if since and r["posted_date"] < since.isoformat():
            continue
        out.append(r)
    return out


def latest(table, category: str | None = None, limit: int = 10, include_pinned: bool = True) -> list[dict]:
    rows = _filter(notices(table), category, None)
    if not include_pinned:
        rows = [r for r in rows if not r["pinned"]]
    return rows[: max(1, min(limit, 50))]


def search(table, query: str, category: str | None = None, since: date | None = None, limit: int = 20) -> list[dict]:
    terms = [t.casefold() for t in query.split() if t.strip()]
    rows = _filter(notices(table), category, since)
    hits = [r for r in rows if all(t in (r["title"] + " " + r["department"]).casefold() for t in terms)]
    return hits[: max(1, min(limit, 50))]


def get(table, post_id: str) -> dict | None:
    return next((r for r in notices(table) if r["post_id"] == str(post_id).strip()), None)


def categories(table) -> list[dict]:
    counts: dict[str, int] = {}
    for r in notices(table):
        counts[r["category_id"]] = counts.get(r["category_id"], 0) + 1
    return [{"category_id": cid, "category": label, "count": counts.get(cid, 0)} for cid, label in CATEGORY_LABEL.items()]


def clear_cache() -> None:
    _CACHE.clear()

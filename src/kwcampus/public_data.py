"""Read-side queries over kw-public for the public MCP tools and JSON API.

All items of one source live in one partition, so a single Query (a few hundred items
at most, thanks to the 120-day TTL) is enough; filtering happens in memory.

What goes out is masked once more on the way (redaction.redact_text), so a title stored before
the current redaction version never shows a name. Expired items (ttl passed, the DynamoDB sweep
can lag by days) and items taken down by hand are never served. The same notice cross-posted by
several departments comes back once, with the other copies' post ids in "also".
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import date

from boto3.dynamodb.conditions import Key

from kwcampus import deptboards, wixfeeds, wwwboards
from kwcampus.config import NOTICE_SOURCE
from kwcampus.kwnotice.redaction import redact_text
from kwcampus.routing import CATEGORY_LABEL, QUIET_CATEGORIES
from kwcampus.store import title_key

_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_S = 60
# Copies of one notice posted by several boards within this many days fold into one result.
CROSS_POST_DAYS = 7

DEPARTMENT_IDS = frozenset(cid for cid in CATEGORY_LABEL if cid.startswith("dept-"))
_SCHOLARSHIP = frozenset({"registration-scholarship"})
_JOB = frozenset({"job"})
# Common words people use for a category. "취업" is the 취업게시판's own label; "외부" still finds
# the main board's outside notices.
_ALIASES: dict[str, frozenset[str]] = {
    "장학": _SCHOLARSHIP, "장학금": _SCHOLARSHIP, "장학생": _SCHOLARSHIP, "등록": _SCHOLARSHIP,
    "등록금": _SCHOLARSHIP,
    "채용": _JOB, "취업관련": _JOB, "취업정보": _JOB, "일자리": _JOB, "인턴": _JOB,
    "대외활동": frozenset({"external"}), "국제": frozenset({"international-exchange", "international-student"}),
    "교환학생": frozenset({"international-exchange"}), "유학생": frozenset({"international-student"}),
    "학과": DEPARTMENT_IDS, "학과공지": DEPARTMENT_IDS, "단과대": DEPARTMENT_IDS, "학부": DEPARTMENT_IDS,
    "인융대": frozenset({"dept-aicon"}), "인공지능융합": frozenset({"dept-aicon"}),
    "전정대": frozenset({"dept-ei"}), "전자정보공대": frozenset({"dept-ei"}), "인사대": frozenset({"dept-chss"}),
    "경영대": frozenset({"dept-biz"}), "참빛": frozenset({"dept-kwlife"}), "인제니움": frozenset({"dept-ingenium"}),
    "미컴": frozenset({"dept-media"}),
}
# Every collected board: the main notice board, department boards and feeds, the 취업게시판 and the FAQ.
SOURCES = (NOTICE_SOURCE, "aicon", *(b.source for b in (*deptboards.BOARDS, *wixfeeds.FEEDS, *wwwboards.BOARDS)))


class UnknownCategory(ValueError):
    """A category filter that matches nothing; never answered with every category instead."""

    def __init__(self, category: str):
        self.category = category
        self.valid = valid_labels()
        super().__init__(f'unknown_category: "{category}" is not a notice category. '
                         f"Use one of: {', '.join(self.valid)}")

    def as_dict(self) -> dict[str, object]:
        return {"error": "unknown_category", "category": self.category, "valid": self.valid}


def valid_labels() -> list[str]:
    return list(dict.fromkeys(CATEGORY_LABEL.values()))


def resolve_category(value: str | None) -> frozenset[str] | None:
    """Category ids for an id ("academic"), a label ("학사"), an alias ("장학금", "학과") or a unique
    part of labels ("건축" -> 건축공학과 and 건축학과). None: no filter. UnknownCategory otherwise."""
    if not value or not value.strip():
        return None
    v = value.strip()
    if v in CATEGORY_LABEL:
        return frozenset({v})
    by_label = frozenset(cid for cid, label in CATEGORY_LABEL.items() if label == v)
    if by_label:
        return by_label
    if v.casefold() in _ALIASES or v in _ALIASES:
        return _ALIASES.get(v) or _ALIASES[v.casefold()]
    part = frozenset(cid for cid, label in CATEGORY_LABEL.items() if v in label)
    if part:
        return part
    within = frozenset(cid for cid, label in CATEGORY_LABEL.items() if len(label) >= 2 and label in v)
    if within:
        return within
    raise UnknownCategory(v)


def category_id(value: str | None) -> str | None:
    """One category id for a value that names exactly one category; None otherwise."""
    try:
        ids = resolve_category(value)
    except UnknownCategory:
        return None
    return next(iter(ids)) if ids and len(ids) == 1 else None


def _query_all(table, pk: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with("N#")}
    while True:
        page = table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _live(item: dict, now: float) -> bool:
    return not item.get("removed") and not (item.get("ttl") and int(item["ttl"]) <= now)


def _order(item: dict) -> tuple[str, int, int]:
    """Newest first: posted date, then when we first saw it, then the board's own number. Ids of
    different sources are never compared as strings (that put same-day main-board posts last)."""
    raw = str(item.get("post_id") or str(item.get("SK", ""))[2:])
    return (str(item.get("posted_date", "")), int(item.get("first_seen") or 0), int(raw) if raw.isdigit() else 0)


def notices(table, source: str | None = None) -> list[dict]:
    """Notices of one source, or of every source (main board + departments) by default, newest
    first. Each row also carries its source as "_source" (dropped on the way out, see _out)."""
    key = source or "*"
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    now = time.time()
    items = [(src, i) for src in ((source,) if source else SOURCES) for i in _query_all(table, f"SRC#{src}")
             if _live(i, now)]
    items.sort(key=lambda pair: _order(pair[1]), reverse=True)
    rows = [{**public_view(i), "_source": src} for src, i in items]
    _CACHE[key] = (time.monotonic(), rows)
    return rows


def _out(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _public_id(item: dict) -> str:
    """Main-board ids stay as they are; other sources get a prefix so ids never collide."""
    raw = str(item.get("post_id") or str(item["SK"])[2:])
    source = str(item.get("source") or NOTICE_SOURCE)
    return raw if source == NOTICE_SOURCE else f"{source}-{raw}"


def public_view(item: dict) -> dict:
    """Only fields meant for readers; internal flags (seed, hashes, ttl) stay in the table."""
    cid = str(item.get("category_id", ""))
    return {
        "post_id": _public_id(item),
        "title": redact_text(str(item.get("title", ""))),
        "category": CATEGORY_LABEL.get(cid, cid),
        "category_id": cid,
        "department": redact_text(str(item.get("department", ""))),
        "posted_date": str(item.get("posted_date", "")),
        "updated_date": str(item.get("updated_date", "")),
        "pinned": bool(item.get("pinned")),
        "url": str(item.get("url", "")),
    }


def _filter(rows: Iterable[dict], category: str | None, since: date | None, quiet: bool = True,
            sources: Iterable[str] | None = None) -> list[dict]:
    """quiet=False leaves out the FAQ (reference pages, never announced) unless asked for by category.
    sources keeps only rows of those sources ("kw-notice" is the main board)."""
    ids = resolve_category(category)
    wanted = frozenset(sources) if sources is not None else None
    out = []
    for r in rows:
        if wanted is not None and r.get("_source", NOTICE_SOURCE) not in wanted:
            continue
        if ids is not None and r.get("category_id") not in ids:
            continue
        if ids is None and not quiet and r.get("category_id") in QUIET_CATEGORIES:
            continue
        if since and r["posted_date"] < since.isoformat():
            continue
        out.append(r)
    return out


def _fold_cross_posts(rows: list[dict]) -> list[dict]:
    """Keep the first of several near-identical titles posted within CROSS_POST_DAYS of each other."""
    kept: dict[str, dict] = {}
    out = []
    for r in rows:
        k = title_key(str(r.get("title", "")))
        first = kept.get(k)
        if k and first is not None and _days_apart(first, r) <= CROSS_POST_DAYS:
            first.setdefault("also", []).append(r.get("post_id"))
            continue
        if k:
            kept[k] = r
        out.append(r)
    return out


def _days_apart(a: dict, b: dict) -> int:
    try:
        return abs((date.fromisoformat(str(a["posted_date"])) - date.fromisoformat(str(b["posted_date"]))).days)
    except (KeyError, ValueError):
        return CROSS_POST_DAYS + 1


def _limited(rows: list[dict], limit: int) -> list[dict]:
    limit = max(1, min(limit, 50))
    folded, start = [], 0
    while len(folded) < limit and start < len(rows):  # fold within a window a bit larger than asked
        start += limit * 3
        folded = _fold_cross_posts([_out(r) for r in rows[:start]])
    return folded[:limit]


def latest(table, category: str | None = None, limit: int = 10, include_pinned: bool = True,
           since: date | None = None, sources: Iterable[str] | None = None) -> list[dict]:
    """Newest notices. Every filter (category, since, sources, pinned) applies before the limit."""
    rows = _filter(notices(table), category, since, quiet=False, sources=sources)
    if not include_pinned:
        rows = [r for r in rows if not r["pinned"]]
    return _limited(rows, limit)


def search(table, query: str, category: str | None = None, since: date | None = None, limit: int = 20,
           sources: Iterable[str] | None = None) -> list[dict]:
    """Notices whose title or department has every word of query; filters apply before the limit."""
    terms = [t.casefold() for t in query.split() if t.strip()]
    rows = _filter(notices(table), category, since, quiet=bool(terms), sources=sources)
    hits = [r for r in rows if all(t in (r["title"] + " " + r["department"]).casefold() for t in terms)]
    return _limited(hits, limit)


def get(table, post_id: str) -> dict | None:
    row = next((r for r in notices(table) if r["post_id"] == str(post_id).strip()), None)
    return _out(row) if row else None


def categories(table) -> list[dict]:
    counts: dict[str, int] = {}
    for r in notices(table):
        counts[r["category_id"]] = counts.get(r["category_id"], 0) + 1
    return [{"category_id": cid, "category": label, "count": counts.get(cid, 0)} for cid, label in CATEGORY_LABEL.items()]


def clear_cache() -> None:
    _CACHE.clear()


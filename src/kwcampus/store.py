"""Notice items in the kw-public table.

Keys: PK = "SRC#<source>", SK = "N#<post id>". The source's own state lives at
SK = "STATE" (seeded_at). Items are written only when they are new or their content
hash changes, so every DynamoDB Streams record means something happened.

norm_v is the redaction version the stored text went through (redaction.REDACTION_LEVEL). A
MODIFY whose norm_v went up is our own re-normalisation; the school did not edit anything.
title_key is the title without tags, brackets and punctuation, to fold cross-posted copies.
A notice taken down by hand (scripts/collector_admin.py takedown) keeps a blank item with
removed=true until its ttl, so the next collector run cannot bring it back.

A new notice (not a seed row), one whose title changed, and one whose updated_date changed (the
수정일 of the main board, the job board and the FAQ; the 등록일 of a department board re-using a post)
also get a row in the page queue (PK = "BODYQ", SK = "<notice PK>|<notice SK>"): extract.py reads
that notice's own page once, in a later collector run, and stores what it found on the notice
(`extracted`, `body_hash`). A re-read of an unchanged body skips the model (same body_hash), so an
edit costs one page request from the server's spare, and the facts follow the edit. The queue's SK
does not start with "N#", so the stream filter keeps it away from the dispatcher.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from kwcampus.config import NOTICE_TTL
from kwcampus.kwnotice.domain import NoticeSummary
from kwcampus.kwnotice.redaction import REDACTION_LEVEL

SCHEMA_VERSION = 1
BODY_QUEUE = "BODYQ"
# A page not read by then is old news: the queue row goes with its ttl.
BODY_QUEUE_TTL = timedelta(days=14)
_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】|<[^>]*>")
_NOT_WORD = re.compile(r"[\W_]+")


def source_pk(source: str) -> str:
    return f"SRC#{source}"


def content_hash(n: NoticeSummary) -> str:
    raw = "\x1f".join([n.title, n.category_id, n.department, n.updated_date.isoformat(), str(n.pinned)])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def title_key(title: str) -> str:
    """"[대학혁신지원사업] 2026 광운대학교 취업박람회 개최 안내" -> "2026광운대학교취업박람회개최안내"."""
    return _NOT_WORD.sub("", _TAGS.sub("", title)).casefold()


def notice_item(source: str, n: NoticeSummary, now: datetime, seed: bool) -> dict[str, object]:
    return {
        "PK": source_pk(source),
        "SK": f"N#{n.duid}",
        "schema_v": SCHEMA_VERSION,
        "norm_v": REDACTION_LEVEL,
        "source": source,
        "post_id": str(n.duid),
        "title": n.title,
        "title_key": title_key(n.title),
        "category_id": n.category_id,
        "department": n.department,
        "posted_date": n.posted_date.isoformat(),
        "updated_date": n.updated_date.isoformat(),
        "url": str(n.source_url),
        "pinned": n.pinned,
        "attachments_present": n.attachments_present,
        "content_hash": content_hash(n),
        "first_seen": int(now.timestamp()),
        "seed": seed,
        "ttl": int((now + NOTICE_TTL).timestamp()),
    }


def queue_body(table, item: dict[str, object]) -> None:
    """Ask for this notice's page to be read once (extract.collect). A second ask before then is the same row."""
    at = int(item["first_seen"])  # notice_item sets it to the run's time
    table.put_item(Item={
        "PK": BODY_QUEUE, "SK": f"{item['PK']}|{item['SK']}",
        "notice_pk": item["PK"], "notice_sk": item["SK"], "queued_at": at,
        "ttl": at + int(BODY_QUEUE_TTL.total_seconds()),
    })


def upsert(table, item: dict[str, object]) -> str:
    """Insert a new notice or update a changed one. Returns inserted/updated/unchanged.

    An update also moves posted_date (a department re-posting an old post for a new term keeps its
    number but gets a new 등록일) and the ttl: a notice the school keeps changing stays 120 days
    from its last change. A new notice that is no seed row, and any notice whose title or updated date
    changed (the school's edits, which the Discord edit note also follows), get their page queued
    (queue_body)."""
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        if not item.get("seed"):
            queue_body(table, item)
        return "inserted"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    try:
        old = table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression=(
                "SET title = :title, title_key = :tkey, category_id = :cat, department = :dept, "
                "posted_date = :posted, updated_date = :upd, pinned = :pinned, content_hash = :hash, "
                "norm_v = :norm, #ttl = :ttl"
            ),
            ConditionExpression="content_hash <> :hash AND attribute_not_exists(removed)",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={
                ":title": item["title"],
                ":tkey": item["title_key"],
                ":cat": item["category_id"],
                ":dept": item["department"],
                ":posted": item["posted_date"],
                ":upd": item["updated_date"],
                ":pinned": item["pinned"],
                ":hash": item["content_hash"],
                ":norm": item["norm_v"],
                ":ttl": item["ttl"],
            },
            ReturnValues="ALL_OLD",  # the whole item before: an unchanged title is still there to compare
        ).get("Attributes") or {}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "unchanged"
        raise
    # A title that changed only because our redaction rules did (norm_v up, same 수정일) is no new page.
    # An item stored before norm_v existed has none: its retitle counts as the school's.
    ours = (old.get("norm_v") is not None and old["norm_v"] != item["norm_v"]
            and old.get("updated_date") == item["updated_date"])
    edited = old.get("updated_date") != item["updated_date"]  # 수정일 moved: the page may say something new
    if (old.get("title") != item["title"] and not ours) or edited:
        queue_body(table, item)
    return "updated"


def is_seeded(table, source: str) -> bool:
    item = table.get_item(Key={"PK": source_pk(source), "SK": "STATE"}, ConsistentRead=True).get("Item")
    return bool(item and item.get("seeded_at"))


def mark_seeded(table, source: str, now: datetime) -> None:
    table.update_item(
        Key={"PK": source_pk(source), "SK": "STATE"},
        UpdateExpression="SET seeded_at = if_not_exists(seeded_at, :now)",
        ExpressionAttributeValues={":now": int(now.timestamp())},
    )

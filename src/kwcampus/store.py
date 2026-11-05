"""Notice items in the kw-public table.

Keys: PK = "SRC#<source>", SK = "N#<post id>". The source's own state lives at
SK = "STATE" (seeded_at). Items are written only when they are new or their content
hash changes, so every DynamoDB Streams record means something happened.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from botocore.exceptions import ClientError

from kwcampus.config import NOTICE_TTL
from kwcampus.kwnotice.domain import NoticeSummary

SCHEMA_VERSION = 1


def source_pk(source: str) -> str:
    return f"SRC#{source}"


def content_hash(n: NoticeSummary) -> str:
    raw = "\x1f".join([n.title, n.category_id, n.department, n.updated_date.isoformat(), str(n.pinned)])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def notice_item(source: str, n: NoticeSummary, now: datetime, seed: bool) -> dict[str, object]:
    return {
        "PK": source_pk(source),
        "SK": f"N#{n.duid}",
        "schema_v": SCHEMA_VERSION,
        "source": source,
        "post_id": str(n.duid),
        "title": n.title,
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


def upsert(table, item: dict[str, object]) -> str:
    """Insert a new notice or update a changed one. Returns inserted/updated/unchanged."""
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return "inserted"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    try:
        table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression=(
                "SET title = :title, category_id = :cat, department = :dept, "
                "updated_date = :upd, pinned = :pinned, content_hash = :hash"
            ),
            ConditionExpression="content_hash <> :hash",
            ExpressionAttributeValues={
                ":title": item["title"],
                ":cat": item["category_id"],
                ":dept": item["department"],
                ":upd": item["updated_date"],
                ":pinned": item["pinned"],
                ":hash": item["content_hash"],
            },
        )
        return "updated"
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "unchanged"
        raise


def is_seeded(table, source: str) -> bool:
    item = table.get_item(Key={"PK": source_pk(source), "SK": "STATE"}, ConsistentRead=True).get("Item")
    return bool(item and item.get("seeded_at"))


def mark_seeded(table, source: str, now: datetime) -> None:
    table.update_item(
        Key={"PK": source_pk(source), "SK": "STATE"},
        UpdateExpression="SET seeded_at = if_not_exists(seeded_at, :now)",
        ExpressionAttributeValues={":now": int(now.timestamp())},
    )

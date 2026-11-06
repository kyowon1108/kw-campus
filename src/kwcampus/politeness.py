"""Per-origin request gate kept in DynamoDB.

One item per origin (server IP group) holds when the next request is allowed, the
current cool-down, and today's request count. Updates use an optimistic version check,
so two overlapping runs can never both get a permit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from botocore.exceptions import ClientError

from kwcampus.config import KST

MIN_INTERVAL = timedelta(seconds=60)
COOLDOWN_START = timedelta(minutes=30)
COOLDOWN_MAX = timedelta(hours=24)
# A source is switched off after this many blocks in a row; a human turns it back on.
MAX_CONSECUTIVE_BLOCKS = 3


@dataclass(frozen=True)
class Permit:
    granted: bool
    reason: str  # "ok" or why not


def _key(origin: str) -> dict[str, str]:
    return {"PK": f"ORIGIN#{origin}", "SK": "STATE"}


def _ts(value: datetime) -> int:
    return int(value.timestamp())


def acquire(table, origin: str, now: datetime, daily_cap: int) -> Permit:
    item = table.get_item(Key=_key(origin), ConsistentRead=True).get("Item") or {}
    if item.get("disabled"):
        return Permit(False, "disabled_after_blocks")
    if int(item.get("cooldown_until", 0)) > _ts(now):
        return Permit(False, "cooldown")
    if int(item.get("next_at", 0)) > _ts(now):
        return Permit(False, "too_soon")
    today = now.astimezone(KST).strftime("%Y-%m-%d")
    count = int(item.get("day_count", 0)) if item.get("day") == today else 0
    if count >= daily_cap:
        return Permit(False, "daily_cap")

    version = int(item.get("ver", 0))
    try:
        table.update_item(
            Key=_key(origin),
            UpdateExpression="SET next_at = :next, #day = :day, day_count = :count, ver = :newver",
            ConditionExpression="attribute_not_exists(ver) OR ver = :ver",
            ExpressionAttributeNames={"#day": "day"},
            ExpressionAttributeValues={
                ":next": _ts(now + MIN_INTERVAL),
                ":day": today,
                ":count": count + 1,
                ":ver": version,
                ":newver": version + 1,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return Permit(False, "raced")
        raise
    return Permit(True, "ok")


def move(table, old: str, new: str) -> None:
    """Carry a gate over to a new key once (cool-down, block count, today's count and all), as when
    aicon's gate went from its host name to its server. A no-op read once moved."""
    item = table.get_item(Key=_key(old), ConsistentRead=True).get("Item")
    if not item:
        return
    try:
        table.put_item(Item={**item, **_key(new)}, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    table.delete_item(Key=_key(old))


def record_success(table, origin: str) -> None:
    table.update_item(
        Key=_key(origin),
        UpdateExpression="SET blocks = :zero REMOVE last_block",
        ExpressionAttributeValues={":zero": 0},
    )


def record_block(table, origin: str, now: datetime, reason: str) -> dict[str, object]:
    """Escalate the cool-down: 30 min, 60 min, ... up to 24 h. Returns the new state."""
    item = table.get_item(Key=_key(origin), ConsistentRead=True).get("Item") or {}
    blocks = int(item.get("blocks", 0)) + 1
    cooldown = min(COOLDOWN_START * (2 ** (blocks - 1)), COOLDOWN_MAX)
    disabled = blocks >= MAX_CONSECUTIVE_BLOCKS
    table.update_item(
        Key=_key(origin),
        UpdateExpression="SET blocks = :b, cooldown_until = :until, last_block = :reason, disabled = :d",
        ExpressionAttributeValues={
            ":b": blocks,
            ":until": _ts(now + cooldown),
            ":reason": reason,
            ":d": disabled,
        },
    )
    return {"blocks": blocks, "cooldown_minutes": int(cooldown.total_seconds() // 60), "disabled": disabled}

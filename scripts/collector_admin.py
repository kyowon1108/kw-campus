"""Collector chores a human does by hand. Every command only reports unless --apply.

    uv run python scripts/collector_admin.py disabled                       # switched-off origins and sources
    uv run python scripts/collector_admin.py reenable --origin 61.111.8.4   # what turning it back on resets
    uv run python scripts/collector_admin.py reenable --source dept-ee --apply
    uv run python scripts/collector_admin.py takedown dept-ee-646           # the notice and its Discord messages
    uv run python scripts/collector_admin.py takedown dept-ee-646 --apply   # delete them
    uv run python scripts/collector_admin.py redaction-report               # notices the current masking would change

reenable puts an origin (or one source) back to the start: no blocks, no transient count, no cool-down,
not disabled. Clearing only `disabled` by hand would leave the block count at 3, and the next single
block would switch it off again.

takedown removes one notice everywhere we keep it: the Discord message (and its "수정됨" reply) in
every channel it went to, the delivery records, and the kw-public item, which stays as a blank
tombstone (removed=true) so the next collector run does not bring it back. The bot deletes only its
own messages, which needs no extra Discord permission. Use it when the school deleted a post that
exposed people, or when someone asks for a notice to be removed.

redaction-report counts stored notices whose title or department the current redaction would change
(no titles printed). It does not write anything; public_data masks again on the way out anyway.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from kwcampus import params, politeness, public_data
from kwcampus.config import (
    DELIVERY_TABLE,
    DISCORD_TOKEN_PARAM,
    NOTICE_SOURCE,
    PUBLIC_TABLE,
)
from kwcampus.discordapi import NotFound, call, clean_token, token_problem
from kwcampus.kwnotice.redaction import REDACTION_LEVEL, redact_text

REGION = "ap-northeast-2"
TOMBSTONE_TTL = timedelta(days=365)


def _scan(table, **kwargs):
    while True:
        page = table.scan(**kwargs)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def disabled(public) -> list[str]:
    out = []
    for item in _scan(public, FilterExpression="SK = :state AND disabled = :true",
                      ExpressionAttributeValues={":state": "STATE", ":true": True}):
        out.append(f"{item['PK']}: last {item.get('last_block') or item.get('last_fail') or '?'}")
    return out


def reenable(public, origin: str | None, source: str | None, apply: bool) -> str:
    if origin:
        state = politeness.state(public, origin)
        shown = {k: state.get(k) for k in ("disabled", "blocks", "soft", "cooldown_until", "last_block")}
        if apply:
            politeness.reenable(public, origin)
        return f"ORIGIN#{origin} {shown} -> {'reset' if apply else 'would reset'}"
    key = {"PK": f"SRC#{source}", "SK": "STATE"}
    state = public.get_item(Key=key, ConsistentRead=True).get("Item") or {}
    shown = {k: state.get(k) for k in ("disabled", "fails", "hard_blocks", "not_before", "last_fail")}
    if apply:
        public.update_item(Key=key, UpdateExpression="REMOVE disabled, fails, hard_blocks, not_before, last_fail")
    return f"SRC#{source} {shown} -> {'reset' if apply else 'would reset'}"


def notice_key(post_id: str) -> dict[str, str]:
    """The kw-public key of a public post id: "55001" (main board) or "<source>-<id>"."""
    post_id = post_id.strip()
    if post_id.isdigit():
        return {"PK": f"SRC#{NOTICE_SOURCE}", "SK": f"N#{post_id}"}
    for source in sorted(public_data.SOURCES, key=len, reverse=True):
        if post_id.startswith(source + "-"):
            return {"PK": f"SRC#{source}", "SK": f"N#{post_id[len(source) + 1:]}"}
    raise SystemExit(f"no source has post id {post_id!r}")


def takedown(public, delivery, post_id: str, apply: bool, send=None, token: str | None = None,
             now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    key = notice_key(post_id)
    item = public.get_item(Key=key, ConsistentRead=True).get("Item")
    lines = [f"notice {key['PK']} {key['SK']}: " + (
        "not stored" if not item else
        f"{'already removed' if item.get('removed') else 'stored'}, posted {item.get('posted_date')}, {item.get('url')}")]
    records = delivery.query(KeyConditionExpression=Key("PK").eq(f"{key['PK']}|{key['SK']}"))["Items"]
    for record in records:
        for field in ("message_id", "reply_message_id"):
            if record.get(field):
                lines.append(f"Discord {record['SK']} channel {record.get('channel_id')} {field} {record[field]}")
                if apply:
                    try:
                        send("DELETE", f"/channels/{record['channel_id']}/messages/{record[field]}", token)
                    except NotFound:  # someone deleted it already
                        pass
        if apply:
            delivery.delete_item(Key={"PK": record["PK"], "SK": record["SK"]})
    if item and apply:  # a blank tombstone: the collector's upsert never rewrites a removed item
        public.update_item(
            Key=key,
            UpdateExpression="SET removed = :true, title = :blank, title_key = :blank, removed_at = :now, #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":true": True, ":blank": "", ":now": int(now.timestamp()),
                                       ":ttl": int((now + TOMBSTONE_TTL).timestamp())},
        )
        public_data.clear_cache()
    lines.append("done" if apply else "dry run: nothing changed (add --apply)")
    return lines


def redaction_report(public) -> dict[str, int]:
    """Counts only: stored notices, those whose title/department the current masking changes, and
    those with names among them. Nothing is written."""
    counts = {"notices": 0, "would_change": 0, "names": 0, f"already_v{REDACTION_LEVEL}": 0}
    for item in _scan(public, FilterExpression="begins_with(SK, :n)", ExpressionAttributeValues={":n": "N#"}):
        counts["notices"] += 1
        if int(item.get("norm_v", 1)) >= REDACTION_LEVEL:
            counts[f"already_v{REDACTION_LEVEL}"] += 1
        before = (str(item.get("title", "")), str(item.get("department", "")))
        after = tuple(redact_text(v) for v in before)
        if after != before:
            counts["would_change"] += 1
            counts["names"] += int(any("[REDACTED_NAME]" in v for v in after))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--region", default=REGION)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("disabled")
    re_en = sub.add_parser("reenable")
    which = re_en.add_mutually_exclusive_group(required=True)
    which.add_argument("--origin", help="a server IP or host name (ORIGIN#<origin>)")
    which.add_argument("--source", help="a source such as dept-ee, aicon, kw-job")
    re_en.add_argument("--apply", action="store_true")
    take = sub.add_parser("takedown")
    take.add_argument("post_id", help='the public post id: "55001", "dept-ee-646", "aicon-3516"')
    take.add_argument("--apply", action="store_true")
    sub.add_parser("redaction-report")
    args = parser.parse_args(argv)

    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    public = dynamodb.Table(PUBLIC_TABLE)
    if args.command == "disabled":
        print("\n".join(disabled(public)) or "nothing is switched off")
    elif args.command == "reenable":
        print(reenable(public, args.origin, args.source, args.apply))
    elif args.command == "takedown":
        send = token = None
        if args.apply:
            token = clean_token(params.get(DISCORD_TOKEN_PARAM, decrypt=True))
            if token_problem(token):
                print(f"cannot delete Discord messages: {token_problem(token)}", file=sys.stderr)
                return 2
            send = call
        print("\n".join(takedown(public, dynamodb.Table(DELIVERY_TABLE), args.post_id, args.apply, send, token)))
    else:
        started = time.monotonic()
        counts = redaction_report(public)
        print(" ".join(f"{k}={v}" for k, v in counts.items()), f"({time.monotonic() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

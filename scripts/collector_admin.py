"""Collector chores a human does by hand. Every command only reports unless --apply.

    uv run python scripts/collector_admin.py disabled                       # switched-off origins and sources
    uv run python scripts/collector_admin.py reenable --origin 61.111.8.4   # what turning it back on resets
    uv run python scripts/collector_admin.py reenable --source dept-ee --apply
    uv run python scripts/collector_admin.py takedown dept-ee-646           # the notice and its Discord messages
    uv run python scripts/collector_admin.py takedown dept-ee-646 --apply   # delete them
    uv run python scripts/collector_admin.py redaction-report               # notices the current masking would change
    uv run python scripts/collector_admin.py extract-backfill               # what a backfill would read (counts)
    uv run python scripts/collector_admin.py extract-backfill --days 45 --max 150 --budget 25 --apply

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

extract-backfill reads the pages of stored notices that have no extracted facts yet (학사, 장학, 취업,
국제 and 학과 boards; every stored notice, or those posted in the last --days) and stores what Claude
reads there (kwcampus/extract.py), newest first, at most --max notices. Unlike the collector's page
runs it also shows Claude the notice's own images (up to 4: posters are what students miss most) and
the text of up to 2 attachments (PDF, HWP, HWPX, DOCX, XLSX, read here by kw_mcp.attachments, about 30
pages each). Text goes through kwnotice.redaction first; images can't be, so a notice whose title may
name people (rosters, prizes, results) and a file named like a roster get the body text only.
Requests go through the collectors' own gate in kw-public: one per server a minute, robots records,
and only what each server's daily cap has to spare after today's list runs (all of it on a weekend),
so on a weekday it may read little. --extra-today N lifts that cap by N per server, but only while no
list run is left today (the collectors check the plain cap); the minute between requests stays. Every Claude call goes into the spend ledger
(../claude-spend/ledger.py) as "notice-backfill"; the run stops once --budget USD is used up. Only the
extraction fields are written (extracted, extract_v, extracted_at, model, body_hash), which the
dispatcher never turns into a Discord job. Stop it any time and run it again: a notice is written only
when it is done, and done notices are not picked again.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Key

from kwcampus import extract, notice_assets, params, politeness, public_data
from kwcampus.config import (
    DELIVERY_TABLE,
    DISCORD_TOKEN_PARAM,
    KST,
    NOTICE_SOURCE,
    PUBLIC_TABLE,
)
from kwcampus.discordapi import NotFound, call, clean_token, token_problem
from kwcampus.kwnotice.redaction import REDACTION_LEVEL, redact_text
from kwcampus.notice_assets import PoliteGate, WatchedTransport

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


# --- extract-backfill -------------------------------------------------------------------------

PIPELINE = "notice-backfill"
MIN_LEFT_USD = 0.3  # stop before a call when the budget has less than this left
def backfill_candidates(public, days: int | None, now: datetime) -> list[dict]:
    """Stored notices without facts of the current extraction version, newest first."""
    since = (now.astimezone(KST).date() - timedelta(days=days)).isoformat() if days else ""
    found = []
    for source in public_data.SOURCES:
        kwargs = {"KeyConditionExpression": Key("PK").eq(f"SRC#{source}") & Key("SK").begins_with("N#")}
        while True:
            page = public.query(**kwargs)
            for item in page["Items"]:
                if (item.get("removed") or (item.get("ttl") and int(item["ttl"]) <= now.timestamp())
                        or int(item.get("extract_v") or 0) >= extract.EXTRACT_V
                        or str(item.get("posted_date") or "") < since or not extract.wanted(item)):
                    continue
                found.append(item)
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(found, key=lambda i: (str(i.get("posted_date")), int(i.get("first_seen") or 0)), reverse=True)


async def run_backfill(public, candidates: list[dict], max_notices: int, budget: float, spend, client=None,
                       now: Callable[[], datetime] = lambda: datetime.now(UTC), sleep=asyncio.sleep,
                       read=None, inner_transport=None, extra_today: int = 0, gap_s: float | None = None) -> Counter:
    """One worker per server, each reading its notices in turn; Claude-only (the run is for its budget)."""
    from kw_mcp import notices

    read = read or notices.read_notice
    gate = PoliteGate(public, now, sleep, refused=notices.Refused, extra_today=extra_today, gap_s=gap_s)
    counts: Counter = Counter()
    done = asyncio.Event()

    def transport():
        return WatchedTransport(public, now, inner_transport() if inner_transport else None)

    async def one(notice: dict) -> None:
        try:
            body, images, files = await notice_assets.read_parts(notice, read, gate, transport)
        except notices.Refused:
            raise
        except notices.NoticeError:  # 404, an empty page, a block (the transport noted it for the gate)
            counts["page_error"] += 1
            return
        sent, original = extract.compose(body, files)
        sources = extract.sources_of(body, images, files)
        if len(body.strip()) < extract.MIN_BODY_CHARS and not images and not files:
            extract.store_facts(public, notice, None, extract.body_hash(original), "", now())
            counts["nothing_to_read"] += 1
            return
        if spend.remaining(budget, PIPELINE) <= MIN_LEFT_USD:
            counts["budget_used"] += 1
            done.set()
            return
        try:
            raw, _, model = await asyncio.to_thread(extract.ask_anthropic, notice, sent, client, tuple(images), PIPELINE)
        except extract.ModelError as e:
            counts[f"model_{e.kind}"] += 1
            if e.kind in ("auth", "retry_later"):  # a key problem or Claude busy: stop, run again later
                done.set()
            return
        facts, _ = extract.validate(raw, notice, sent, original, sources)
        if extract.store_facts(public, notice, facts, extract.body_hash(original), model, now()):
            counts["extracted"] += 1
            counts.update(f"with_{s}" for s in facts["sources_used"] if s != "body")

    async def worker(items: list[dict]) -> None:
        for notice in items:
            if done.is_set() or counts["read"] >= max_notices:
                return
            counts["read"] += 1
            try:
                await one(notice)
            except notices.Refused:  # this server is out of turns for today (or cooling down)
                counts["server_out_of_turns"] += 1
                return

    by_origin: dict[str, list[dict]] = {}
    for notice in candidates:
        by_origin.setdefault(extract.page_for(notice)[0].origin, []).append(notice)
    await asyncio.gather(*(worker(items) for items in by_origin.values()))
    return counts


def backfill_plan(public, candidates: list[dict], now: datetime) -> dict[str, object]:
    """Counts only: candidates per server and what each server has to spare today."""
    per: Counter = Counter(extract.page_for(n)[0].origin for n in candidates)
    pages = {extract.page_for(n)[0].origin: extract.page_for(n)[0] for n in candidates}
    return {"candidates": len(candidates), "by_server": dict(per),
            "spare_today": {origin: extract.spare(public, page, now) for origin, page in pages.items()}}


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
    back = sub.add_parser("extract-backfill")
    back.add_argument("--days", type=int, default=None, help="only notices posted in the last N days (default: all)")
    back.add_argument("--max", type=int, default=150, help="notices to read at most")
    back.add_argument("--budget", type=float, default=25.0, help="USD this pipeline may spend (spend ledger)")
    back.add_argument("--extra-today", type=int, default=0,
                      help="requests per server above our own daily cap, used only while no list run is left "
                           "today (weekends, or after the last weekday run); still one request per server a minute")
    back.add_argument("--gap", type=float, default=None,
                      help="seconds between requests to one server for this run (at least 1; default: the collectors' 60)")
    back.add_argument("--apply", action="store_true")
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
    elif args.command == "extract-backfill":
        os.environ.setdefault("AWS_DEFAULT_REGION", args.region)  # SSM: the Claude key, in Seoul
        now = datetime.now(UTC)
        candidates = backfill_candidates(public, args.days, now)
        spend = extract.ledger()
        plan = backfill_plan(public, candidates, now)
        left = spend.remaining(args.budget, PIPELINE) if spend else None
        print(" ".join(f"{k}={v}" for k, v in plan.items()), f"budget_left_usd={left if left is None else round(left, 2)}",
              f"model={extract.MODELS['anthropic']}")
        if not args.apply:
            print("dry run: nothing read or written (add --apply)")
            return 0
        if spend is None:
            print("no spend ledger (../claude-spend/ledger.py or KW_SPEND_LEDGER): not running", file=sys.stderr)
            return 2
        try:
            import kw_mcp.notices  # noqa: F401 - images and attachments come through it
        except ImportError:
            print("kw_mcp.notices is missing: update kw-mcp first", file=sys.stderr)
            return 2
        counts = asyncio.run(run_backfill(public, candidates, args.max, args.budget, spend,
                                          extra_today=args.extra_today, gap_s=args.gap))
        public_data.clear_cache()
        print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())),
              f"spent_usd={round(spend.spent(PIPELINE), 2)}")
    else:
        started = time.monotonic()
        counts = redaction_report(public)
        print(" ".join(f"{k}={v}" for k, v in counts.items()), f"({time.monotonic() - started:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

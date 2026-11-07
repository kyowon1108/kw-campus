"""Per-origin request gate kept in DynamoDB, robots.txt records, and per-source back-off.

One item per origin (server IP group) holds when the next request is allowed, the
current cool-down, and today's request count. Updates use an optimistic version check,
so two overlapping runs can never both get a permit.

What a failed request does depends on what it says about the server:
- hard blocks (403, 429, a real challenge page) cool the whole origin down, 30 min doubling up to
  24 h (longer if Retry-After asks), and three in a row switch the origin off until a human turns
  it back on (scripts/collector_admin.py reenable);
- transient failures (connection errors, timeouts, 5xx) wait for the next run the first time, then
  30 min doubling up to 6 h; an empty page that may be a block page we don't recognise starts at
  30 min. Neither ever switches the origin off; six in a row raise NeedsHuman;
- anything about one page only (404, a redirect, an unexpected page or content type, an oversized
  page) leaves the origin alone: that source backs off by itself (source_failed), and from its
  third failure in a row on it raises NeedsHuman each time (a board that moved for good answers
  404 or a redirect forever; at 2 h, 4 h, 8 h ... it would never reach the SourceFailed alarm).

robots.txt is checked by hand and recorded in ROBOTS; once a record is older than ROBOTS_MAX_AGE
the daily campus run raises NeedsHuman (robots_stale) until someone looks again and updates it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from kwcampus.config import KST
from kwcampus.obs import log, metrics

MIN_INTERVAL = timedelta(seconds=60)
# A notice's own images and attachment files, right after its page (a browser opening the notice loads
# them all at once): this far apart, then the server's minute again (rest). Each still counts.
ASSET_GAP_S = 3
COOLDOWN_START = timedelta(minutes=30)
COOLDOWN_MAX = timedelta(hours=24)
SOFT_COOLDOWN_MAX = timedelta(hours=6)
RETRY_AFTER_MAX = timedelta(days=7)
SOURCE_BACKOFF_MAX = timedelta(hours=24)
# An origin is switched off after this many hard blocks in a row; a human turns it back on.
MAX_CONSECUTIVE_BLOCKS = 3
# Transient failures in a row before a human should look (the origin stays on).
SOFT_NEEDS_HUMAN = 6
# One source's own failures in a row (404, a redirect, a changed page) before a human should look.
SOURCE_NEEDS_HUMAN = 3
# A robots.txt record older than this needs a fresh look by hand.
ROBOTS_MAX_AGE = timedelta(days=180)

HARD = frozenset({"forbidden", "rate_limit", "captcha"})
EMPTY = frozenset({"no_rows", "no_rows_everywhere"})
SOFT = frozenset({"transport_failure", "server_error", "slow"}) | EMPTY


def is_server_signal(reason: str) -> bool:
    """True when a failure says something about the whole server, not just the page asked for."""
    return reason in HARD or reason in SOFT


@dataclass(frozen=True)
class Permit:
    granted: bool
    reason: str  # "ok" or why not


def _key(origin: str) -> dict[str, str]:
    return {"PK": f"ORIGIN#{origin}", "SK": "STATE"}


def _ts(value: datetime) -> int:
    return int(value.timestamp())


def state(table, origin: str) -> dict:
    return table.get_item(Key=_key(origin), ConsistentRead=True).get("Item") or {}


def acquire(table, origin: str, now: datetime, daily_cap: int, by: str | None = None,
            interval: timedelta | None = None) -> Permit:
    """`now` is the time of the request about to be made. `by` records who took the permit. `interval`
    (default MIN_INTERVAL) is how long the next request to this server waits; only an operator's
    backfill run and a page run's notice images and files (ASSET_GAP_S, then rest) pass a shorter one."""
    item = state(table, origin)
    if item.get("disabled"):
        log("origin_disabled", origin=origin)
        metrics(OriginDisabled=1)
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
    values = {":next": _ts(now + (interval or MIN_INTERVAL)), ":day": today, ":count": count + 1, ":ver": version,
              ":newver": version + 1, ":now": _ts(now), ":false": False}
    update = "SET next_at = :next, #day = :day, day_count = :count, ver = :newver"
    if by:
        update += ", last_by = :by"
        values[":by"] = by
    try:
        table.update_item(
            Key=_key(origin),
            UpdateExpression=update,
            # a block recorded by an overlapping run since the read above still counts
            ConditionExpression=(
                "(attribute_not_exists(ver) OR ver = :ver) "
                "AND (attribute_not_exists(cooldown_until) OR cooldown_until <= :now) "
                "AND (attribute_not_exists(disabled) OR disabled = :false)"
            ),
            ExpressionAttributeNames={"#day": "day"},
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return Permit(False, "raced")
        raise
    return Permit(True, "ok")


def rest(table, origin: str, at: datetime) -> None:
    """The server's full MIN_INTERVAL from `at` (the end of a burst taken with a shorter `interval`):
    the next list request waits as usual. Never brings the next request closer."""
    try:
        table.update_item(
            Key=_key(origin),
            UpdateExpression="SET next_at = :next ADD ver :one",  # a run that read the old state must re-read
            ConditionExpression="attribute_not_exists(next_at) OR next_at < :next",
            ExpressionAttributeValues={":next": _ts(at + MIN_INTERVAL), ":one": 1},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


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
        UpdateExpression="SET blocks = :zero, soft = :zero REMOVE last_block",
        ExpressionAttributeValues={":zero": 0},
    )


def record_block(table, origin: str, now: datetime, reason: str, retry_after: int = 0) -> dict[str, object]:
    """A hard block or a transient failure of the origin (see the module docstring). Returns the new state."""
    item = state(table, origin)
    blocks, soft, disabled = int(item.get("blocks", 0)), int(item.get("soft", 0)), bool(item.get("disabled"))
    if reason in HARD:
        blocks += 1
        cooldown = min(COOLDOWN_START * (2 ** (blocks - 1)), COOLDOWN_MAX)
        disabled = disabled or blocks >= MAX_CONSECUTIVE_BLOCKS
    else:  # transient: a connection blip just waits for the next run the first time
        soft += 1
        steps = soft if reason in EMPTY else soft - 1
        cooldown = min(COOLDOWN_START * (2 ** (steps - 1)), SOFT_COOLDOWN_MAX) if steps > 0 else timedelta(0)
    if retry_after > 0:
        cooldown = max(cooldown, min(timedelta(seconds=retry_after), RETRY_AFTER_MAX))
    table.update_item(
        Key=_key(origin),
        UpdateExpression=("SET blocks = :b, soft = :s, cooldown_until = :until, last_block = :reason, "
                          "disabled = :d"),
        ExpressionAttributeValues={
            ":b": blocks,
            ":s": soft,
            ":until": _ts(now + cooldown),
            ":reason": reason,
            ":d": disabled,
        },
    )
    if reason not in HARD and soft >= SOFT_NEEDS_HUMAN:
        log("origin_failing", origin=origin, soft=soft)
        metrics(NeedsHuman=1)
    return {"blocks": blocks, "soft": soft, "cooldown_minutes": int(cooldown.total_seconds() // 60),
            "disabled": disabled}


def reenable(table, origin: str) -> None:
    """What a human does after checking a switched-off origin: everything back to the start."""
    table.update_item(
        Key=_key(origin),
        UpdateExpression="SET blocks = :zero, soft = :zero, disabled = :false REMOVE cooldown_until, last_block",
        ExpressionAttributeValues={":zero": 0, ":false": False},
    )


# --- one source's own failures ---------------------------------------------------------------

def source_waiting(source_state: dict, now: datetime) -> bool:
    """A source switched off after hard blocks, or still backing off after its own failures."""
    return bool(source_state.get("disabled")) or int(source_state.get("not_before", 0)) > _ts(now)


def source_failed(table, key: dict[str, str], source_state: dict, now: datetime, reason: str,
                  base: timedelta) -> dict[str, object]:
    """Back one source off: base, 2 x base, ... up to 24 h. Three hard blocks on the same source in
    a row (its path blocked while the rest of the server answers) switch it off for a human. Its own
    trouble (404, a redirect, a changed page) keeps it on, but from the third failure in a row each
    one raises NeedsHuman: the page may have moved for good."""
    fails = int(source_state.get("fails", 0)) + 1
    hard = int(source_state.get("hard_blocks", 0)) + (1 if reason in HARD else 0)
    wait = min(base * (2 ** (fails - 1)), SOURCE_BACKOFF_MAX)
    disabled = bool(source_state.get("disabled")) or hard >= MAX_CONSECUTIVE_BLOCKS
    table.update_item(
        Key=key,
        UpdateExpression="SET fails = :f, hard_blocks = :h, not_before = :t, last_fail = :r, disabled = :d",
        ExpressionAttributeValues={":f": fails, ":h": hard, ":t": _ts(now + wait), ":r": reason, ":d": disabled},
    )
    if reason not in HARD:
        metrics(SourceFailed=1)
        if fails >= SOURCE_NEEDS_HUMAN and not disabled:
            log("source_failing", source=str(key.get("PK", "")), fails=fails, reason=reason)
            metrics(NeedsHuman=1)
    if disabled:
        metrics(NeedsHuman=1)
    return {"fails": fails, "wait_minutes": int(wait.total_seconds() // 60), "source_disabled": disabled}


def source_ok(table, key: dict[str, str], source_state: dict) -> None:
    if any(k in source_state for k in ("fails", "hard_blocks", "not_before", "last_fail")):
        table.update_item(Key=key, UpdateExpression="REMOVE fails, hard_blocks, not_before, last_fail")


def source_skipped(source: str, source_state: dict) -> dict[str, object]:
    """Log a source that is switched off or backing off; a switched-off one needs a human."""
    if source_state.get("disabled"):
        log("source_disabled", source=source)
        metrics(NeedsHuman=1)
        return {"status": "skipped", "reason": "source_disabled"}
    return {"status": "skipped", "reason": "backing_off"}


# --- robots.txt ------------------------------------------------------------------------------

@dataclass(frozen=True)
class RobotsCheck:
    checked: str  # KST date of the last look by hand (one GET of /robots.txt)
    result: str  # what came back
    disallow: tuple[str, ...] = ()  # rules for "User-agent: *" (none names kw-campus); * and $ as in RFC 9309


_NO_FILE = "no robots.txt (404)"
# Every host a collector reads, checked by hand. A new board's host needs a line here first
# (tests/test_robots.py fails without one), and a path its robots.txt disallows is never fetched
# (robots_skip: NeedsHuman). Look again within ROBOTS_MAX_AGE (180 days) and update the date;
# robots_age_check raises NeedsHuman every weekday morning until then.
ROBOTS: dict[str, RobotsCheck] = {
    "www.kw.ac.kr": RobotsCheck("2026-11-06", "no robots.txt (200 with the site's HTML error page, no rules)"),
    "aicon.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    # E1
    "ei.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "ee.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "elcomm.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "radiowave.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "electric.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "snme.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "semicon.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "law.kw.ac.kr": RobotsCheck("2026-11-06", "User-agent: * / Allow: /"),
    # Not collected; shares E1. Recorded so nobody adds its boards by mistake (Crawl-delay: 10).
    "job.kw.ac.kr": RobotsCheck("2026-11-06", "User-agent: * with Disallow rules, Crawl-delay: 10", (
        "/recommend/", "/mobile/recommend/", "/internship/", "/job/", "/give/story.php", "/notice/news.php")),
    # E2
    "archi.kw.ac.kr": RobotsCheck("2026-11-06", "no robots.txt (302 to an error page)"),
    "chss.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "korean.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "english.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "psy.kw.ac.kr": RobotsCheck("2026-11-06", "no robots.txt (302 to an error page)"),
    "dnaci.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "kwpa.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "biz.kw.ac.kr": RobotsCheck("2026-11-06", "no robots.txt (302 to an error page)"),
    # E3
    "kwlife.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "kwlaw.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "kwgame.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "kwsports.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "ingenium.kw.ac.kr": RobotsCheck("2026-11-06", "User-agent: * / Allow: /"),
    # E4
    "chemng.kw.ac.kr": RobotsCheck("2026-11-06", _NO_FILE),
    "sports.kw.ac.kr": RobotsCheck("2026-11-06", "User-agent: * / Allow: /"),
    # Wix (auto-generated files: everything but lightbox URLs; only AdsBot and PetalBot get more)
    "www.kwuarchitecture.com": RobotsCheck("2026-11-06", "User-agent: * / Allow: / / Disallow: *?lightbox=",
                                           ("*?lightbox=",)),
    "www.kwmedia.info": RobotsCheck("2026-11-06", "User-agent: * / Allow: / / Disallow: *?lightbox=",
                                    ("*?lightbox=",)),
}


def _rule(pattern: str) -> re.Pattern[str]:
    body = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
    return re.compile(body + ("$" if pattern.endswith("$") else ""))


def robots_allows(url: str) -> bool:
    """False for a host never checked by hand, or a path its robots.txt disallows (fail closed)."""
    parts = urlsplit(url)
    check = ROBOTS.get(parts.hostname or "")
    if check is None:
        return False
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    # "*?lightbox=" style rules match anywhere after the first character, like Google's reading
    return not any(_rule(rule if rule.startswith("/") else "/" + rule).match(target) for rule in check.disallow)


def robots_skip(source: str, url: str) -> dict[str, object]:
    log("robots_skip", source=source, host=urlsplit(url).hostname)
    metrics(NeedsHuman=1, RobotsSkipped=1)
    return {"status": "skipped", "reason": "robots"}


def stale_robots(now: datetime) -> list[str]:
    """Hosts whose robots.txt record is older than ROBOTS_MAX_AGE (KST dates), oldest first."""
    today = now.astimezone(KST).date()
    old = [(date.fromisoformat(check.checked), host) for host, check in ROBOTS.items()
           if today - date.fromisoformat(check.checked) > ROBOTS_MAX_AGE]
    return [host for _, host in sorted(old)]


def robots_age_check(now: datetime) -> int:
    """NeedsHuman while any record is too old: look at those hosts' /robots.txt again by hand and
    update ROBOTS (the date, the result, the Disallow rules). Returns how many are too old."""
    hosts = stale_robots(now)
    if hosts:
        oldest = ROBOTS[hosts[0]].checked
        log("robots_stale", hosts=len(hosts), oldest=oldest, first=hosts[0])
        metrics(NeedsHuman=1, RobotsStale=len(hosts))
    return len(hosts)

"""The sweep: a nightly safety net that reads again, a little at a time, older notices with no finished read.

It runs in its own phase of the collector Lambda (event {"phase": "sweep"}, infra SWEEP_SCHEDULE: every night,
weekends too, from 22:00 KST, after the day's last list run at 21:00). By then no list run is left today, so a
server's spare (extract.spare) is the day's leftover. A weekday leaves little on the department servers (31 of
their 32 requests go to list runs) and on www; the weekends and 총동문회's server leave plenty. The global kill
switch (/kw/collector/enabled) and its own (SWITCH_PARAM: on when missing or "true", "1", "on"; any other value
stops it) must both allow it.

Candidates: notices within their TTL with no `read` record, or one that ended failed or partial (no retry still
planned), last tried over AGAIN_AFTER ago; swept fewer than MAX_SWEEPS times (the notice's `sweeps`) and not
told to wait (`sweep_after`). A notice with a page-queue row is the queue's.

Each run looks at a few sources: SOURCES_PER_RUN, round-robin from a cursor in the state item MARK#sweep/STATE,
one Query each on its partition (PK SRC#<source>) projecting only CANDIDATE_ATTRIBUTES. A source holds a few
hundred notices at most (the 120-day TTL): one or two Query pages, no scan and no index. Once every source
walked has nothing left the sweep looks at one source a run.

From the sources walked:
1. Notices with no page to read get their final record without a request (RECORDS_PER_RUN at most, and only
   while time allows): 현장실습 title_only_robots, the Wix feeds no_reader, a 총학생회 post never read (no
   `read`, no facts) and with no queued text seed_no_text or no_text. A 총학생회 post read before keeps its record.
2. The newest candidate of each server is read, one a server and run, through the same code, gates, gaps and
   asset rules as a queued page (extract.read_page, notice_assets.page_run), only from the server's spare above
   a margin (notice_assets.margin: a tenth of its cap, at least 1), on every server an image or file is on.
   No page is fetched while no model provider key is there, nor (for a notice without facts) while the sweep has
   no model call left.
3. The model is asked only for what the stored facts lack: no facts, a source they didn't use, or a changed
   text. Images or files found but not read tonight (no spare request, no time, a failed request) mean no call
   on the text alone: the notice waits for another night. On its last sweep it gets a call with what was read
   if its text is long enough (extract.MIN_BODY_CHARS), else only a partial read record. A read that brings less
   than the facts came from (a strict subset of sources_used) never replaces them: only its read record is
   written, with the earlier counts. Facts of the same text are merged. Model calls come from the day's cap
   minus a reserve for new notices (extract.sweep_reserve).
Every fetch counts a sweep on the notice (`sweeps`) and makes it wait for the next night (`sweep_after`), both
written before the request, so a crash mid-read counts too and no notice starves the others. A read that ends
(a record or facts written) lifts the wait.
The state item keeps, per source, how many candidates its last walk found (opsdigest: "다시 읽기 남음").
Metrics: SweepRead (a page read), SweepRecorded (a record written without a model call), SweepError.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from kwcampus import extract, params, politeness
from kwcampus.config import KST
from kwcampus.fetch import fetch_list_page
from kwcampus.obs import log, metrics
from kwcampus.routing import name_risk

STATE = {"PK": "MARK#sweep", "SK": "STATE"}
SWITCH_PARAM = "/kw/collector/sweep-enabled"  # missing, "true", "1" or "on" (any case): on; any other value: off
SWITCH_ON = frozenset({"true", "1", "on"})
SOURCES_PER_RUN = 3
IDLE_SOURCES_PER_RUN = 1
RECORDS_PER_RUN = 25
RECORDS_MIN_S = 20.0  # less Lambda time than this: no records either
AGAIN_AFTER = timedelta(days=7)
MAX_SWEEPS = 2
CANDIDATE_ATTRIBUTES = ("SK", "source", "url", "seed", "removed", "ttl", "posted_date", "first_seen", "read",
                        "extract_v", "sweeps", "sweep_after")
COUNTED = frozenset({"recorded", "extracted", "failed", "no_body", "model_failed", "waiting", "blocked", "stop",
                     "no_model", "unchanged"})  # outcomes after which the notice is no candidate tonight


def enabled() -> bool:
    """The sweep's own switch, read fresh each run: on while SWITCH_PARAM is missing or one of SWITCH_ON."""
    raw = params.get(SWITCH_PARAM, ttl_s=0)
    return raw is None or raw.strip().lower() in SWITCH_ON


def sources() -> list[str]:
    from kwcampus import public_data  # it imports extract: here, not at the top

    return list(public_data.SOURCES)


def is_candidate(item: dict, now: float) -> bool:
    if item.get("removed") or (item.get("ttl") and int(item["ttl"]) <= now):
        return False
    if int(item.get("sweeps") or 0) >= MAX_SWEEPS or int(item.get("sweep_after") or 0) > now:
        return False
    read = item.get("read")
    if not isinstance(read, dict):
        return True
    if read.get("status") not in ("failed", "partial") or extract.pending(read, now):
        return False
    return int(read.get("last_try_at") or 0) <= now - AGAIN_AFTER.total_seconds()


def candidates(table, source: str, now: float, queued: set[str] | frozenset[str]) -> list[dict]:
    """A source's candidates, newest first: one Query of its partition, CANDIDATE_ATTRIBUTES only."""
    names = {f"#a{i}": a for i, a in enumerate(CANDIDATE_ATTRIBUTES)}
    kwargs = {"KeyConditionExpression": Key("PK").eq(f"SRC#{source}") & Key("SK").begins_with("N#"),
              "ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}
    found = []
    while True:
        page = table.query(**kwargs)
        found += [{**i, "source": i.get("source") or source} for i in page["Items"]
                  if is_candidate(i, now) and f"SRC#{source}|{i['SK']}" not in queued]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(found, key=_newest, reverse=True)


def _newest(item: dict) -> tuple[str, int]:
    return str(item.get("posted_date") or ""), int(item.get("first_seen") or 0)


def no_page(item: dict) -> str | None:
    """Why there is no page to read (the title is all there is), "" when it isn't the sweep's to record (a
    총학생회 post read before), or None when there is a page."""
    found = extract.reader_of(item)
    if found == extract.FEED:  # 총학생회: its text came with its list, on a queue row long gone
        if isinstance(item.get("read"), dict) or item.get("extract_v"):
            return ""
        return "seed_no_text" if item.get("seed") else "no_text"
    return found if isinstance(found, str) else None


def collect(now: datetime, table, fetch=fetch_list_page, clock: Callable[[], datetime] | None = None,
            time_left: Callable[[], float] | None = None, cap: int | None = None, anthropic_client=None,
            openai_send: Callable[[dict, str], dict] = extract.openai_post, read=None, inner_transport=None,
            sleep=None) -> dict[str, object]:
    """One night run (see the module docstring). Never raises: a failure is logged (sweep_error, SweepError)."""
    clock = clock or (lambda: now)
    try:
        if not enabled():
            log("sweep_skip", reason="switched_off")
            return {"status": "off"}
        if time_left is not None and time_left() < extract.NEEDED_S:
            return {"status": "skipped", "reason": "no_time"}
        cap = extract.daily_cap() if cap is None else cap
        asks = {"anthropic": lambda item, body, images: extract.ask_anthropic(item, body, anthropic_client, images),
                "openai": lambda item, body, images: extract.ask_openai(item, body, openai_send, images)}
        counts: Counter = Counter()

        def assets(notice: dict, page, url: str, skip: str | None, html: str, unchanged, already: frozenset[str]):
            from kwcampus import notice_assets

            got = notice_assets.page_run(table, notice, page, url, html, clock, time_left, read, inner_transport,
                                         sleep, skip=skip, unchanged=unchanged, already=already, keep=True)
            if got.how not in ("read", "none", "unchanged", "privacy"):
                counts[f"assets_{got.how}"] += 1
            return got

        queued = {f"{r.get('notice_pk')}|{r.get('notice_sk')}" for r in extract.queue_items(table)}
        counts.update(run(now, table, clock, now.astimezone(KST).date().isoformat(), cap, asks, fetch, assets,
                          time_left, queued))
        counts = Counter({k: v for k, v in counts.items() if v})
        return {"status": "ok" if counts else "idle", **counts}
    except Exception as e:  # noqa: BLE001 - a safety net must never be what fails
        log("sweep_error", error=e.__class__.__name__)
        metrics(SweepError=1)
        return {"status": "error"}


def run(now: datetime, table, clock, today: str, cap: int, asks: dict, fetch, assets, time_left=None,
        queued: set[str] | frozenset[str] = frozenset()) -> Counter:
    counts: Counter = Counter()
    ts = now.timestamp()
    state = table.get_item(Key=STATE, ConsistentRead=True).get("Item") or {}
    names = sources()
    left = {k: int(v) for k, v in (state.get("left") or {}).items() if k in names}
    idle = len(left) == len(names) and not any(left.values())
    start = int(state.get("next") or 0) % len(names)
    walk = [names[(start + i) % len(names)] for i in range(min(IDLE_SOURCES_PER_RUN if idle else SOURCES_PER_RUN,
                                                                len(names)))]
    found = {source: candidates(table, source, ts, queued) for source in walk}
    for source in walk:
        left[source] = len(found[source])

    def time_for(seconds: float) -> bool:
        return time_left is None or time_left() >= seconds

    readable, written = [], 0  # 1. no page to read: the final record, no request
    for source in walk:
        for item in found[source]:
            reason = no_page(item)
            if reason is None:
                readable.append(item)
            elif reason == "":
                left[source] -= 1  # not the sweep's to record
            elif written < RECORDS_PER_RUN and time_for(RECORDS_MIN_S) and _title_only(table, item, reason, clock()):
                written += 1
                left[source] -= 1
                counts["sweep_recorded"] += 1

    if readable:  # 2. one notice a server, the newest first
        providers = extract.providers_ready()
        if not providers:  # no page is fetched for a call that can't be made
            counts["sweep_no_provider"] += 1
            readable = []
        model_left = extract.used_today(table, today) < cap - _reserve()
        if not model_left:
            readable = [i for i in readable if i.get("extract_v")]  # no call left: only notices with facts
        picks: dict[str, dict] = {}
        for item in sorted(readable, key=_newest, reverse=True):
            picks.setdefault(extract.page_for(item)[0].origin, item)
        for item in picks.values():
            if not time_for(extract.NEEDED_S):
                counts["sweep_no_time"] += 1
                break
            outcome = _one(table, item, clock, today, cap, asks, providers, fetch, assets)
            counts[f"sweep_{outcome}"] += 1
            if outcome in COUNTED:
                left[str(item["source"])] -= 1
    table.put_item(Item={**STATE, "next": (start + len(walk)) % len(names), "left": left,
                         "walked_at": {**(state.get("walked_at") or {}), **{s: int(ts) for s in walk}}})
    if counts:
        log("sweep_run", walked=len(walk), left=sum(max(0, v) for v in left.values()), **counts)
    return counts


def _reserve() -> int:
    try:
        return extract.sweep_reserve()
    except Exception:  # noqa: BLE001 - an SSM hiccup: the default reserve
        return extract.DEFAULT_SWEEP_RESERVE


def _title_only(table, item: dict, reason: str, now: datetime) -> bool:
    notice = table.get_item(Key={"PK": f"SRC#{item['source']}", "SK": item["SK"]}, ConsistentRead=True).get("Item")
    if not notice or notice.get("removed") or (reason in ("seed_no_text", "no_text") and no_page(notice) == ""):
        return False
    if extract.store_read(table, notice, extract.read_record("title_only", "none", extract.no_assets(), 0, now,
                                                             reason)):
        metrics(SweepRecorded=1)
        return True
    return False


def _tomorrow(now: datetime) -> int:
    local = now.astimezone(KST)
    return int((datetime(local.year, local.month, local.day, tzinfo=KST) + timedelta(days=1)).timestamp())


def _update(table, notice: dict, update: str, values: dict | None = None) -> bool:
    """False when the notice was taken down meanwhile (left alone)."""
    kwargs = {"ExpressionAttributeValues": values} if values else {}
    try:
        table.update_item(Key={"PK": notice["PK"], "SK": notice["SK"]}, UpdateExpression=update,
                          ConditionExpression="attribute_exists(PK) AND attribute_not_exists(removed)", **kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


def _begin(table, notice: dict, now: datetime) -> bool:
    """Before the request: one more sweep of this notice, and not again before the next night, unless the read
    ends (_settle). A crash mid-read leaves both."""
    return _update(table, notice, "SET sweeps = if_not_exists(sweeps, :zero) + :one, sweep_after = :t",
                   {":zero": 0, ":one": 1, ":t": _tomorrow(now)})


def _settle(table, notice: dict) -> None:
    """The read ended (its record or facts written): no wait."""
    _update(table, notice, "REMOVE sweep_after")


def _one(table, item: dict, clock, today: str, cap: int, asks: dict, providers: list[str], fetch, assets) -> str:
    """Read one candidate's page; its outcome (the notice's records written as it goes)."""
    from kwcampus import notice_assets

    notice = table.get_item(Key={"PK": f"SRC#{item['source']}", "SK": item["SK"]}, ConsistentRead=True).get("Item")
    if not notice or notice.get("removed") or extract.page_for(notice) is None:
        return "gone"
    page, url = extract.page_for(notice)
    if not politeness.robots_allows(url):
        politeness.robots_skip(str(notice.get("source")), url)
        return "recorded" if _title_only(table, notice, "robots", clock()) else "gone"
    left = extract.spare(table, page, clock()) - notice_assets.margin(page)
    if left < 1:
        return "no_spare"
    short = left > 1 and not name_risk(str(notice.get("category_id") or ""), str(notice.get("title") or ""))
    gap = timedelta(seconds=max(politeness.ASSET_GAP_S, politeness.crawl_delay(url))) if short else None
    if not politeness.acquire(table, page.origin, clock(), daily_cap=page.daily_cap, interval=gap).granted:
        return "gate"
    try:
        if not _begin(table, notice, clock()):  # counted and told to wait before the request: a crash still counts
            return "gone"
        metrics(BodyFetched=1, SweepRead=1)
        result = fetch(url, encoding=page.encoding, errors="replace")
        done = extract.read_page(table, {}, notice, page, result, clock,
                                 partial(assets, notice, page, url, None if left > 1 else "budget"))
    finally:
        if short:
            politeness.rest(table, page.origin, clock())
    prev = notice.get("read") if isinstance(notice.get("read"), dict) else {}
    if not isinstance(done, extract.PageRead):
        outcome, _, reason = done
        if outcome == "failed":  # a 404, a redirect: noted
            extract.store_read(table, notice, extract.read_record(
                "failed", "failed", extract.no_assets(), int(prev.get("tries") or 0) + 1, clock(), reason))
        if outcome == "unchanged":  # read before with this text and these assets: nothing to wait for
            _settle(table, notice)
        return outcome  # blocked, no_body, failed: not again tonight
    return _conclude(table, notice, prev, done, clock, today, cap, asks, providers)


def _conclude(table, notice: dict, prev: dict, done, clock, today: str, cap: int, asks: dict,
              providers: list[str]) -> str:
    """The read record, and a model call only for what the stored facts lack."""
    now = clock()
    original, digest, got = done.original, done.digest, done.got
    body = "ok" if len(original.strip()) >= extract.MIN_BODY_CHARS else "empty"
    facts = notice.get("extracted") if isinstance(notice.get("extracted"), dict) else None
    used = set((facts or {}).get("sources_used") or [])
    brought = set(extract.sources_of(original, got.images, got.files))
    same = extract.same_text(notice, digest, "page")
    last = int(notice.get("sweeps") or 0) + 1 >= MAX_SWEEPS  # this read is the notice's last sweep
    record = extract.read_record(extract.read_status(body, got), body, got, int(prev.get("tries") or 0) + 1, now,
                                 extract._reason(got), sent_fp=str(prev.get("sent_fp") or ""))

    def recorded(rec: dict) -> str:
        extract.store_read(table, notice, rec)
        _settle(table, notice)
        metrics(SweepRecorded=1)
        return "recorded"

    if got.missing():  # images or files found but not read tonight: never a call on the text alone
        if not last:
            return "waiting"  # another night (sweep_after is set)
        if body == "empty":  # the last sweep, and too little text for a call: what was read, partial
            return recorded(record)
    if body == "empty" and not (got.images or got.files):  # nothing to read but the title
        if facts is None:
            extract.store_facts(table, notice, None, digest, "", now, read=record)
            _settle(table, notice)
            metrics(SweepRecorded=1)
            return "recorded"
        return recorded(record)
    if facts is not None and brought < used:  # poorer than what the facts came from: they stay, with their counts
        return recorded({**record, "status": prev.get("status") or record["status"],
                         "images": prev.get("images") or record["images"], "files": prev.get("files") or record["files"]})
    if facts is not None and same and brought <= used:  # nothing the facts didn't come from
        return recorded(record)
    if extract.used_today(table, today) >= cap - _reserve() or not extract._take_slot(table, today, cap - _reserve()):
        return "no_model"  # read, but no model call left today (or another run took it): another night
    sent, text = extract.compose(original, got.files)
    try:
        raw, usage, model, provider = extract._ask(providers, notice, sent, tuple(got.images), asks)
    except extract._Stop:
        return "stop"  # another night
    except extract.ModelError as e:
        metrics(ExtractFail=int(e.kind != "refused"), ExtractRefused=int(e.kind == "refused"))
        log("sweep_model_failed", notice=notice["SK"], reason=e.kind)
        if e.kind == "retry_later":
            return "stop"
        extract.store_read(table, notice, {**record, "status": "partial" if facts else "failed",
                                           "last_reason": f"model_{e.kind}"})
        _settle(table, notice)
        return "model_failed"
    new, dropped = extract.validate(raw, notice, sent, text, extract.sources_of(original, got.images, got.files))
    if facts is not None and same:  # the same text read before: nothing it found is lost
        new = extract.merge_facts(facts, new)
    record["sent_fp"] = extract.sent_fingerprint(digest, got.read_keys)
    if not extract.store_facts(table, notice, new, digest, model, clock(), read=record):
        return "gone"
    _settle(table, notice)
    tokens_in, tokens_out = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    log("sweep_extract_ok", notice=notice["SK"], provider=provider, model=model, dropped=dropped,
        input_tokens=tokens_in, output_tokens=tokens_out, read=record["status"])
    metrics(ExtractOk=1, ExtractInputTokens=tokens_in, ExtractOutputTokens=tokens_out)
    return "extracted"


def remaining(table) -> tuple[int, bool] | None:
    """(candidates left at the sources' last walks, whether every source has been walked), or None before the
    sweep ever ran. For the opsdigest's "다시 읽기 남음"."""
    state = table.get_item(Key=STATE).get("Item")
    if not state:
        return None
    left = {k: int(v) for k, v in (state.get("left") or {}).items()}
    return sum(max(0, v) for v in left.values()), set(sources()) <= set(left)

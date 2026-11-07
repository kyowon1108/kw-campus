"""Lambda: collect page 1 of the main notice board into kw-public, then the other boards.

Runs on a schedule, only when the kill switch is on and each server's gate allows it. The main
board goes first with one request to www.kw.ac.kr; the department servers get one request each
(the 인공지능융합대학 widget shares its server's), the Wix blog feeds one per host every 2 hours, and
총동문회, 총학생회 and 현장실습 one each every 2 or 4 hours (smallboards, each on its own server).
Every couple of hours a second www request reads the 취업게시판 or the FAQ, 61 s after the first.

The page runs (event {"phase": "pages"}, 10 minutes after each daytime list run) read queued notice
pages instead, one per server, and pull facts out of them (extract.py). Same kill switch. The night runs
(event {"phase": "sweep"}, every night from 22:00) read again older notices with no finished read, from the
day's leftover requests (sweep.py; also its own switch).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

import boto3

from kwcampus import (
    aicon,
    deptboards,
    extract,
    params,
    politeness,
    smallboards,
    store,
    sweep,
    wixfeeds,
    wwwboards,
)
from kwcampus.config import (
    COLLECTOR_ENABLED_PARAM,
    NOTICE_SOURCE,
    PUBLIC_TABLE,
    WWW_DAILY_CAP,
)
from kwcampus.fetch import empty_reason, fetch_list_page
from kwcampus.kwnotice.domain import ParseIssueCode
from kwcampus.kwnotice.list_parser import parse_list_soup
from kwcampus.kwnotice.parsing_shared import boundary_or_issue, soup_from_boundary
from kwcampus.kwnotice.source import NOTICE_HOST, build_list_url
from kwcampus.obs import log, metrics

DAILY_CAP = WWW_DAILY_CAP
_STATE = {"PK": store.source_pk(NOTICE_SOURCE), "SK": "STATE"}


def run(now: datetime, table, fetch=fetch_list_page, clock: Callable[[], datetime] | None = None) -> dict[str, object]:
    clock = clock or (lambda: now)
    if not params.is_true(COLLECTOR_ENABLED_PARAM):
        log("collector_skip", reason="disabled")
        return {"status": "disabled"}
    url = str(build_list_url(1))
    if not politeness.robots_allows(url):
        return politeness.robots_skip(NOTICE_SOURCE, url)
    state = table.get_item(Key=_STATE, ConsistentRead=True).get("Item") or {}
    if politeness.source_waiting(state, now):
        return politeness.source_skipped(NOTICE_SOURCE, state)

    permit = politeness.acquire(table, NOTICE_HOST, clock(), daily_cap=DAILY_CAP)
    if not permit.granted:
        log("collector_skip", reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}

    result = fetch(url)
    records, issues = (), ()
    if result.html is not None:
        boundary, _ = boundary_or_issue(result.html)
        if boundary is not None:
            parsed = parse_list_soup(soup_from_boundary(boundary))
            records = parsed.records
            issues = tuple(i for i in parsed.issues if i.code != ParseIssueCode.MALFORMED_MARKUP)  # rows only
    if issues:  # rows with a 분류 the parser doesn't know (a new category, say): counts only
        codes = Counter(str(i.code) for i in issues)
        log("collector_row_issues", **{k.lower(): v for k, v in codes.items()})
        metrics(RowIssues=len(issues))
    reason = result.block_reason or (None if records or issues else empty_reason(result.html))
    if reason:
        if politeness.is_server_signal(reason):
            state_after = politeness.record_block(table, NOTICE_HOST, clock(), reason, result.retry_after)
            metrics(Fetched=1, Blocked=int(reason in politeness.HARD), Transient=int(reason not in politeness.HARD),
                    ParseEmpty=int(reason == "no_rows"))
        else:  # this page's own problem: the job board, FAQ and campus pages on www go on
            state_after = politeness.source_failed(table, _STATE, state, now, reason, politeness.COOLDOWN_START)
            metrics(Fetched=1)
        log("collector_blocked", reason=reason, status=result.status, **state_after)
        return {"status": "blocked", "reason": reason}
    if not records:  # rows came, none usable: a parse problem, not a block
        politeness.record_success(table, NOTICE_HOST)
        metrics(Fetched=1, ParseEmpty=1)
        return {"status": "no_rows", "issues": len(issues)}

    seeded = store.is_seeded(table, NOTICE_SOURCE)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        outcome = store.upsert(table, store.notice_item(NOTICE_SOURCE, record, now, seed=not seeded))
        counts[outcome] += 1
    if not seeded:
        store.mark_seeded(table, NOTICE_SOURCE, now)
    politeness.record_success(table, NOTICE_HOST)
    politeness.source_ok(table, _STATE, state)

    log("collector_ok", rows=len(records), seeded_now=not seeded, **counts)
    metrics(Fetched=1, Blocked=0, Inserted=counts["inserted"], Updated=counts["updated"])
    return {"status": "ok", "rows": len(records), "seed_run": not seeded, **counts}


def handler(event, context):
    table = boto3.resource("dynamodb").Table(PUBLIC_TABLE)
    now = datetime.now(UTC)

    def clock() -> datetime:  # the gate counts from the moment of each request, not the run's start
        return datetime.now(UTC)

    time_left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else None
    phase = (event or {}).get("phase")
    if phase in ("pages", "sweep"):
        if not params.is_true(COLLECTOR_ENABLED_PARAM):
            log("collector_skip", reason="disabled")
            return {"status": "disabled"}
        if phase == "sweep":  # its own switch too (sweep.SWITCH_PARAM); it never raises
            return {"sweep": sweep.collect(now, table, clock=clock, time_left=time_left)}
        return {"pages": extract.collect(now, table, clock=clock, time_left=time_left)}
    try:
        out = run(now, table, clock=clock)
    except Exception as e:  # noqa: BLE001 - the other servers must still get their turn
        log("main_error", error=e.__class__.__name__)
        out = {"status": "error"}
        try:  # but only with the kill switch known to be on (cached by then, unless its read failed)
            if not params.is_true(COLLECTOR_ENABLED_PARAM):
                return out
        except Exception:  # noqa: BLE001
            return out
    if out.get("status") != "disabled":  # same kill switch; each server keeps its own gate
        # www's extra boards go last: they may wait out the 60 s gap after the main board's request.
        for name, source in (("aicon", aicon), ("dept", deptboards), ("wix", wixfeeds), ("small", smallboards),
                             ("www", wwwboards)):
            try:
                out = {**out, name: source.collect(now, table, clock=clock, time_left=time_left)}
            except Exception as e:  # noqa: BLE001 - one site's trouble must not fail the main run or the others
                log(f"{name}_error", error=e.__class__.__name__)
                out = {**out, name: {"status": "error"}}
    return out

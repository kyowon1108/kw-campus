"""Lambda: collect page 1 of the main notice board into kw-public, then the other boards.

Runs on a schedule, only when the kill switch is on and each server's gate allows it. The main
board goes first with one request to www.kw.ac.kr; the department servers get one request each.
Every couple of hours a second www request reads the 취업게시판 or the FAQ, 61 s after the first.
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3

from kwcampus import aicon, deptboards, params, politeness, store, wwwboards
from kwcampus.config import COLLECTOR_ENABLED_PARAM, NOTICE_SOURCE, PUBLIC_TABLE
from kwcampus.fetch import fetch_list_page
from kwcampus.kwnotice.list_parser import parse_list_soup
from kwcampus.kwnotice.parsing_shared import boundary_or_issue, soup_from_boundary
from kwcampus.kwnotice.source import NOTICE_HOST, build_list_url
from kwcampus.obs import log, metrics

DAILY_CAP = 40  # weekday 09:00-18:59 every 20 min = 30 runs


def run(now: datetime, table, fetch=fetch_list_page) -> dict[str, object]:
    if not params.is_true(COLLECTOR_ENABLED_PARAM):
        log("collector_skip", reason="disabled")
        return {"status": "disabled"}

    permit = politeness.acquire(table, NOTICE_HOST, now, daily_cap=DAILY_CAP)
    if not permit.granted:
        log("collector_skip", reason=permit.reason)
        return {"status": "skipped", "reason": permit.reason}

    result = fetch(str(build_list_url(1)))
    records = ()
    if result.html is not None:
        boundary, _ = boundary_or_issue(result.html)
        if boundary is not None:
            parsed = parse_list_soup(soup_from_boundary(boundary))
            records = parsed.records
    if result.block_reason or not records:
        reason = result.block_reason or "no_rows"
        state = politeness.record_block(table, NOTICE_HOST, now, reason)
        log("collector_blocked", reason=reason, status=result.status, **state)
        metrics(Fetched=1, Blocked=1)
        return {"status": "blocked", "reason": reason}

    seeded = store.is_seeded(table, NOTICE_SOURCE)
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for record in records:
        outcome = store.upsert(table, store.notice_item(NOTICE_SOURCE, record, now, seed=not seeded))
        counts[outcome] += 1
    if not seeded:
        store.mark_seeded(table, NOTICE_SOURCE, now)
    politeness.record_success(table, NOTICE_HOST)

    log("collector_ok", rows=len(records), seeded_now=not seeded, **counts)
    metrics(Fetched=1, Blocked=0, Inserted=counts["inserted"], Updated=counts["updated"])
    return {"status": "ok", "rows": len(records), "seed_run": not seeded, **counts}


def handler(event, context):
    table = boto3.resource("dynamodb").Table(PUBLIC_TABLE)
    now = datetime.now(UTC)
    out = run(now, table)
    if out.get("status") != "disabled":  # same kill switch; each server keeps its own gate
        # www's extra boards go last: they may wait out the 60 s gap after the main board's request.
        time_left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else None
        for name, source in (("aicon", aicon), ("dept", deptboards), ("www", wwwboards)):
            try:
                extra = {"time_left": time_left} if source is wwwboards and time_left else {}
                out = {**out, name: source.collect(now, table, **extra)}
            except Exception as e:  # noqa: BLE001 - one site's trouble must not fail the main run or the others
                log(f"{name}_error", error=e.__class__.__name__)
                out = {**out, name: {"status": "error"}}
    return out

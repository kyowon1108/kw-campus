"""Lambda: has a school page the rules or the knowledge base were built from changed? Mondays 09:00 KST.

The pages (WATCHES) are the ones kw-mcp's docs/rules-sources.md and docs/knowledge-sources.md list as
sources: five regulations on rule.kw.ac.kr and the 학사안내·장학 pages on www.kw.ac.kr. Nothing else is
fetched.

- A regulation (rule.kw.ac.kr lawFullView.do?SEQ=<n>) lists every revision in its 연혁 select
  (SEQ_HISTORY, with the revision date). A newest SEQ_HISTORY other than the stored one is a change.
- A www page is a change when the SHA-256 of its main content's text (.major-contents-box, whitespace
  collapsed) differs from the stored one.

The last state lives in kw-public: PK "RULEWATCH#<id>", SK "STATE" (seq_history and rev_date, or hash;
checked_at, changed_at). A page seen for the first time is only recorded. Changes go to #운영 in one
message, a line each: "📘 학칙이 바뀌었어요 (2026.10.28 → 2026.11.20) · <link> · 지식베이스 다시 만들기 필요"
(an "ops" job on the Discord queue; the sender posts it). A page that can't be read (a changed layout,
an error page) is left as it was and raises NeedsHuman.

Requests follow the collectors' rules: robots records (politeness.ROBOTS, fail closed), the per-server
gate (politeness.acquire: one request a minute, cool-downs, the daily cap), the same kill switch
(/kw/collector/enabled). rule.kw.ac.kr is its own server. www.kw.ac.kr shares the notice collector's gate
and daily cap, and this run takes only what the cap has to spare after today's list runs (extract.spare),
so a list run never loses its turn; the regulations go first, so the 09:00 list run is done before the
first www request. Pages left without a turn are checked first next week (oldest check first).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3
from bs4 import BeautifulSoup

from kwcampus import extract, params, politeness
from kwcampus.config import COLLECTOR_ENABLED_PARAM, KST, PUBLIC_TABLE, WWW_DAILY_CAP
from kwcampus.dispatcher import send_jobs
from kwcampus.fetch import WORST_CASE_S, fetch_list_page
from kwcampus.kwnotice.source import NOTICE_HOST
from kwcampus.obs import log, metrics

RULE_HOST = "rule.kw.ac.kr"  # its own server (128.134.45.40), so its own gate
RULE_DAILY_CAP = 8  # five regulations a week
PAGE_SELECTOR = ".major-contents-box"
_WWW = extract.Page(NOTICE_HOST, WWW_DAILY_CAP, NOTICE_HOST, PAGE_SELECTOR)
_RULE = "https://rule.kw.ac.kr/lmxsrv/law/lawFullView.do?SEQ={}"
_LIFE = "https://www.kw.ac.kr/ko/life/{}.jsp"
_SHOW_DATE = re.compile(r"showDate\('(\d{4})(\d{2})(\d{2})'")
MAX_SEQ_DIGITS = 9  # SEQ_HISTORY values are three digits today
TRIES = 3  # permits a run asks for per request when another run just took the server's minute


@dataclass(frozen=True)
class Watch:
    id: str
    name: str  # as the message says it ("학칙이 바뀌었어요")
    url: str
    kind: str  # "rule" or "page"


WATCHES: tuple[Watch, ...] = (
    Watch("hakchik", "학칙", _RULE.format(21), "rule"),
    Watch("sihaeng", "학칙 시행세칙", _RULE.format(22), "rule"),
    Watch("damajor-isu", "다전공 이수에 관한 내규", _RULE.format(95), "rule"),
    Watch("damajor-unyeong", "다전공 운영에 관한 내규", _RULE.format(96), "rule"),
    Watch("early-grad", "조기졸업에 관한 규정", _RULE.format(216), "rule"),
    Watch("info01", "교과안내 페이지", _LIFE.format("bachelor_info01"), "page"),
    Watch("info03", "수업안내 페이지", _LIFE.format("bachelor_info03"), "page"),
    Watch("info07", "수료·졸업요건 안내 페이지", _LIFE.format("bachelor_info07"), "page"),
    Watch("info08", "학적변동(휴학·복학) 안내 페이지", _LIFE.format("bachelor_info08"), "page"),
    Watch("info11", "다전공 이수제도 안내 페이지", _LIFE.format("bachelor_info11"), "page"),
    Watch("info12", "입학연도별 졸업학점 안내 페이지", _LIFE.format("bachelor_info12"), "page"),
    Watch("summary", "장학안내 페이지", _LIFE.format("summary"), "page"),
    Watch("scholar01", "교내장학금 페이지", _LIFE.format("scholarship01"), "page"),
    Watch("scholar02", "교외장학금 페이지", _LIFE.format("scholarship02"), "page"),
)


def newest_revision(html: str) -> tuple[int, str] | None:
    """(SEQ_HISTORY, "2026.10.28") of the newest revision in the 연혁 select, or None without one."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.select_one('select[name="SEQ_HISTORY"]')
    best = None
    for option in select.find_all("option") if select else []:
        value = str(option.get("value") or "")
        if not (value.isascii() and value.isdigit() and len(value) <= MAX_SEQ_DIGITS):
            continue  # anything else (a huge number, junk) is no revision of ours
        m = _SHOW_DATE.search(str(option))
        day = ".".join(m.groups()) if m else ""
        if best is None or int(value) > best[0]:
            best = (int(value), day)
    return best


def page_hash(html: str) -> str | None:
    """SHA-256 of the main content's text, whitespace collapsed; None when the page has no main content."""
    node = BeautifulSoup(html, "html.parser").select_one(PAGE_SELECTOR)
    if node is None:
        return None
    for tag in node.find_all(["script", "style", "noscript"]):
        tag.decompose()
    text = " ".join(node.get_text(" ").split())
    return hashlib.sha256(text.encode()).hexdigest() if text else None


def _subject(name: str) -> str:
    """"학칙" -> "학칙이", "내규" -> "내규가" (the last syllable's final consonant)."""
    last = name[-1]
    return name + ("이" if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 else "가")


def change_line(watch: Watch, old: dict, new: dict) -> str:
    dates = f" ({old.get('rev_date') or '?'} → {new.get('rev_date') or '?'})" if watch.kind == "rule" else ""
    return f"📘 {_subject(watch.name)} 바뀌었어요{dates} · <{watch.url}> · 지식베이스 다시 만들기 필요"


def _key(watch: Watch) -> dict[str, str]:
    return {"PK": f"RULEWATCH#{watch.id}", "SK": "STATE"}


class _Gate:
    """This run's requests, through the collectors' gate: per server one a minute (really waited out
    here), robots records first, www only from the day's spare. A server that says no for the day
    (cool-down, switched off, cap, no spare, no time left) gets nothing more this run."""

    def __init__(self, table, clock: Callable[[], datetime], sleep: Callable[[float], None],
                 time_left: Callable[[], float] | None, fetch=fetch_list_page):
        self.table, self.clock, self.sleep, self.time_left, self._fetch = table, clock, sleep, time_left, fetch
        self.last: dict[str, datetime] = {}
        self.stopped: dict[str, str] = {}

    def fetch(self, watch: Watch) -> tuple[str | None, str]:
        origin, cap = (RULE_HOST, RULE_DAILY_CAP) if watch.kind == "rule" else (NOTICE_HOST, WWW_DAILY_CAP)
        if not politeness.robots_allows(watch.url):
            politeness.robots_skip(f"rulewatch-{watch.id}", watch.url)
            return None, "robots"
        if origin in self.stopped:
            return None, self.stopped[origin]
        for _ in range(TRIES):
            wait = 0.0
            if origin in self.last:
                wait = max(0.0, (self.last[origin] + politeness.MIN_INTERVAL - self.clock()).total_seconds() + 1)
            if self.time_left is not None and self.time_left() < wait + WORST_CASE_S:
                self.stopped[origin] = "no_time"
                return None, "no_time"
            if wait:
                self.sleep(wait)
            if origin == NOTICE_HOST and extract.spare(self.table, _WWW, self.clock()) <= 0:
                self.stopped[origin] = "no_spare"
                return None, "no_spare"
            permit = politeness.acquire(self.table, origin, self.clock(), daily_cap=cap, by="rulewatch")
            if permit.granted:
                break
            if permit.reason not in ("too_soon", "raced"):
                self.stopped[origin] = permit.reason
                return None, permit.reason
            self.last[origin] = self.clock()  # another run has the server's minute: wait a whole one
        else:
            self.stopped[origin] = "too_soon"
            return None, "too_soon"
        self.last[origin] = self.clock()
        result = self._fetch(watch.url)
        if result.block_reason:
            if politeness.is_server_signal(result.block_reason):
                politeness.record_block(self.table, origin, self.clock(), result.block_reason, result.retry_after)
                metrics(Blocked=int(result.block_reason in politeness.HARD),
                        Transient=int(result.block_reason not in politeness.HARD))
                self.stopped[origin] = result.block_reason
            log("rulewatch_fetch_failed", watch=watch.id, reason=result.block_reason, status=result.status)
            return None, result.block_reason
        politeness.record_success(self.table, origin)
        return result.html, "ok"


def run(now: datetime, table, gate: _Gate) -> dict[str, object]:
    if not params.is_true(COLLECTOR_ENABLED_PARAM):
        log("rulewatch_skip", reason="disabled")
        return {"status": "disabled"}
    stored = {w.id: table.get_item(Key=_key(w), ConsistentRead=True).get("Item") or {} for w in WATCHES}
    # regulations first (their own server), then the www pages; each the longest unchecked first
    order = sorted(WATCHES, key=lambda w: (w.kind != "rule", int(stored[w.id].get("checked_at") or 0)))
    outcomes: Counter = Counter()
    lines = []
    for watch in order:
        html, reason = gate.fetch(watch)
        if html is None:
            outcomes[reason] += 1
            continue
        if watch.kind == "rule":
            revision = newest_revision(html)
            new = {"seq_history": revision[0], "rev_date": revision[1]} if revision else None
            same = new is not None and int(stored[watch.id].get("seq_history") or -1) == new["seq_history"]
        else:
            digest = page_hash(html)
            new = {"hash": digest} if digest else None
            same = new is not None and stored[watch.id].get("hash") == digest
        if new is None:  # a changed layout or an error page answered 200: a human should look
            log("rulewatch_unreadable", watch=watch.id)
            metrics(NeedsHuman=1)
            outcomes["unreadable"] += 1
            continue
        first = not stored[watch.id]
        values = {**new, "kind": watch.kind, "checked_at": int(gate.clock().timestamp())}
        if not first and not same:
            values["changed_at"] = values["checked_at"]
            lines.append(change_line(watch, stored[watch.id], new))
        table.update_item(Key=_key(watch), UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in values),
                          ExpressionAttributeNames={f"#{k}": k for k in values},
                          ExpressionAttributeValues={f":{k}": v for k, v in values.items()})
        outcomes["recorded" if first else "same" if same else "changed"] += 1
    if lines:
        today = now.astimezone(KST).date().isoformat()
        send_jobs(boto3.client("sqs"), [{"action": "ops", "channel": "ops", "content": "\n".join(lines),
                                         "id": f"rulewatch|{today}"}])
    log("rulewatch_done", **outcomes)
    metrics(RulewatchChecked=outcomes["recorded"] + outcomes["same"] + outcomes["changed"],
            RulewatchChanged=outcomes["changed"])
    return {"status": "ok", **outcomes}


def handler(event, context):
    table = boto3.resource("dynamodb").Table(PUBLIC_TABLE)
    time_left = (lambda: context.get_remaining_time_in_millis() / 1000) if context else None
    gate = _Gate(table, lambda: datetime.now(UTC), time.sleep, time_left)
    return run(datetime.now(UTC), table, gate)

"""The weekly regulation / 학사안내 change watcher: what counts as a change, the first run, the gate."""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta

import pytest

from kwcampus import dispatcher, politeness, rulewatch
from kwcampus.config import KST
from kwcampus.fetch import FetchResult
from kwcampus.kwnotice.source import NOTICE_HOST

MONDAY_9 = datetime(2026, 11, 9, 9, 0, 5, tzinfo=KST)


def law_view(*revisions: tuple[int, str]) -> str:
    """The shape of lawFullView.do: the 연혁 select, newest first, dates through showDate()."""
    options = "".join(
        f'<option value="{seq}"{" selected" if i == 0 else ""}><script>showDate(\'{day}\', \'2\');</script> 개정</option>'
        for i, (seq, day) in enumerate(revisions))
    return f'<html><body><select id="histroySeq" name="SEQ_HISTORY">{options}</select></body></html>'


def life_page(text: str) -> str:
    return (f'<html><body><div id="kw_content_w"><div class="contents-util">글자확대</div>'
            f'<div class="major-contents-box"><h3>안내</h3><p>{text}</p><script>var t = 1;</script></div>'
            f'</div></body></html>')


class Site:
    """Answers every watched URL; records the order and the time of each request."""

    def __init__(self, clock):
        self.clock = clock
        self.pages = {w.url: law_view((675, "20261028"), (664, "20260812")) if w.kind == "rule"
                      else life_page(f"{w.id} 본문") for w in rulewatch.WATCHES}
        self.asked: list[tuple[str, datetime]] = []

    def __call__(self, url, **kwargs):
        self.asked.append((url, self.clock()))
        page = self.pages[url]
        return page if isinstance(page, FetchResult) else FetchResult(page, None, 200)


@pytest.fixture
def watch(aws, monkeypatch):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Type="String", Value="true")
    queue = aws["sqs"].create_queue(QueueName="kw-discord")["QueueUrl"]
    monkeypatch.setattr(dispatcher, "DISCORD_QUEUE_URL", queue)
    now = [MONDAY_9]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)

    site = Site(clock)

    def run():
        gate = rulewatch._Gate(aws["public"], clock, sleep, None, fetch=site)
        return rulewatch.run(now[0], aws["public"], gate)

    def jobs():
        got = aws["sqs"].receive_message(QueueUrl=queue, MaxNumberOfMessages=10).get("Messages", [])
        return [json.loads(m["Body"]) for m in got]

    return {"run": run, "site": site, "jobs": jobs, "now": now, "public": aws["public"], "ssm": aws["ssm"]}


def test_parsers_on_the_page_shapes():
    assert rulewatch.newest_revision(law_view((614, "20260211"), (622, "20251217"), (389, "20250211"))) == \
        (622, "2025.12.17")  # the highest SEQ_HISTORY, wherever it sits
    assert rulewatch.newest_revision("<html><body>점검 중</body></html>") is None
    # hostile values: only ASCII digits, at most 9 of them, count as a revision
    hostile = law_view((675, "20261028"), ("9" * 10, "20991231"), ("1" * 400, "20991231"), ("１２３４", "20991231"),
                       ("-1", "20991231"), ("12e3", "20991231"), ("999999999", "20270101"))
    assert rulewatch.newest_revision(hostile) == (999999999, "2027.01.01")
    assert rulewatch.newest_revision(law_view(("9" * 10, "20991231"))) is None
    one = rulewatch.page_hash(life_page("졸업학점 133"))
    assert one and one == rulewatch.page_hash(life_page("졸업학점   133\n"))  # whitespace doesn't count
    assert one != rulewatch.page_hash(life_page("졸업학점 130"))
    assert rulewatch.page_hash(life_page("x").replace("var t = 1", "var t = 2")) == rulewatch.page_hash(life_page("x"))
    assert rulewatch.page_hash("<html><body><p>다른 레이아웃</p></body></html>") is None


def test_the_watch_list_is_what_the_rules_and_the_knowledge_base_cite():
    rules = {w.url.rsplit("=", 1)[1] for w in rulewatch.WATCHES if w.kind == "rule"}
    assert rules == {"21", "22", "95", "96", "216"}  # 학칙, 시행세칙, 다전공 내규 x2, 조기졸업 규정
    pages = {w.url.rsplit("/", 1)[1] for w in rulewatch.WATCHES if w.kind == "page"}
    assert {"bachelor_info01.jsp", "bachelor_info07.jsp", "bachelor_info11.jsp", "bachelor_info12.jsp"} <= pages
    assert {"summary.jsp", "scholarship01.jsp", "scholarship02.jsp", "bachelor_info08.jsp",
            "bachelor_info03.jsp"} <= pages  # 장학, 휴학(학적변동), 수업
    assert len({w.id for w in rulewatch.WATCHES}) == len(rulewatch.WATCHES)
    for w in rulewatch.WATCHES:  # hand-checked robots records, nothing disallowed
        assert w.url.split("/")[2] in politeness.ROBOTS and politeness.robots_allows(w.url), w.url


def test_the_first_run_only_records(watch):
    out = watch["run"]()
    assert out["status"] == "ok" and out["recorded"] == len(rulewatch.WATCHES) - out.get("no_spare", 0)
    assert watch["jobs"]() == []
    state = watch["public"].get_item(Key={"PK": "RULEWATCH#hakchik", "SK": "STATE"})["Item"]
    assert (int(state["seq_history"]), state["rev_date"], state["kind"]) == (675, "2026.10.28", "rule")
    page = watch["public"].get_item(Key={"PK": "RULEWATCH#info07", "SK": "STATE"})["Item"]
    assert len(page["hash"]) == 64 and "changed_at" not in page


def test_a_new_revision_and_a_changed_page_are_posted_to_ops(watch):
    watch["run"]()
    watch["now"][0] += timedelta(days=7)
    site = watch["site"]
    site.pages[rulewatch._RULE.format(21)] = law_view((690, "20261120"), (675, "20261028"))
    site.pages[rulewatch._LIFE.format("bachelor_info07")] = life_page("info07 본문 (2027학번 졸업학점 추가)")
    out = watch["run"]()
    assert out["changed"] == 2
    [job] = watch["jobs"]()
    assert job["action"] == "ops" and job["channel"] == "ops" and job["id"] == "rulewatch|2026-11-16"
    assert job["content"].splitlines() == [
        ("📘 학칙이 바뀌었어요 (2026.10.28 → 2026.11.20) · <https://rule.kw.ac.kr/lmxsrv/law/lawFullView.do?SEQ=21> "
         "· 지식베이스 다시 만들기 필요"),
        ("📘 수료·졸업요건 안내 페이지가 바뀌었어요 · <https://www.kw.ac.kr/ko/life/bachelor_info07.jsp> "
         "· 지식베이스 다시 만들기 필요"),
    ]
    state = watch["public"].get_item(Key={"PK": "RULEWATCH#hakchik", "SK": "STATE"})["Item"]
    assert int(state["seq_history"]) == 690 and int(state["changed_at"]) == int(state["checked_at"])

    watch["now"][0] += timedelta(days=7)
    out = watch["run"]()  # nothing new: nothing posted
    assert out.get("changed", 0) == 0 and watch["jobs"]() == []


def test_an_unreadable_page_is_left_as_it_was_and_needs_a_human(watch, capsys):
    watch["run"]()
    before = watch["public"].get_item(Key={"PK": "RULEWATCH#sihaeng", "SK": "STATE"})["Item"]
    watch["now"][0] += timedelta(days=7)
    watch["site"].pages[rulewatch._RULE.format(22)] = "<html><body>시스템 점검 중</body></html>"
    watch["site"].pages[rulewatch._LIFE.format("summary")] = FetchResult(None, "http_failure", 404)
    out = watch["run"]()
    assert out["unreadable"] == 1 and out["http_failure"] == 1 and watch["jobs"]() == []
    assert watch["public"].get_item(Key={"PK": "RULEWATCH#sihaeng", "SK": "STATE"})["Item"] == before
    logs = capsys.readouterr().out
    assert '"event": "rulewatch_unreadable", "watch": "sihaeng"' in logs and '"NeedsHuman": 1' in logs


def test_requests_go_through_the_gate_a_minute_apart_regulations_first(watch):
    watch["run"]()
    asked = watch["site"].asked
    kinds = ["rule" if "rule.kw.ac.kr" in url else "page" for url, _ in asked]
    assert kinds == sorted(kinds, key=lambda k: k != "rule")  # all regulations before any www page
    for host in ("rule.kw.ac.kr", "www.kw.ac.kr"):
        times = [at for url, at in asked if host in url]
        assert all(b - a >= politeness.MIN_INTERVAL for a, b in itertools.pairwise(times)), host
    www = politeness.state(watch["public"], NOTICE_HOST)
    rule = politeness.state(watch["public"], rulewatch.RULE_HOST)
    assert int(rule["day_count"]) == 5 and rule["last_by"] == "rulewatch"
    assert int(www["day_count"]) == len([k for k in kinds if k == "page"])


def test_www_takes_only_the_days_spare_and_the_rest_waits_for_next_week(watch):
    out = watch["run"]()
    pages = [w for w in rulewatch.WATCHES if w.kind == "page"]
    assert out.get("no_spare", 0) >= 1 and out["recorded"] + out["no_spare"] == len(rulewatch.WATCHES)
    asked = {url for url, _ in watch["site"].asked}
    left = [w for w in pages if w.url not in asked]
    assert left and len(left) == out["no_spare"]
    # next week the pages left over go first
    watch["now"][0] += timedelta(days=7)
    watch["site"].asked.clear()
    watch["run"]()
    first_www = next(url for url, _ in watch["site"].asked if "www.kw.ac.kr" in url)
    assert first_www == left[0].url


def test_a_server_in_cool_down_or_switched_off_gets_nothing(watch):
    watch["public"].put_item(Item={"PK": f"ORIGIN#{rulewatch.RULE_HOST}", "SK": "STATE",
                                   "cooldown_until": int((MONDAY_9 + timedelta(hours=1)).timestamp())})
    watch["public"].put_item(Item={"PK": f"ORIGIN#{NOTICE_HOST}", "SK": "STATE", "disabled": True})
    out = watch["run"]()
    rules = len([w for w in rulewatch.WATCHES if w.kind == "rule"])
    assert watch["site"].asked == [] and "recorded" not in out
    assert out["cooldown"] == rules and out["disabled_after_blocks"] == len(rulewatch.WATCHES) - rules


def test_a_block_cools_the_server_down(watch):
    watch["site"].pages[rulewatch._RULE.format(21)] = FetchResult(None, "forbidden", 403)
    out = watch["run"]()
    rule = politeness.state(watch["public"], rulewatch.RULE_HOST)
    assert int(rule["blocks"]) == 1 and int(rule["cooldown_until"]) > MONDAY_9.timestamp()
    assert len([u for u, _ in watch["site"].asked if "rule.kw.ac.kr" in u]) == 1  # nothing more there this run
    assert out["forbidden"] == 5  # the blocked page, and the four regulations left for next week
    assert out["recorded"] >= 1  # www, another server, goes on


def test_another_run_holding_the_minute_is_waited_out(watch, monkeypatch):
    real = politeness.acquire
    refused = []

    def busy_once(table, origin, now, daily_cap, by=None, interval=None):
        if origin == NOTICE_HOST and not refused:
            refused.append(now)
            return politeness.Permit(False, "too_soon")  # the 09:00 list run just took www
        return real(table, origin, now, daily_cap, by=by, interval=interval)

    monkeypatch.setattr(politeness, "acquire", busy_once)
    watch["run"]()
    first_www = next(at for url, at in watch["site"].asked if "www.kw.ac.kr" in url)
    assert refused and first_www - refused[0] >= politeness.MIN_INTERVAL


def test_the_kill_switch_stops_it(watch):
    watch["ssm"].put_parameter(Name="/kw/collector/enabled", Type="String", Value="false", Overwrite=True)
    from kwcampus import params

    params.clear_cache()
    assert watch["run"]() == {"status": "disabled"} and watch["site"].asked == []


def test_no_time_left_stops_before_a_request(watch, aws):
    gate = rulewatch._Gate(aws["public"], lambda: MONDAY_9, lambda s: None, lambda: 10.0, fetch=watch["site"])
    out = rulewatch.run(MONDAY_9, aws["public"], gate)
    assert watch["site"].asked == [] and out["no_time"] == len(rulewatch.WATCHES)

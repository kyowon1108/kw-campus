"""The request gate: hard blocks vs transient failures, Retry-After, re-enabling, and fetch's own limits."""

import http.client
import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from kwcampus import fetch, politeness
from kwcampus.config import USER_AGENT

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
ORIGIN = "61.111.8.4"


def metric_names(capsys) -> list[str]:
    out = []
    for line in capsys.readouterr().out.splitlines():
        doc = json.loads(line)
        if "_aws" in doc:
            out += [m["Name"] for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"] if doc[m["Name"]]]
    return out


def test_transient_failures_back_off_but_never_disable(aws, capsys):
    table, at, waits = aws["public"], NOW, []
    for _ in range(7):
        state = politeness.record_block(table, ORIGIN, at, "transport_failure")
        waits.append(state["cooldown_minutes"])
        assert not state["disabled"]
        at += timedelta(hours=7)
    assert waits == [0, 30, 60, 120, 240, 360, 360]  # first one waits for the next run only; 6 h at most
    assert politeness.acquire(table, ORIGIN, at, daily_cap=32).granted
    assert "NeedsHuman" in metric_names(capsys)  # six in a row: someone should look


def test_hard_blocks_escalate_and_three_in_a_row_disable(aws, capsys):
    table = aws["public"]
    assert politeness.record_block(table, ORIGIN, NOW, "forbidden")["cooldown_minutes"] == 30
    assert politeness.record_block(table, ORIGIN, NOW + timedelta(hours=1), "server_error")["blocks"] == 1  # not hard
    assert politeness.record_block(table, ORIGIN, NOW + timedelta(hours=2), "rate_limit")["cooldown_minutes"] == 60
    assert politeness.record_block(table, ORIGIN, NOW + timedelta(hours=4), "captcha")["disabled"] is True
    capsys.readouterr()
    for hours in (30, 31):  # every skipped run says so, so the alarm stays on until a human acts
        assert politeness.acquire(table, ORIGIN, NOW + timedelta(hours=hours), 32).reason == "disabled_after_blocks"
    assert metric_names(capsys) == ["OriginDisabled", "OriginDisabled"]


def test_reenable_resets_everything_so_one_block_does_not_disable_again(aws):
    table = aws["public"]
    for n in range(3):
        politeness.record_block(table, ORIGIN, NOW + timedelta(hours=n), "forbidden")
    politeness.reenable(table, ORIGIN)
    assert politeness.acquire(table, ORIGIN, NOW + timedelta(hours=4), 32).granted
    assert politeness.record_block(table, ORIGIN, NOW + timedelta(hours=5), "forbidden") == {
        "blocks": 1, "soft": 0, "cooldown_minutes": 30, "disabled": False}


def test_retry_after_is_respected(aws):
    table = aws["public"]
    state = politeness.record_block(table, ORIGIN, NOW, "rate_limit", retry_after=7200)
    assert state["cooldown_minutes"] == 120
    assert politeness.acquire(table, ORIGIN, NOW + timedelta(minutes=90), 32).reason == "cooldown"
    assert politeness.record_block(table, "x", NOW, "server_error", retry_after=30 * 86400)["cooldown_minutes"] == 7 * 24 * 60


def test_a_block_between_read_and_update_still_counts(aws):
    """Two overlapping runs: the other run's block lands after this run read the gate."""
    table = aws["public"]
    real_get = table.get_item

    class Racing:
        def get_item(self, **kw):
            item = real_get(**kw)
            politeness.record_block(table, ORIGIN, NOW, "rate_limit")  # lands right after our read
            return item

        def __getattr__(self, name):
            return getattr(table, name)

    assert politeness.acquire(Racing(), ORIGIN, NOW, 32).reason == "raced"


def test_last_by_records_who_asked(aws):
    table = aws["public"]
    politeness.acquire(table, ORIGIN, NOW, 32, by="aicon")
    assert politeness.state(table, ORIGIN)["last_by"] == "aicon"


def test_source_back_off_and_switch_off(aws, capsys):
    table, key = aws["public"], {"PK": "SRC#kw-job", "SK": "STATE"}

    def state():
        return table.get_item(Key=key)["Item"]

    assert politeness.source_failed(table, key, {}, NOW, "http_failure", timedelta(hours=2))["wait_minutes"] == 120
    assert politeness.source_waiting(state(), NOW + timedelta(hours=1))
    assert not politeness.source_waiting(state(), NOW + timedelta(hours=2, seconds=1))
    assert politeness.source_failed(table, key, state(), NOW, "redirect", timedelta(hours=2))["wait_minutes"] == 240
    politeness.source_ok(table, key, state())
    assert "fails" not in state()
    for _ in range(3):
        out = politeness.source_failed(table, key, state(), NOW, "forbidden", timedelta(hours=2))
    assert out["source_disabled"] is True and politeness.source_waiting(state(), NOW + timedelta(days=30))


# --- fetch -------------------------------------------------------------------------------------

class _Response:
    def __init__(self, body: bytes, status=200, ctype="text/html", chunk=None, clock=None):
        self.body, self.status, self.headers = body, status, {"Content-Type": ctype}
        self.chunk, self.clock = chunk or len(body) or 1, clock

    def read1(self, limit):
        part, self.body = self.body[: self.chunk], self.body[self.chunk:]
        if self.clock:
            self.clock[0] += 20  # a server trickling bytes: 20 s per chunk
        return part

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(behaviour):
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("User-agent") == USER_AGENT
            return behaviour()
    return Opener()


def _http_error(code, headers=None, body=b""):
    return urllib.error.HTTPError("https://x", code, "x", headers or {}, io.BytesIO(body))


def test_fetch_classifies_failures_and_reads_retry_after(monkeypatch):
    cases = {
        403: "forbidden", 404: "http_failure", 410: "http_failure", 302: "redirect", 500: "server_error",
    }
    for code, reason in cases.items():
        def fail(code=code):
            raise _http_error(code)
        monkeypatch.setattr(fetch, "_opener", _opener(fail))
        assert fetch.fetch_list_page("https://ee.kw.ac.kr/").block_reason == reason

    def limited():
        raise _http_error(429, {"Retry-After": "120"})
    monkeypatch.setattr(fetch, "_opener", _opener(limited))
    assert fetch.fetch_list_page("https://ee.kw.ac.kr/") == fetch.FetchResult(None, "rate_limit", 429, 120)

    def unavailable():
        raise _http_error(503, {"Retry-After": "Thu, 05 Nov 2026 04:00:00 GMT"})
    monkeypatch.setattr(fetch, "_opener", _opener(unavailable))
    monkeypatch.setattr(fetch, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    assert fetch.fetch_list_page("https://ee.kw.ac.kr/").retry_after == 3600

    def challenge():
        raise _http_error(503, body=b"<html><head><title>Just a moment...</title></head></html>")
    monkeypatch.setattr(fetch, "_opener", _opener(challenge))
    assert fetch.fetch_list_page("https://ee.kw.ac.kr/").block_reason == "captcha"

    def broken():
        raise http.client.RemoteDisconnected("closed")
    monkeypatch.setattr(fetch, "_opener", _opener(broken))
    assert fetch.fetch_list_page("https://ee.kw.ac.kr/").block_reason == "transport_failure"


def test_fetch_has_an_overall_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(fetch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fetch, "_opener", _opener(lambda: _Response(b"x" * 10, chunk=1, clock=clock)))
    assert fetch.fetch_list_page("https://ee.kw.ac.kr/").block_reason == "slow"
    clock[0] = 0.0
    monkeypatch.setattr(fetch, "_opener", _opener(lambda: _Response("<html>목록</html>".encode(), chunk=4)))
    assert fetch.fetch_list_page("https://ee.kw.ac.kr/").html == "<html>목록</html>"


@pytest.mark.parametrize(("body", "challenge"), [
    (b"<html><head><title>Just a moment...</title></head><body>cf-chl-opt</body></html>", True),
    (b"<html><head><title>Attention Required! | Cloudflare</title></head></html>", True),
    (b"<html><body><div class='g-recaptcha'></div></body></html>", True),
    (b"<html><head><script src='https://static.cloudflareinsights.com/beacon.min.js'></script></head></html>", False),
    (b"<html><head><link href='https://cdnjs.cloudflare.com/x.css'></head><body>akamai access denied</body></html>", False),
])
def test_challenge_signatures(body, challenge):
    assert fetch.is_challenge(body) is challenge


def test_user_agent_is_honest_and_has_no_e_mail():
    assert USER_AGENT.startswith("kw-campus/") and "https://github.com/kyowon1108/kw-campus" in USER_AGENT
    assert "/privacy" in USER_AGENT and "@" not in USER_AGENT and "kw-notice-mcp" not in USER_AGENT

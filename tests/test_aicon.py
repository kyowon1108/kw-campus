"""인공지능융합대학 notices from the college home page's widget."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kwcampus import aicon, collector, deptboards, dispatcher, public_data, wwwboards
from kwcampus.discord import handle_job
from kwcampus.fetch import FetchResult

# Saved from https://aicon.kw.ac.kr/main/main.php on 2026-11-05 (public page, no personal data).
WIDGET = (Path(__file__).parent / "fixtures" / "aicon_main.html").read_text()
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)


def widget_fetch(url):
    assert url == aicon.WIDGET_URL
    return FetchResult(WIDGET, None, 200)


def test_parse_widget_reads_school_title_date_and_link():
    rows = aicon.parse_widget(WIDGET)
    assert [r.duid for r in rows] == ["3516", "3515", "3514", "3513", "3512"]  # graduate/Q&A tabs left out
    first = rows[0]
    assert first.department == "컴퓨터정보공학부" and first.title.startswith("[공지] 2026년 졸업작품")
    assert first.posted_date.isoformat() == "2026-11-05" and first.category_id == "dept-aicon"
    assert first.source_url == "https://aicon.kw.ac.kr/administration/notice.php?admin_mode=read&no=3516"
    assert {r.department for r in rows} == {"컴퓨터정보공학부", "정보융합학부", "소프트웨어학부"}


def test_collect_seeds_quietly_then_inserts_new_posts(aws):
    table = aws["public"]
    first = aicon.collect(NOW, table, fetch=widget_fetch)
    assert first == {"status": "ok", "rows": 5, "seed_run": True, "inserted": 5, "updated": 0, "unchanged": 0}
    items = [i for i in table.scan()["Items"] if i["PK"] == "SRC#aicon" and i["SK"].startswith("N#")]
    assert len(items) == 5 and all(i["seed"] for i in items)  # baseline: no alerts for what was already there

    newer = WIDGET.replace("no=3516", "no=3517").replace("[공지] 2026년 졸업작품", "[공지] 새 공지")
    second = aicon.collect(NOW + timedelta(minutes=20), table, fetch=lambda url: FetchResult(newer, None, 200))
    assert second["inserted"] == 1 and second["seed_run"] is False
    new = table.get_item(Key={"PK": "SRC#aicon", "SK": "N#3517"})["Item"]
    assert new["seed"] is False and new["department"] == "컴퓨터정보공학부"


def test_collect_respects_the_hosts_gate(aws):
    table = aws["public"]
    aicon.collect(NOW, table, fetch=widget_fetch)
    calls = []
    out = aicon.collect(NOW + timedelta(seconds=30), table, fetch=lambda url: calls.append(url))
    assert out == {"status": "skipped", "reason": "too_soon"} and calls == []


def test_department_notices_show_in_search_with_distinct_ids(aws):
    table = aws["public"]
    aicon.collect(NOW, table, fetch=widget_fetch)
    public_data._CACHE.clear()
    hits = public_data.search(table, "정보융합학부", None, None, 20)
    assert hits and all(h["post_id"].startswith("aicon-") for h in hits)
    assert {h["category"] for h in hits} == {"인공지능융합대학"}
    assert public_data.latest(table, "인융대", 10) and public_data.get(table, hits[0]["post_id"])


def test_department_alerts_fall_back_to_the_notice_channel():
    record = {"eventName": "INSERT", "dynamodb": {"NewImage": {
        "PK": {"S": "SRC#aicon"}, "SK": {"S": "N#3517"}, "category_id": {"S": "dept-aicon"},
        "seed": {"BOOL": False}, "pinned": {"BOOL": False}, "posted_date": {"S": "2026-11-05"}}}}
    jobs = dispatcher.jobs_for([record], NOW.date())
    assert jobs and jobs[0]["channel"] == "department"

    class Public:
        def get_item(self, Key):
            return {}

    # with no "department" channel configured, the sender uses "notice" (and here finds nothing to post)
    assert handle_job(jobs[0], Public(), None, "t", {"notice": "123"}) == "gone"
    assert handle_job(jobs[0], Public(), None, "t", {}) == "no_channel"


def test_scheduled_run_survives_a_department_site_failure(aws, monkeypatch):
    aws["ssm"].put_parameter(Name="/kw/collector/enabled", Value="true", Type="String")
    monkeypatch.setattr(collector, "run", lambda now, table: {"status": "ok"})

    def boom(now, table):
        raise RuntimeError("aicon down")

    monkeypatch.setattr(aicon, "collect", boom)
    monkeypatch.setattr(deptboards, "collect", lambda now, table: {"status": "ok"})
    monkeypatch.setattr(wwwboards, "collect", lambda now, table: {"status": "not_due"})
    monkeypatch.setattr(collector.boto3, "resource", lambda name: type("R", (), {"Table": lambda self, n: aws["public"]})())
    assert collector.handler({}, None) == {"status": "ok", "aicon": {"status": "error"}, "dept": {"status": "ok"},
                                           "www": {"status": "not_due"}}

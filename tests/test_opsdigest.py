"""The morning #운영 digest: counts only, one line when all is well, and the sender posts it to #운영 alone."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import boto3
import pytest

from kwcampus import discord, dispatcher, invites, opsdigest, public_data
from kwcampus.config import KST

TUESDAY = datetime(2026, 11, 10, 8, 40, tzinfo=KST)  # covers Monday 11/9
MONDAY = datetime(2026, 11, 9, 8, 40, tzinfo=KST)  # covers Friday 11/6 to Sunday 11/8
MEMBER = "123456789012345678"


class FakeCloudWatch:
    def __init__(self, sums: dict[str, int] | None = None, alarms: list[str] = (), fail: bool = False):
        self.sums, self.alarm_names, self.fail = sums or {}, list(alarms), fail
        self.asked: list[dict] = []

    def get_metric_data(self, **kwargs):
        if self.fail:
            raise RuntimeError("cloudwatch down")
        self.asked.append(kwargs)
        return {"MetricDataResults": [
            {"Id": q["Id"], "Values": [self.sums[q["MetricStat"]["Metric"]["MetricName"]]]
             if q["MetricStat"]["Metric"]["MetricName"] in self.sums else []}
            for q in kwargs["MetricDataQueries"]]}

    def describe_alarms(self, **kwargs):
        if self.fail:
            raise RuntimeError("cloudwatch down")
        assert kwargs["AlarmNamePrefix"] == "kw-" and kwargs["StateValue"] == "ALARM"
        return {"MetricAlarms": [{"AlarmName": f"kw-{n}"} for n in self.alarm_names]}


@pytest.fixture
def ops(aws, monkeypatch):
    sqs = aws["sqs"]
    queue = sqs.create_queue(QueueName="kw-discord")["QueueUrl"]
    dlq = sqs.create_queue(QueueName="kw-discord-dlq")["QueueUrl"]
    monkeypatch.setattr(dispatcher, "DISCORD_QUEUE_URL", queue)
    monkeypatch.setattr(opsdigest, "DISCORD_QUEUE_URL", queue)
    monkeypatch.setenv("KW_DISCORD_DLQ_URL", dlq)
    bridge = boto3.resource("dynamodb").create_table(
        TableName="kw-bridge",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    return {"public": aws["public"], "bridge": bridge, "sqs": sqs, "queue": queue, "dlq": dlq}


def notice(public, source: str, post: str, first_seen: datetime, seed: bool = False, title: str = "공지") -> None:
    public.put_item(Item={"PK": f"SRC#{source}", "SK": f"N#{post}", "title": title, "seed": seed,
                          "first_seen": int(first_seen.timestamp())})


def healthy(public, now: datetime) -> None:
    """Every watched board has something new from the day before."""
    for source in public_data.SOURCES:
        notice(public, source, "1", now - timedelta(hours=20))


def posted(ops) -> list[dict]:
    messages = ops["sqs"].receive_message(QueueUrl=ops["queue"], MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(m["Body"]) for m in messages]


def test_all_well_is_two_short_lines(ops):
    healthy(ops["public"], TUESDAY)
    notice(ops["public"], "kw-notice", "2", TUESDAY - timedelta(days=3), seed=False)  # before the period
    notice(ops["public"], "kw-notice", "3", TUESDAY - timedelta(hours=20), seed=True)  # a seed row
    ops["bridge"].put_item(Item={"PK": "DINVITES#2026-11-09", "SK": "META", "n": 3})
    ops["bridge"].put_item(Item={"PK": "DINVITES#2026-11-10", "SK": "META", "n": 1})  # this morning: tomorrow's
    cw = FakeCloudWatch({"ExtractOk": 9, "ExtractSkippedCap": 2})
    out = opsdigest.run(TUESDAY, ops["public"], ops["bridge"], cw, ops["sqs"])
    assert out == {"status": "ok", "fine": True, "unread": 0}
    [job] = posted(ops)
    watched = len([s for s in public_data.SOURCES if s not in opsdigest.NOT_WATCHED])
    assert job == {"action": "ops", "channel": "ops", "id": "digest|2026-11-10",
                   "content": f"✅ 어제 이상 없음 · 새 공지 {watched} · 초대 3\n"
                              f"읽기: 새 {watched} · 완료 0 · 일부 0 · 대기 {watched} · 제목만 0 · 큐 0(가장 오래 0시간) · "
                              "못 읽고 만료 0 · 다시 읽기 남음 미집계"}
    [asked] = cw.asked
    assert asked["StartTime"] == datetime(2026, 11, 9, tzinfo=KST) and asked["EndTime"] == datetime(2026, 11, 10,
                                                                                                    tzinfo=KST)
    assert {q["MetricStat"]["Period"] for q in asked["MetricDataQueries"]} == {86400}


def test_the_notice_query_asks_for_the_granted_attributes_only(ops):
    """infra grants this Query dynamodb:Attributes = PK, SK, first_seen, seed, removed (test_stack) and
    Select = SPECIFIC_ATTRIBUTES."""
    notice(ops["public"], "kw-notice", "1", TUESDAY - timedelta(hours=3), title="제목은 읽지 않음")
    asked = []
    real = ops["public"].query

    class Spy:
        def query(self, **kwargs):
            asked.append(kwargs)
            return real(**kwargs)

    newest, fresh, read = opsdigest._seen(Spy(), "kw-notice", 0, 2**40)
    assert (newest, fresh, dict(read)) == (int((TUESDAY - timedelta(hours=3)).timestamp()), 1, {"pending": 1})
    [kwargs] = asked
    assert kwargs["Select"] == "SPECIFIC_ATTRIBUTES"
    names = kwargs["ExpressionAttributeNames"]
    assert sorted(names[n.strip()] for n in kwargs["ProjectionExpression"].split(",")) == sorted(
        opsdigest.QUERY_ATTRIBUTES) == ["extract_v", "first_seen", "read", "removed", "seed"]
    assert "title" not in json.dumps(real(**kwargs)["Items"], default=str)


def test_monday_covers_friday_to_sunday(ops):
    healthy(ops["public"], MONDAY)
    for day, n in (("2026-11-06", 2), ("2026-11-07", 1), ("2026-11-08", 4), ("2026-11-05", 50)):
        ops["bridge"].put_item(Item={"PK": f"DINVITES#{day}", "SK": "META", "n": n})
    cw = FakeCloudWatch()
    opsdigest.run(MONDAY, ops["public"], ops["bridge"], cw, ops["sqs"])
    [job] = posted(ops)
    first = job["content"].splitlines()[0]
    assert first.startswith("✅ 금~일 이상 없음 · ") and first.endswith("· 초대 7")
    assert {q["MetricStat"]["Period"] for q in cw.asked[0]["MetricDataQueries"]} == {3 * 86400}


def test_problems_get_a_short_report_with_counts_and_names_only(ops):
    public, now = ops["public"], TUESDAY
    healthy(public, now)
    notice(public, "kw-notice", "9", now - timedelta(hours=10), title="홍길동 장학생 선발 명단")
    public.put_item(Item={"PK": "SRC#dept-ee", "SK": "STATE", "disabled": True})
    public.put_item(Item={"PK": "SRC#dept-law", "SK": "STATE", "not_before": int((now + timedelta(hours=2)).timestamp())})
    public.put_item(Item={"PK": "ORIGIN#61.111.8.4", "SK": "STATE",
                          "cooldown_until": int((now + timedelta(minutes=30)).timestamp())})
    public.delete_item(Key={"PK": "SRC#dept-kwgame", "SK": "N#1"})
    notice(public, "dept-kwgame", "1", now - timedelta(days=40))  # quiet for 40 days
    ops["sqs"].send_message(QueueUrl=ops["dlq"], MessageBody="{}")
    invites.issue(ops["bridge"], MEMBER, "https://bridge.example.com", now=int((now - timedelta(days=1)).timestamp()))
    cw = FakeCloudWatch({"ExtractOk": 5, "ExtractFail": 2, "ExtractRefused": 1, "ExtractSkippedCap": 0,
                         "DiscordRetries": 4}, alarms=["discord-dlq", "collector-needs-human"])
    out = opsdigest.run(now, public, ops["bridge"], cw, ops["sqs"])
    assert out["fine"] is False
    [job] = posted(ops)
    text = job["content"]
    # one new notice per watched board, kwgame's is 40 days old, the main board has one more
    new = len([s for s in public_data.SOURCES if s not in opsdigest.NOT_WATCHED]) - 1 + 1
    assert text.splitlines() == [
        "⚠️ 어제 확인할 게 있어요",
        f"- 게시판: 꺼짐 1 (dept-ee) · 잠시 멈춤 2 (61.111.8.4, dept-law) · 조용한 게시판 1 · 새 공지 {new}",
        "- 공지 읽기: 완료 5 · 실패 3 · 한도로 미룸 0",
        (f"- 읽기 상태: 새 {new} · 완료 0 · 일부 0 · 대기 {new} · 제목만 0 · 큐 0(가장 오래 0시간) · 못 읽고 만료 0 · "
         "다시 읽기 남음 미집계"),
        "- 디스코드: 대기 0 · 실패 1 · 재시도 4",
        "- 초대: 1 (전체 1)",
        "- 알람: collector-needs-human, discord-dlq",
    ]
    assert "홍길동" not in text and MEMBER not in text and len(text) < 2000


def test_a_part_that_cannot_be_read_says_so_and_the_rest_still_goes_out(ops, capsys):
    healthy(ops["public"], TUESDAY)
    out = opsdigest.run(TUESDAY, ops["public"], ops["bridge"], FakeCloudWatch(fail=True), ops["sqs"])
    assert out == {"status": "ok", "fine": False, "unread": 2}
    [job] = posted(ops)
    lines = job["content"].splitlines()
    assert "- 공지 읽기: 확인 못 함" in lines and "- 알람: 확인 못 함" in lines
    assert lines[1].startswith("- 게시판: 꺼짐 0 · 잠시 멈춤 0")
    assert '"event": "ops_digest_part_failed", "part": "metrics", "error": "RuntimeError"' in capsys.readouterr().out


def test_a_backlog_is_worth_a_look(ops):
    healthy(ops["public"], TUESDAY)
    for i in range(opsdigest.BACKLOG_WARN):
        ops["sqs"].send_message(QueueUrl=ops["queue"], MessageBody=json.dumps({"n": i}))
    text, fine = opsdigest.compose("어제", {"off": [], "paused": [], "stale": 0, "new": 1},
                                   dict.fromkeys(opsdigest.EXTRACT_METRICS, 0),
                                   opsdigest.queues(ops["sqs"]), {"period": 0, "total": 0}, [])
    assert not fine and f"- 디스코드: 대기 {opsdigest.BACKLOG_WARN} · 실패 0 · 재시도 0" in text


def test_long_name_lists_are_cut():
    text, _ = opsdigest.compose("어제", {"off": [f"dept-{i}" for i in range(8)], "paused": [], "stale": 0, "new": 0},
                                dict.fromkeys(opsdigest.EXTRACT_METRICS, 0), {"waiting": 0, "dead": 0},
                                {"period": 0, "total": 0}, [])
    assert "꺼짐 8 (dept-0, dept-1, dept-2, dept-3, dept-4 외 3)" in text


def test_every_collector_server_is_watched():
    from kwcampus import deptboards, wixfeeds

    assert set(opsdigest.origins()) >= {"www.kw.ac.kr", deptboards.SERVER_E1, deptboards.SERVER_E2,
                                        deptboards.SERVER_E3, deptboards.SERVER_E4,
                                        *(f.host for f in wixfeeds.FEEDS)}
    assert {b.server for b in deptboards.BOARDS} <= set(opsdigest.origins())


# --- the sender's side ---------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, token, body=None, timeout=10):
        self.calls.append((method, path, body))
        return {"id": "1"}


def test_the_sender_posts_an_ops_job_to_the_ops_channel_only_and_pings_nobody(aws):
    send = Recorder()
    channels = {"notice": "201", "ops": "501"}
    job = {"action": "ops", "channel": "notice", "content": "@everyone ✅ 어제 이상 없음", "id": "digest|2026-11-10"}
    outcome = discord.handle_job(job, aws["public"], aws["delivery"], "token", channels, send=send)
    assert outcome == "ops_posted"
    [(method, path, body)] = send.calls
    assert (method, path) == ("POST", "/channels/501/messages")  # whatever channel the job names
    assert body["allowed_mentions"] == {"parse": []} and body["flags"] == discord.SUPPRESS_EMBEDS
    assert body["enforce_nonce"] is True and body["nonce"] == discord._nonce("ops", "digest|2026-11-10")
    assert aws["delivery"].scan()["Items"] == []  # no delivery record for an ops message

    assert discord.handle_job(job, aws["public"], aws["delivery"], "token", {"notice": "201"}, send=send) == \
        "no_channel"
    assert discord.handle_job({**job, "content": "  "}, aws["public"], aws["delivery"], "token", channels,
                              send=send) == "empty"
    assert len(send.calls) == 1


def test_quiet_boards_alone_are_not_a_warning():
    from kwcampus import opsdigest

    text, fine = opsdigest.compose("어제", {"off": [], "paused": [], "stale": 29, "new": 13},
                                   {"ExtractOk": 0, "ExtractFail": 0, "ExtractRefused": 0, "ExtractSkippedCap": 0,
                                    "DiscordRetries": 0},
                                   {"waiting": 0, "dead": 0}, {"period": 0, "total": 7}, [])
    assert fine and text.startswith("✅ 어제 이상 없음")


def test_a_freshly_seeded_board_is_not_taken_for_a_live_one(ops):
    """A board's first read stores its old posts as seed rows: they say nothing about the board posting now."""
    notice(ops["public"], "council", "cl1", TUESDAY - timedelta(hours=20), seed=True)
    assert opsdigest._seen(ops["public"], "council", 0, 2**40)[:2] == (None, 0)
    notice(ops["public"], "council", "cl2", TUESDAY - timedelta(hours=10))
    assert opsdigest._seen(ops["public"], "council", 0, 2**40)[:2] == (int((TUESDAY - timedelta(hours=10)).timestamp()), 1)



def test_reading_health_counts_new_notices_by_how_they_were_read(ops, monkeypatch):
    """Done, partial, pending (no record yet, or a retry planned), title only; the queue and its oldest row; a
    notice whose queue row ran out unread makes the day worth a look."""
    public, now = ops["public"], TUESDAY
    public.put_item(Item={"PK": "MARK#read-records", "SK": "SINCE",  # read records began a month ago
                          "at": int((TUESDAY - timedelta(days=30)).timestamp())})
    healthy(public, now)
    day = now - timedelta(hours=20)

    def read(post: str, record: dict | None, at=day, source="kw-notice"):
        public.put_item(Item={"PK": f"SRC#{source}", "SK": f"N#{post}", "seed": False, "first_seen": int(at.timestamp()),
                              **({"read": record} if record else {})})

    read("11", {"status": "complete"})
    read("12", {"status": "body_only"})
    read("13", {"status": "partial"})
    read("14", {"status": "partial", "retry_at": int((now + timedelta(hours=1)).timestamp())})  # still to come
    read("15", {"status": "title_only"}, source="internship")
    read("16", {"status": "privacy_skip"})
    read("17", {"status": "failed"})
    read("18", None, at=now - timedelta(days=15))  # its queue row ran out yesterday: never read
    read("19", {"status": "complete"}, at=now - timedelta(days=15))  # read in time: fine
    public.put_item(Item={"PK": "BODYQ", "SK": "SRC#kw-notice|N#14", "queued_at": int((now - timedelta(hours=5)).timestamp()),
                          "text": "never read by the digest"})
    out = opsdigest.run(now, public, ops["bridge"], FakeCloudWatch(), ops["sqs"])
    assert out["fine"] is False
    [job] = posted(ops)
    watched = len([s for s in public_data.SOURCES if s not in opsdigest.NOT_WATCHED])
    line = next(x for x in job["content"].splitlines() if x.startswith("- 읽기 상태"))
    assert line == (f"- 읽기 상태: 새 {watched + 7} · 완료 3 · 일부 2 · 대기 {watched + 1} · 제목만 1 · "
                    "큐 1(가장 오래 5시간) · 못 읽고 만료 1 · 다시 읽기 남음 미집계")
    assert opsdigest.queue_health(public, now) == {"queue": 1, "oldest_h": 5}



def test_notices_from_before_read_records_are_neither_pending_nor_expired(ops):
    """A notice read before read records existed (extract_v, no `read`) is done; one first seen before them and
    never read (일반, the FAQ and the Wix feeds weren't queued then) never counts as expired."""
    public, now = ops["public"], TUESDAY
    healthy(public, now)
    public.put_item(Item={"PK": "SRC#kw-notice", "SK": "N#21", "seed": False, "extract_v": 1,
                          "first_seen": int((now - timedelta(hours=20)).timestamp())})
    public.put_item(Item={"PK": "SRC#kw-notice", "SK": "N#22", "seed": False,  # 일반, 2026-10-26: never queued
                          "first_seen": int((now - timedelta(days=15)).timestamp())})
    since = int(datetime(2026, 11, 8, 15, tzinfo=KST).timestamp())  # the deploy: the first read record written
    public.put_item(Item={"PK": "MARK#read-records", "SK": "SINCE", "at": since})
    _, _, read = opsdigest._seen(public, "kw-notice", int((now - timedelta(days=1)).timestamp()), int(now.timestamp()),
                                 since)
    assert read["done"] == 1 and read["expired"] == 0
    assert opsdigest.run(now, public, ops["bridge"], FakeCloudWatch(), ops["sqs"])["fine"] is True


def test_expired_counts_only_after_read_records_began_and_never_without_them(ops):
    """The cutoff is the first read record ever written (extract.mark_reads), not a date in the code: before
    any, nothing counts as expired; after, only notices first seen since then that were never read."""
    from kwcampus import extract

    public, now = ops["public"], datetime(2026, 11, 30, 8, 40, tzinfo=KST)
    healthy(public, now)
    seen = int((now - timedelta(days=15)).timestamp())  # its queue row ran out yesterday
    public.put_item(Item={"PK": "SRC#kw-notice", "SK": "N#31", "seed": False, "first_seen": seen})
    start, end = int((now - timedelta(days=1)).timestamp()), int(now.timestamp())
    assert extract.reads_since(public) is None
    assert opsdigest._seen(public, "kw-notice", start, end, extract.reads_since(public))[2]["expired"] == 0
    extract.mark_reads(public, now - timedelta(days=20))  # the deploy's first read record
    extract._marked.clear()
    extract.mark_reads(public, now)  # later ones never move it
    assert extract.reads_since(public) == int((now - timedelta(days=20)).timestamp())
    assert opsdigest._seen(public, "kw-notice", start, end, extract.reads_since(public))[2]["expired"] == 1
    assert opsdigest.run(now, public, ops["bridge"], FakeCloudWatch(), ops["sqs"])["fine"] is False


def test_a_retry_that_never_came_is_partial_not_pending(ops):
    public, now = ops["public"], TUESDAY
    fresh = int((now - timedelta(hours=20)).timestamp())
    for post, retry in (("41", now + timedelta(hours=2)), ("42", now - timedelta(days=3))):
        public.put_item(Item={"PK": "SRC#kw-notice", "SK": f"N#{post}", "seed": False, "first_seen": fresh,
                              "read": {"status": "partial", "retry_at": int(retry.timestamp())}})
    _, _, read = opsdigest._seen(public, "kw-notice", int((now - timedelta(days=1)).timestamp()), int(now.timestamp()),
                                 None, now.timestamp())
    assert (read["pending"], read["partial"]) == (1, 1)

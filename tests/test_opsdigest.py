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


def test_all_well_is_one_line(ops):
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
                   "content": f"✅ 어제 이상 없음 · 새 공지 {watched} · 초대 3"}
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

    newest, fresh = opsdigest._seen(Spy(), "kw-notice", 0, 2**40)
    assert (newest, fresh) == (int((TUESDAY - timedelta(hours=3)).timestamp()), 1)
    [kwargs] = asked
    assert kwargs["Select"] == "SPECIFIC_ATTRIBUTES"
    names = kwargs["ExpressionAttributeNames"]
    assert sorted(names[n.strip()] for n in kwargs["ProjectionExpression"].split(",")) == sorted(
        opsdigest.QUERY_ATTRIBUTES) == ["first_seen", "removed", "seed"]
    assert "title" not in json.dumps(real(**kwargs)["Items"], default=str)


def test_monday_covers_friday_to_sunday(ops):
    healthy(ops["public"], MONDAY)
    for day, n in (("2026-11-06", 2), ("2026-11-07", 1), ("2026-11-08", 4), ("2026-11-05", 50)):
        ops["bridge"].put_item(Item={"PK": f"DINVITES#{day}", "SK": "META", "n": n})
    cw = FakeCloudWatch()
    opsdigest.run(MONDAY, ops["public"], ops["bridge"], cw, ops["sqs"])
    [job] = posted(ops)
    assert job["content"].startswith("✅ 금~일 이상 없음 · ") and job["content"].endswith("· 초대 7")
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
        f"- 게시판: 꺼짐 1 (dept-ee) · 잠시 멈춤 2 (61.111.8.4, dept-law) · 한동안 새 글 없음 1 · 새 공지 {new}",
        "- 공지 읽기: 완료 5 · 실패 3 · 한도로 미룸 0",
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

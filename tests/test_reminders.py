"""D-3 / D-1 reminders (reminders.py and the sender's "remind" job), the summary line on new posts,
and the infrastructure for both and for the page runs."""

import json
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from kwcampus import discord, discordapi, reminders

sys.path.insert(0, str(Path(__file__).parents[1] / "infra"))
from kw_campus_stack import KwCampusStack

KEY = {"PK": "SRC#kw-notice", "SK": "N#1001"}
AT = "2026-11-18T23:59:59+09:00"  # Wednesday, at the day's end
CHANNELS = {"scholarship": "111"}


def notice(**extra):
    return {**KEY, "source": "kw-notice", "post_id": "1001", "title": "2학기 장학생 선발 안내",
            "category_id": "registration-scholarship", "posted_date": "2026-11-06", "updated_date": "2026-11-06",
            "department": "학생복지팀", "pinned": False, "seed": False, "url": "https://www.kw.ac.kr/x",
            "extracted": {"deadlines": [{"kind": "행사", "at": "2026-11-12", "text": ""},
                                        {"kind": "신청마감", "at": AT, "text": "11.18.(수) 24:00까지"}],
                          "target": "재학생(소득 0~9구간)", "eligibility": [], "how_to_apply": "KLAS 장학조회",
                          "amount": "", "contact_office": "", "category_hint": "장학", "summary": "",
                          "sources_used": ["body"], "confidence": Decimal("0.9")}, **extra}


# --- when ---------------------------------------------------------------------------------------

def levels_on(day: date, deadline: date) -> list[int]:
    return reminders.due_levels((deadline - day).days, reminders.gap_to_next_run(day))


def test_each_level_goes_out_on_the_last_weekday_run_on_or_before_its_day():
    wed = date(2026, 11, 18)  # D-3 is a Sunday: Friday (D-5) is the last run before it, never Monday (late)
    assert levels_on(date(2026, 11, 13), wed) == [3]
    assert levels_on(date(2026, 11, 16), wed) == [] and levels_on(date(2026, 11, 17), wed) == [1]
    thu = date(2026, 11, 19)
    assert levels_on(date(2026, 11, 16), thu) == [3] and levels_on(date(2026, 11, 18), thu) == [1]
    tue = date(2026, 11, 17)  # D-3 is a Saturday: Friday says D-4; D-1 on Monday
    assert levels_on(date(2026, 11, 13), tue) == [3] and levels_on(date(2026, 11, 16), tue) == [1]
    mon = date(2026, 11, 16)  # D-3 Friday, D-1 Sunday: one reply on Friday
    assert levels_on(date(2026, 11, 13), mon) == [3, 1] and levels_on(date(2026, 11, 16), mon) == []


def test_jobs_only_for_announced_live_notices_with_an_application_deadline():
    sunday = date(2026, 11, 15)  # a manual run on Sunday (D-3): the next weekday run is Monday
    items = [notice(), notice(SK="N#2", removed=True), notice(SK="N#3", seed=True),
             notice(SK="N#4", category_id="faq"), notice(SK="N#5", ttl=1),
             notice(SK="N#6", extracted={"deadlines": [{"kind": "행사", "at": "2026-11-18", "text": ""}]})]
    jobs = reminders.jobs_for(items, sunday, now_ts=datetime(2026, 11, 15, tzinfo=UTC).timestamp())
    assert jobs == [{"action": "remind", "key": KEY, "channel": "scholarship", "levels": [3], "at": AT}]


def test_the_daily_job_queues_nothing_while_notifications_are_off(aws, monkeypatch):
    queue = aws["sqs"].create_queue(QueueName="kw-discord")["QueueUrl"]
    monkeypatch.setattr(reminders, "send_jobs", lambda sqs, jobs: sqs.send_message_batch(
        QueueUrl=queue, Entries=[{"Id": str(i), "MessageBody": json.dumps(j)} for i, j in enumerate(jobs)]))
    aws["public"].put_item(Item=notice())
    assert reminders.handler({}, None) == {"status": "notify_off"}
    aws["ssm"].put_parameter(Name="/kw/collector/notify", Value="true", Type="String")
    monkeypatch.setattr(reminders, "datetime", type("D", (), {"now": staticmethod(
        lambda tz=None: datetime(2026, 11, 15, 0, 0, tzinfo=UTC))}))
    assert reminders.handler({}, None) == {"status": "ok", "jobs": 1}
    body = json.loads(aws["sqs"].receive_message(QueueUrl=queue)["Messages"][0]["Body"])
    assert body == {"action": "remind", "key": KEY, "channel": "scholarship", "levels": [3], "at": AT}


# --- the reply ----------------------------------------------------------------------------------

class FakeDiscord:
    def __init__(self, gone=False, limited=False):
        self.calls, self.gone, self.limited = [], gone, limited

    def __call__(self, method, path, token, body=None, timeout=10):
        self.calls.append((method, path, body))
        if self.limited and method == "POST":
            raise discordapi.RateLimited("discord 429", 1.0)
        if method == "GET" and self.gone:
            raise discordapi.NotFound("discord 404 on GET")
        if method == "GET":
            return {"id": "500"} if "?" not in path else []
        return {"id": "900"}


def posted(aws, **extra):
    aws["public"].put_item(Item=notice(**extra))
    aws["delivery"].put_item(Item={"PK": "SRC#kw-notice|N#1001", "SK": "CH#scholarship", "status": "sent",
                                   "channel_id": "111", "message_id": "500"})


JOB = {"action": "remind", "key": KEY, "channel": "scholarship", "levels": [3], "at": AT}
MONDAY = datetime(2026, 11, 15, 23, 30, tzinfo=UTC)  # Monday 08:30 KST: D-2


def run(aws, job, fake, when=MONDAY):
    return discord.handle_job(job, aws["public"], aws["delivery"], "t", CHANNELS, {}, send=fake, now=when)


def test_a_reminder_is_one_quiet_reply_per_level(aws):
    posted(aws)
    fake = FakeDiscord()
    assert run(aws, JOB, fake) == "reminded"
    method, path, body = fake.calls[-1]
    assert (method, path) == ("POST", "/channels/111/messages")
    assert body["content"] == "⏰ 신청 마감 D-2 · 11/18(수) 24:00까지"
    assert body["allowed_mentions"] == {"parse": [], "replied_user": False}
    assert body["message_reference"] == {"message_id": "500", "channel_id": "111", "fail_if_not_exists": True}
    record = aws["delivery"].get_item(Key={"PK": "SRC#kw-notice|N#1001", "SK": "CH#scholarship"})["Item"]
    assert record["remind_d3"] == "sent" and record["remind_d3_id"] == "900"
    assert run(aws, JOB, fake) == "already_reminded"  # an SQS redelivery or a second run
    assert run(aws, {**JOB, "levels": [1]}, fake, MONDAY + timedelta(days=1)) == "reminded"
    assert fake.calls[-1][2]["content"] == "⏰ 신청 마감 D-1 · 11/18(수) 24:00까지"
    assert discord.render_reminder("2026-11-18T18:00:00+09:00", MONDAY) == "⏰ 신청 마감 D-2 · 11/18(수) 18:00까지"
    assert discord.render_reminder("2026-11-16", MONDAY) == "⏰ 신청 마감 D-day · 11/16(월)까지"


def test_no_reminder_for_a_gone_message_a_removed_notice_or_a_changed_deadline(aws):
    posted(aws)
    assert run(aws, JOB, FakeDiscord(gone=True)) == "message_gone"
    record = aws["delivery"].get_item(Key={"PK": "SRC#kw-notice|N#1001", "SK": "CH#scholarship"})["Item"]
    assert record["remind_d3"] == "gone"
    assert run(aws, JOB, FakeDiscord()) == "already_reminded"  # never tried again
    assert run(aws, {**JOB, "levels": [1], "at": "2026-11-19"}, FakeDiscord()) == "deadline_changed"
    aws["public"].update_item(Key=KEY, UpdateExpression="SET removed = :t", ExpressionAttributeValues={":t": True})
    assert run(aws, {**JOB, "levels": [1]}, FakeDiscord()) == "removed"
    aws["delivery"].delete_item(Key={"PK": "SRC#kw-notice|N#1001", "SK": "CH#scholarship"})
    aws["public"].put_item(Item=notice())
    assert run(aws, {**JOB, "levels": [1]}, FakeDiscord()) == "nothing_to_remind"  # never announced


def test_a_429_frees_the_reminder_for_the_retry(aws):
    posted(aws)
    try:
        run(aws, JOB, FakeDiscord(limited=True))
        raise AssertionError("expected a 429")
    except discordapi.RateLimited:
        pass
    record = aws["delivery"].get_item(Key={"PK": "SRC#kw-notice|N#1001", "SK": "CH#scholarship"})["Item"]
    assert record["remind_d3"] == "claimed" and int(record["remind_d3_at"]) == 0
    assert run(aws, JOB, FakeDiscord()) == "reminded"


# --- the summary line on a new post ----------------------------------------------------------------

POST_AT = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)


def test_a_new_post_shows_one_summary_line_when_the_facts_are_there_and_sure():
    lines = discord.render(notice(), [], POST_AT).splitlines()
    assert lines[0] == "**[등록/장학]** 2학기 장학생 선발 안내"
    assert lines[1] == "📌 대상: 재학생\\(소득 0\\~9구간\\) · ⏰ 11/18(수) 24:00 마감 · 신청: KLAS 장학조회"
    only_date = notice(extracted={"deadlines": [{"kind": "신청마감", "at": "2026-11-18", "text": ""}],
                                  "confidence": Decimal("0.8")})
    assert discord.summary_line(only_date, POST_AT) == "📌 ⏰ 11/18(수) 마감"


def test_no_summary_line_without_facts_when_unsure_or_for_a_title_that_may_name_someone():
    plain = discord.render({k: v for k, v in notice().items() if k != "extracted"}, [], POST_AT)
    assert len(plain.splitlines()) == 3 and "📌" not in plain  # title, department and date, link
    assert "📌" not in discord.render(notice(extracted={**notice()["extracted"], "confidence": Decimal("0.5")}),
                                      [], POST_AT)
    empty = notice(extracted={"deadlines": [{"kind": "신청마감", "at": "2026-11-01", "text": ""}], "target": "",
                              "how_to_apply": "", "confidence": Decimal("0.95")})  # its only deadline has passed
    assert discord.summary_line(empty, POST_AT) is None
    assert discord.summary_line(notice(title="[수상] 홍*동 학생 장학금 수상"), POST_AT) is None


# --- infrastructure -------------------------------------------------------------------------------

def template(tmp_path) -> Template:
    (tmp_path / "placeholder.py").write_text("")
    stack = KwCampusStack(cdk.App(), "Test", asset_path=str(tmp_path), env=cdk.Environment(region="ap-northeast-2"))
    return Template.from_stack(stack)


def test_reminders_run_weekday_mornings_with_only_what_they_need(tmp_path):
    t = template(tmp_path)
    t.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "kw-reminders", "Handler": "kwcampus.reminders.handler",
        "Environment": {"Variables": Match.object_like({"KW_DISCORD_QUEUE_URL": Match.any_value()})}})
    t.has_resource_properties("AWS::Scheduler::Schedule", {
        "Name": "kw-remind-tick", "ScheduleExpression": "cron(30 8 ? * MON-FRI *)",
        "ScheduleExpressionTimezone": "Asia/Seoul", "Target": {"RetryPolicy": {"MaximumRetryAttempts": 0}}})
    policy = str(next(p for n, p in t.find_resources("AWS::IAM::Policy").items() if n.startswith("RemindersServiceRole")))
    assert "/kw/collector/notify" in policy and "sqs:SendMessage" in policy and "dynamodb:Query" in policy
    assert "/kw/discord" not in policy and "PutItem" not in policy and "Delivery" not in policy
    alarms = {a["Properties"]["AlarmName"] for a in t.find_resources("AWS::CloudWatch::Alarm").values()}
    assert "kw-reminders-errors" in alarms


def test_page_runs_and_extraction_alarms(tmp_path):
    t = template(tmp_path)
    t.has_resource_properties("AWS::Scheduler::Schedule", {
        "Name": "kw-collector-pages", "ScheduleExpression": "cron(10/20 9-18 ? * MON-FRI *)",
        "Target": Match.object_like({"Input": '{"phase": "pages"}'})})
    for name in ("kw-collector-tick", "kw-collector-evening"):  # the list runs carry no input
        schedule = next(s["Properties"] for s in t.find_resources("AWS::Scheduler::Schedule").values()
                        if s["Properties"]["Name"] == name)
        assert "Input" not in schedule["Target"]
    alarms = {a["Properties"]["AlarmName"]: a["Properties"] for a in t.find_resources("AWS::CloudWatch::Alarm").values()}
    assert alarms["kw-collector-extract-failed"]["MetricName"] == "ExtractFail"
    assert alarms["kw-collector-extract-auth"]["MetricName"] == "ExtractAuthFail"
    assert alarms["kw-collector-extract-auth"]["Threshold"] == 1
    collector = str(next(p for n, p in t.find_resources("AWS::IAM::Policy").items() if n.startswith("CollectorServiceRole")))
    assert "bedrock" not in collector and "/kw/collector/*" in collector  # the API keys live under /kw/collector
    assert "bedrock" not in json.dumps(t.to_json())

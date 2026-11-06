import json
from datetime import UTC, date, datetime, timedelta

import pytest
from boto3.dynamodb.types import TypeSerializer

from kwcampus import discord, discordapi, dispatcher
from kwcampus.config import KST

_ser = TypeSerializer()


def snowflake(when: datetime) -> str:
    return str((int(when.timestamp() * 1000) - discord.DISCORD_EPOCH_MS) << 22)


class FakeDiscord:
    """Records calls; POSTs get message ids made at the given clock time."""

    def __init__(self, clock):
        self.clock, self.calls, self.fail = clock, [], None

    def __call__(self, method, path, token, body=None, timeout=10):
        if self.fail:
            raise self.fail
        self.calls.append((method, path, body))
        return {"id": snowflake(self.clock())} if method == "POST" else {}


def stream(event, new, old=None):
    rec = {"eventName": event, "dynamodb": {"NewImage": {k: _ser.serialize(v) for k, v in new.items()}}}
    if old:
        rec["dynamodb"]["OldImage"] = {k: _ser.serialize(v) for k, v in old.items()}
    return rec


NOTICE = {"PK": "SRC#kw-notice", "SK": "N#53483", "title": "KLAS 보안 강화 안내", "category_id": "academic",
          "posted_date": "2026-10-15", "pinned": False, "seed": False,
          "department": "정보운영팀", "url": "https://www.kw.ac.kr/ko/life/notice.jsp?BoardMode=view&DUID=53483"}


def test_routing_and_skips():
    today = date(2026, 11, 5)
    records = [
        stream("INSERT", NOTICE),
        stream("INSERT", {**NOTICE, "SK": "N#1", "seed": True}),  # seed row
        stream("INSERT", {**NOTICE, "SK": "N#2", "pinned": True, "posted_date": "2026-10-01"}),  # old pinned
        stream("INSERT", {**NOTICE, "SK": "N#3", "category_id": "registration-scholarship"}),
        stream("MODIFY", {**NOTICE, "title": "바뀐 제목"}, old=NOTICE),
        stream("MODIFY", {**NOTICE, "department": "x"}, old=NOTICE),  # nothing readers care about
        stream("MODIFY", {**NOTICE, "updated_date": "2026-10-16"}, old={**NOTICE, "updated_date": "2026-10-15"}),
        stream("MODIFY", {**NOTICE, "pinned": True}, old=NOTICE),
        stream("INSERT", {"PK": "ORIGIN#www.kw.ac.kr", "SK": "STATE"}),  # not a notice
    ]
    jobs = dispatcher.jobs_for(records, today)
    assert [(j["action"], j["key"]["SK"], j["channel"], j.get("changed")) for j in jobs] == [
        ("post", "N#53483", "academic", None),
        ("post", "N#3", "scholarship", None),
        ("edit", "N#53483", "academic", ["title"]),
        ("edit", "N#53483", "academic", ["updated_date"]),
    ]


def test_dispatcher_sends_nothing_while_notify_is_off(aws, monkeypatch):
    queue = aws["sqs"].create_queue(QueueName="kw-discord")["QueueUrl"]
    monkeypatch.setattr(dispatcher, "DISCORD_QUEUE_URL", queue)
    dispatcher.handler({"Records": [stream("INSERT", NOTICE)]}, None)
    assert "Messages" not in aws["sqs"].receive_message(QueueUrl=queue)

    aws["ssm"].put_parameter(Name="/kw/collector/notify", Value="true", Type="String")
    dispatcher.handler({"Records": [stream("INSERT", NOTICE)]}, None)
    body = json.loads(aws["sqs"].receive_message(QueueUrl=queue)["Messages"][0]["Body"])
    assert body == {"action": "post", "key": {"PK": "SRC#kw-notice", "SK": "N#53483"}, "channel": "academic"}


def test_render_escapes_markdown_and_never_pings():
    text = discord.render({**NOTICE, "title": "**긴급** @everyone [링크](x)"})
    assert text.startswith("**[학사]** \\*\\*긴급\\*\\* @everyone \\[링크\\]\\(x\\)")
    assert text.endswith(f"<{NOTICE['url']}>")


JOB = {"action": "post", "key": {"PK": NOTICE["PK"], "SK": NOTICE["SK"]}, "channel": "academic"}
EDIT = {**JOB, "action": "edit", "changed": ["title"]}
CHANNELS = {"academic": "111"}
ROLES = {"academic": "777", "general": "778"}
DAY = datetime(2026, 11, 6, 10, 0, tzinfo=KST)  # a Friday morning


def run(aws, job, fake, when, roles=None):
    return discord.handle_job(job, aws["public"], aws["delivery"], "t", CHANNELS, roles, send=fake, now=when)


def retitle(aws, title):
    aws["public"].update_item(Key=JOB["key"], UpdateExpression="SET title = :t", ExpressionAttributeValues={":t": title})


def test_sender_posts_once_and_edits_same_message(aws):
    aws["public"].put_item(Item=NOTICE)
    clock = [DAY]
    fake = FakeDiscord(lambda: clock[0])
    assert run(aws, JOB, fake, DAY) == "posted"
    # SQS redelivery of the same job must not post twice
    assert run(aws, JOB, fake, DAY) == "already_posted"

    retitle(aws, "제목 수정")
    # 10 minutes after posting: just fix the message, no reply
    assert run(aws, EDIT, fake, DAY + timedelta(minutes=10)) == "edited"
    mid = snowflake(DAY)
    assert [(m, p, b["content"].splitlines()[0]) for m, p, b in fake.calls] == [
        ("POST", "/channels/111/messages", "**[학사]** KLAS 보안 강화 안내"),
        ("PATCH", f"/channels/111/messages/{mid}", "**[학사]** 제목 수정"),
    ]


def test_edit_after_half_an_hour_replies_once_per_kst_day(aws):
    aws["public"].put_item(Item={**NOTICE, "updated_date": "2026-10-15"})
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)
    mid = snowflake(DAY)

    retitle(aws, "제목 수정")
    assert run(aws, EDIT, fake, DAY + timedelta(minutes=45)) == "edited_replied"
    method, path, body = fake.calls[-1]
    assert (method, path) == ("POST", "/channels/111/messages")
    assert body["message_reference"] == {"message_id": mid, "channel_id": "111", "fail_if_not_exists": False}
    assert body["allowed_mentions"] == {"parse": [], "replied_user": False}
    # no 수정일 change (aicon/department style): the day the change was seen
    assert body["content"] == f"✏️ 수정됨 · 2026-11-06 · 제목 변경\n<{NOTICE['url']}>"

    # a second edit the same KST day only fixes the message
    aws["public"].update_item(Key=JOB["key"], UpdateExpression="SET updated_date = :u",
                              ExpressionAttributeValues={":u": "2026-11-06"})
    later = {**EDIT, "changed": ["updated_date"]}
    assert run(aws, later, fake, DAY + timedelta(hours=8)) == "edited"
    assert fake.calls[-1][0] == "PATCH"
    assert "(수정 2026-11-06)" in fake.calls[-1][2]["content"]

    # the next KST day (00:30 KST is still Nov 6 in UTC) may reply again, with the site's 수정일
    aws["public"].update_item(Key=JOB["key"], UpdateExpression="SET updated_date = :u",
                              ExpressionAttributeValues={":u": "2026-11-07"})
    assert run(aws, later, fake, datetime(2026, 11, 7, 0, 30, tzinfo=KST)) == "edited_replied"
    assert fake.calls[-1][2]["content"] == f"✏️ 수정됨 · 2026-11-07\n<{NOTICE['url']}>"
    record = aws["delivery"].get_item(Key={"PK": f"{NOTICE['PK']}|{NOTICE['SK']}", "SK": "CH#academic"})["Item"]
    assert record["reply_day"] == "2026-11-07" and record["reply_state"] == "sent"


def test_failed_edit_reply_is_retried_but_never_doubled(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)
    retitle(aws, "제목 수정")

    class FailOnReply(FakeDiscord):
        def __call__(self, method, path, token, body=None, timeout=10):
            if method == "POST":
                raise discord.RetryLater("discord 503")
            return super().__call__(method, path, token, body, timeout)

    when = DAY + timedelta(hours=1)
    with pytest.raises(discord.RetryLater):
        run(aws, EDIT, FailOnReply(lambda: when), when)
    # a duplicate delivery while the first attempt may still be running: no second reply
    assert run(aws, EDIT, fake, when + timedelta(seconds=30)) == "edited"
    # SQS redelivers after the visibility timeout: the stale claim is taken over and the reply goes out
    assert run(aws, EDIT, fake, when + timedelta(minutes=3)) == "edited_replied"
    assert sum(1 for m, p, b in fake.calls if m == "POST" and "message_reference" in b) == 1


def test_edit_of_a_deleted_message_is_left_alone(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)
    fake.fail = discord.NotFound("discord 404 on PATCH")
    assert run(aws, EDIT, fake, DAY + timedelta(hours=2)) == "message_gone"


def test_post_mentions_only_its_topic_role_and_not_at_night(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    assert run(aws, JOB, fake, DAY, ROLES) == "posted"
    _, _, body = fake.calls[-1]
    assert body["content"].endswith("\n<@&777>")
    assert body["allowed_mentions"] == {"parse": [], "roles": ["777"]}

    # the edit keeps the role in the text but never pings
    retitle(aws, "제목 수정")
    run(aws, EDIT, fake, DAY + timedelta(minutes=5), ROLES)
    _, _, body = fake.calls[-1]
    assert body["content"].endswith("\n<@&777>") and body["allowed_mentions"] == {"parse": []}

    for hour in (22, 23, 0, 7):  # 22:00-08:00 KST: no mention
        item = {**NOTICE, "SK": f"N#{hour}"}
        aws["public"].put_item(Item=item)
        night = datetime(2026, 11, 6, hour, 5, tzinfo=KST)
        run(aws, {**JOB, "key": {"PK": item["PK"], "SK": item["SK"]}}, fake, night, ROLES)
        _, _, body = fake.calls[-1]
        assert "<@&" not in body["content"] and body["allowed_mentions"] == {"parse": []}
    # 08:00 is daytime again
    item = {**NOTICE, "SK": "N#8"}
    aws["public"].put_item(Item=item)
    run(aws, {**JOB, "key": {"PK": item["PK"], "SK": item["SK"]}}, fake, datetime(2026, 11, 6, 8, 0, tzinfo=KST), ROLES)
    assert fake.calls[-1][2]["allowed_mentions"] == {"parse": [], "roles": ["777"]}


def test_post_without_a_role_for_its_topic_pings_nobody(aws):
    item = {**NOTICE, "category_id": "external"}  # career topic: no role configured
    aws["public"].put_item(Item=item)
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY, {**ROLES, "career": "not-a-snowflake"})
    _, _, body = fake.calls[-1]
    assert "<@&" not in body["content"] and body["allowed_mentions"] == {"parse": []}


def test_render_shows_the_sites_updated_date():
    assert "2026-10-15 (수정 2026-10-20)" in discord.render({**NOTICE, "updated_date": "2026-10-20"})
    assert "(수정" not in discord.render({**NOTICE, "updated_date": "2026-10-15"})
    assert discord.posted_at(snowflake(DAY)) == DAY.astimezone(UTC)


def test_sender_skips_unknown_channel(aws):
    aws["public"].put_item(Item=NOTICE)
    job = {"action": "post", "key": {"PK": NOTICE["PK"], "SK": NOTICE["SK"]}, "channel": "academic"}
    assert discord.handle_job(job, aws["public"], aws["delivery"], "t", {}, send=None) == "no_channel"


def test_handler_reports_a_pasted_non_token_clearly(aws, monkeypatch, capsys):
    aws["ssm"].put_parameter(Name="/kw/discord/bot-token", Value="토큰 복사 안 됨", Type="SecureString")
    aws["ssm"].put_parameter(Name="/kw/discord/channels", Value='{"academic": "111"}', Type="String")
    event = {"Records": [{"messageId": "m1", "body": json.dumps(
        {"action": "post", "key": {"PK": NOTICE["PK"], "SK": NOTICE["SK"]}, "channel": "academic"})}]}
    assert discord.handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert "not a Discord token" in capsys.readouterr().out


def test_rest_calls_carry_a_body_except_get_and_delete(monkeypatch):
    import io
    import urllib.error

    sent = []

    def fake_urlopen(request, timeout, context):
        sent.append((request.get_method(), request.data, timeout))
        if "missing" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "x", {}, io.BytesIO(b""))
        if "busy" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 429, "x", {}, io.BytesIO(b""))
        return io.BytesIO(b"")  # 204 No Content

    monkeypatch.setattr(discordapi.urllib.request, "urlopen", fake_urlopen)
    assert discordapi.call("PUT", "/guilds/1/members/2/roles/3", "t", timeout=2) == {}
    discordapi.call("DELETE", "/guilds/1/members/2/roles/3", "t")
    assert sent == [("PUT", b"{}", 2), ("DELETE", None, 10)]
    with pytest.raises(discordapi.NotFound):
        discordapi.call("PATCH", "/missing", "t", {"content": "x"})
    with pytest.raises(discordapi.RetryLater):
        discordapi.call("POST", "/busy", "t", {"content": "x"})

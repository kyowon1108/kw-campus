import json
from datetime import date

from boto3.dynamodb.types import TypeSerializer

from kwcampus import discord, dispatcher

_ser = TypeSerializer()


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
        stream("MODIFY", {**NOTICE, "department": "x"}, old=NOTICE),  # title unchanged
        stream("INSERT", {"PK": "ORIGIN#www.kw.ac.kr", "SK": "STATE"}),  # not a notice
    ]
    jobs = dispatcher.jobs_for(records, today)
    assert [(j["action"], j["key"]["SK"], j["channel"]) for j in jobs] == [
        ("post", "N#53483", "academic"),
        ("post", "N#3", "scholarship"),
        ("edit", "N#53483", "academic"),
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


def test_sender_posts_once_and_edits_same_message(aws):
    aws["public"].put_item(Item=NOTICE)
    calls = []

    def fake(method, path, token, body):
        calls.append((method, path, body["content"].splitlines()[0]))
        return {"id": "999"}

    job = {"action": "post", "key": {"PK": NOTICE["PK"], "SK": NOTICE["SK"]}, "channel": "academic"}
    channels = {"academic": "111"}
    assert discord.handle_job(job, aws["public"], aws["delivery"], "t", channels, send=fake) == "posted"
    # SQS redelivery of the same job must not post twice
    assert discord.handle_job(job, aws["public"], aws["delivery"], "t", channels, send=fake) == "already_posted"

    aws["public"].update_item(Key=job["key"], UpdateExpression="SET title = :t",
                              ExpressionAttributeValues={":t": "제목 수정"})
    edit = {**job, "action": "edit"}
    assert discord.handle_job(edit, aws["public"], aws["delivery"], "t", channels, send=fake) == "edited"
    assert calls == [
        ("POST", "/channels/111/messages", "**[학사]** KLAS 보안 강화 안내"),
        ("PATCH", "/channels/111/messages/999", "**[학사]** 제목 수정"),
    ]


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

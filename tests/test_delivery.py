import json
from datetime import UTC, date, datetime, timedelta

import pytest
from boto3.dynamodb.types import TypeSerializer

from kwcampus import discord, discordapi, dispatcher
from kwcampus.config import KST
from kwcampus.kwnotice.redaction import redact_text

_ser = TypeSerializer()


def snowflake(when: datetime) -> str:
    return str((int(when.timestamp() * 1000) - discord.DISCORD_EPOCH_MS) << 22)


class FakeDiscord:
    """Records calls; POSTs become messages with ids made at the given clock time (GET lists them)."""

    def __init__(self, clock):
        self.clock, self.calls, self.fail, self.messages = clock, [], None, []
        self.during_post = None  # called inside a POST, before it returns

    def __call__(self, method, path, token, body=None, timeout=10):
        if self.fail:
            raise self.fail
        if method == "GET":
            return list(reversed(self.messages))
        self.calls.append((method, path, body))
        if method != "POST":
            return {}
        message = {"id": str(int(snowflake(self.clock())) + len(self.messages)), **body}
        self.messages.append(message)
        if self.during_post:
            hook, self.during_post = self.during_post, None
            hook()
        return {"id": message["id"]}


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
ROLES = {"academic": "777", "general": "778", "urgent": "779"}
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
    assert body["content"] == f"✏️ 공지가 수정됐어요 · 2026-11-06 · 제목이 바뀌었어요\n<{NOTICE['url']}>"

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
    assert fake.calls[-1][2]["content"] == f"✏️ 공지가 수정됐어요 · 2026-11-07\n<{NOTICE['url']}>"
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

    # the edit keeps the role in the text (and as a mention: edits notify nobody)
    retitle(aws, "제목 수정")
    run(aws, EDIT, fake, DAY + timedelta(minutes=5), ROLES)
    _, _, body = fake.calls[-1]
    assert body["content"].endswith("\n<@&777>") and body["allowed_mentions"] == {"parse": [], "roles": ["777"]}

    for hour in (22, 23, 0, 7):  # 22:00-08:00 KST: no mention
        item = {**NOTICE, "SK": f"N#{hour}"}
        aws["public"].put_item(Item=item)
        night = datetime(2026, 11, 6, hour, 5, tzinfo=KST)
        run(aws, {**JOB, "key": {"PK": item["PK"], "SK": item["SK"]}}, fake, night, ROLES)
        _, _, body = fake.calls[-1]
        assert "<@&" not in body["content"] and body["allowed_mentions"] == {"parse": []}
    # 08:00 is daytime again (the next morning, past the 30-minute mention limit)
    item = {**NOTICE, "SK": "N#8"}
    aws["public"].put_item(Item=item)
    run(aws, {**JOB, "key": {"PK": item["PK"], "SK": item["SK"]}}, fake, datetime(2026, 11, 7, 8, 0, tzinfo=KST), ROLES)
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


def test_urgent_notices_also_ping_the_urgent_role_even_at_night(aws):
    item = {**NOTICE, "title": "11/7(금) 화도관 출입 통제 및 휴강 안내"}
    aws["public"].put_item(Item=item)
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY, ROLES)
    _, _, body = fake.calls[-1]
    assert body["content"].endswith("\n<@&777> <@&779>")
    assert body["allowed_mentions"] == {"parse": [], "roles": ["777", "779"]}

    night_item = {**item, "SK": "N#night"}
    aws["public"].put_item(Item=night_item)
    night = datetime(2026, 11, 6, 23, 40, tzinfo=KST)
    run(aws, {**JOB, "key": {"PK": item["PK"], "SK": "N#night"}}, fake, night, ROLES)
    _, _, body = fake.calls[-1]
    assert body["content"].endswith("\n<@&779>") and "<@&777>" not in body["content"]
    assert body["allowed_mentions"] == {"parse": [], "roles": ["779"]}

    # without a 긴급 role configured an urgent night post pings nobody
    quiet_item = {**item, "SK": "N#quiet"}
    aws["public"].put_item(Item=quiet_item)
    run(aws, {**JOB, "key": {"PK": item["PK"], "SK": "N#quiet"}}, fake, night, {"academic": "777"})
    assert fake.calls[-1][2]["allowed_mentions"] == {"parse": []}


def test_a_duplicate_job_while_the_first_is_posting_does_not_post_again(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    second = []
    fake.during_post = lambda: second.append(run(aws, JOB, fake, DAY + timedelta(seconds=2), ROLES))
    assert run(aws, JOB, fake, DAY, ROLES) == "posted"
    assert second == ["already_posted"]
    assert [m for m, _, _ in fake.calls] == ["POST"]
    body = fake.calls[0][2]
    assert body["enforce_nonce"] is True and len(body["nonce"]) <= 25


def test_a_post_that_died_mid_claim_is_taken_over_after_the_stale_window(aws):
    aws["public"].put_item(Item=NOTICE)
    dead = FakeDiscord(lambda: DAY)
    dead.fail = discord.RetryLater("discord unreachable")
    with pytest.raises(discord.RetryLater):
        run(aws, JOB, dead, DAY)
    fake = FakeDiscord(lambda: DAY)
    assert run(aws, JOB, fake, DAY + timedelta(seconds=30)) == "already_posted"  # maybe still in flight
    assert run(aws, JOB, fake, DAY + timedelta(minutes=3)) == "posted"  # SQS retry after the timeout


def test_a_reply_whose_answer_was_lost_is_not_posted_twice(aws):
    aws["public"].put_item(Item=NOTICE)
    clock = [DAY]
    fake = FakeDiscord(lambda: clock[0])
    run(aws, JOB, fake, DAY)
    retitle(aws, "제목 수정")

    when = DAY + timedelta(hours=1)
    clock[0] = when

    def lose_the_answer():
        raise discord.RetryLater("discord unreachable")  # Discord made the reply; we never heard back

    fake.during_post = lose_the_answer
    with pytest.raises(discord.RetryLater):
        run(aws, EDIT, fake, when)
    assert run(aws, EDIT, fake, when + timedelta(minutes=3)) == "edited_replied"  # found, not re-posted
    replies = [m for m in fake.messages if "message_reference" in m]
    assert len(replies) == 1
    record = aws["delivery"].get_item(Key={"PK": f"{NOTICE['PK']}|{NOTICE['SK']}", "SK": "CH#academic"})["Item"]
    assert record["reply_state"] == "sent" and record["reply_message_id"] == replies[0]["id"]


def test_tokens_are_checked_strictly_and_never_echoed(monkeypatch):
    real = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fake_token_value_for_tests_only_xx"
    assert discordapi.token_problem(discordapi.clean_token(f"\n{real}\n")) is None
    for bad in (None, "", "토큰 복사 안 됨", f"{real}\nX", "a.b.c", real.replace(".", " ")):
        assert discordapi.token_problem(bad), bad

    def header_error(request, timeout, context):
        raise ValueError(f"Invalid header value b'Bot {real}\\n'")

    monkeypatch.setattr(discordapi.urllib.request, "urlopen", header_error)
    with pytest.raises(RuntimeError) as e:
        discordapi.call("POST", "/channels/1/messages", real, {"content": "x"})
    assert real not in str(e.value)


def test_handler_never_mentions_the_everyone_role(aws, monkeypatch):
    seen = {}
    monkeypatch.setattr(discord, "handle_job", lambda job, p, d, token, channels, roles: seen.update(roles) or "x")
    token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fake_token_value_for_tests_only_xx"
    aws["ssm"].put_parameter(Name="/kw/discord/bot-token", Value=token + "\n", Type="SecureString")
    aws["ssm"].put_parameter(Name="/kw/discord/roles", Type="String", Value=json.dumps(
        {"guild": "100", "roles": {"academic": "100", "career": "777", "general": "x1"}}))
    event = {"Records": [{"messageId": "m1", "body": json.dumps(JOB)}]}
    assert discord.handler(event, None) == {"batchItemFailures": []}  # trailing newline stripped
    assert seen == {"career": "777"}


DEPT = {"PK": "SRC#dept-ee", "SK": "N#631", "title": "[필독] 2026-1 졸업요건 안내", "category_id": "dept-ee",
        "posted_date": "2026-03-02", "updated_date": "2026-03-02", "pinned": True, "seed": True,
        "department": "전자공학과", "url": "https://ee.kw.ac.kr/x?BoardMode=view&UID=631"}


def test_dispatcher_tells_school_edits_from_our_own_and_spots_reposts():
    today = date(2026, 11, 6)
    jobs = dispatcher.jobs_for([
        # our redaction rules changed the title (norm_v bumped), the site's 수정일 didn't: quiet
        stream("MODIFY", {**NOTICE, "title": redact_text("문의 02-940-5114"), "norm_v": 2},
               old={**NOTICE, "title": "문의 02-940-5114", "norm_v": 1}),
        # the school moved it from 일반 to 학사: quiet, addressed to where it was posted
        stream("MODIFY", NOTICE, old={**NOTICE, "category_id": "general"}),
        # a department reused UID 631 for a new semester: a repost
        stream("MODIFY", {**DEPT, "title": "[필독] 2026-2 졸업요건 안내", "updated_date": "2026-09-01"}, old=DEPT),
        # a department fixed a typo a few days later: an ordinary edit
        stream("MODIFY", {**DEPT, "title": "[필독] 2026-1 졸업 요건 안내", "updated_date": "2026-03-05"}, old=DEPT),
        # the main board reuses nothing: a big date jump is still an edit
        stream("MODIFY", {**NOTICE, "title": "새 제목", "updated_date": "2026-12-30"}, old=NOTICE),
    ], today)
    assert [(j["action"], j["channel"], j.get("quiet", False)) for j in jobs] == [
        ("edit", "academic", True),
        ("edit", "notice", True),
        ("repost", "department", False),
        ("edit", "department", False),
        ("edit", "academic", False),
    ]
    assert jobs[1]["changed"] == ["category_id"]


def test_dispatcher_resends_only_the_rejected_entries(monkeypatch):
    class Sqs:
        def __init__(self, fail_first):
            self.calls, self.fail_first = [], fail_first

        def send_message_batch(self, QueueUrl, Entries):
            self.calls.append([e["Id"] for e in Entries])
            if self.fail_first:
                self.fail_first -= 1
                return {"Failed": [{"Id": Entries[0]["Id"]}]}
            return {}

    sqs = Sqs(fail_first=1)
    dispatcher.send_jobs(sqs, [{"n": 1}, {"n": 2}, {"n": 3}])
    assert sqs.calls == [["0", "1", "2"], ["0"]]
    with pytest.raises(RuntimeError):
        dispatcher.send_jobs(Sqs(fail_first=9), [{"n": 1}])


def test_quiet_edits_fix_the_message_without_a_note(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)
    retitle(aws, "KLAS 보안 강화 안내 (정규화)")
    assert run(aws, {**EDIT, "quiet": True}, fake, DAY + timedelta(hours=3)) == "edited_quietly"
    assert [m for m, _, _ in fake.calls] == ["POST", "PATCH"]


def test_an_edit_finds_the_message_in_the_channel_it_was_posted_to(aws):
    aws["public"].put_item(Item=NOTICE)
    fake = FakeDiscord(lambda: DAY)
    run(aws, JOB, fake, DAY)  # posted under CH#academic
    moved = {**EDIT, "channel": "notice", "changed": ["category_id"], "quiet": True}
    assert run(aws, moved, fake, DAY + timedelta(hours=1)) == "edited_quietly"
    assert fake.calls[-1][1] == f"/channels/111/messages/{snowflake(DAY)}"


def test_a_repost_is_posted_once_then_edited(aws):
    item = {**DEPT, "title": "[필독] 2026-2 졸업요건 안내", "updated_date": "2026-09-01"}
    aws["public"].put_item(Item=item)
    fake = FakeDiscord(lambda: DAY)
    channels = {"notice": "111"}  # no department channel yet: 학교-공지
    repost = {"action": "repost", "key": {"PK": item["PK"], "SK": item["SK"]}, "channel": "department",
              "changed": ["title", "updated_date"]}
    call = lambda when: discord.handle_job(repost, aws["public"], aws["delivery"], "t", channels, ROLES,
                                           send=fake, now=when)
    assert call(DAY) == "posted"
    assert call(DAY + timedelta(hours=2)) == "edited_replied"  # the same reuse again: edit what we posted


def test_titles_that_may_name_someone_go_out_without_title_or_mention(aws):
    item = {**DEPT, "SK": "N#700", "title": "축하합니다! 홍길동 학생 OO경진대회 수상", "seed": False}
    aws["public"].put_item(Item=item)
    fake = FakeDiscord(lambda: DAY)
    job = {"action": "post", "key": {"PK": item["PK"], "SK": item["SK"]}, "channel": "department"}
    discord.handle_job(job, aws["public"], aws["delivery"], "t", {"notice": "111"}, {"ei": "780"}, send=fake, now=DAY)
    _, _, body = fake.calls[-1]
    assert body["content"].startswith("**[전자공학과]** 새 글이 올라왔어요. 제목은 링크를 눌러 확인해 주세요.")
    assert "홍길동" not in body["content"] and body["allowed_mentions"] == {"parse": []}
    assert discord.render({**NOTICE, "title": "장학생 김*수 외 3명"}).startswith("**[학사]** 새 글")


def test_urgent_rings_only_for_school_wide_notices(aws):
    fake = FakeDiscord(lambda: DAY)
    dept = {**DEPT, "SK": "N#701", "title": "전자회로 휴강 안내", "seed": False}
    aws["public"].put_item(Item=dept)
    job = {"action": "post", "key": {"PK": dept["PK"], "SK": dept["SK"]}, "channel": "department"}
    discord.handle_job(job, aws["public"], aws["delivery"], "t", {"notice": "111"}, {**ROLES, "ei": "780"},
                       send=fake, now=DAY)
    assert fake.calls[-1][2]["allowed_mentions"] == {"parse": [], "roles": ["780"]}  # the college only


def test_a_topic_role_is_mentioned_at_most_every_30_minutes_but_urgent_always(aws):
    fake = FakeDiscord(lambda: DAY)
    for i, (title, minutes) in enumerate([("수강신청 안내", 0), ("성적 공시 안내", 10), ("화도관 출입 통제", 20),
                                          ("졸업 사정 안내", 45)]):
        item = {**NOTICE, "SK": f"N#t{i}", "title": title}
        aws["public"].put_item(Item=item)
        run(aws, {**JOB, "key": {"PK": item["PK"], "SK": item["SK"]}}, fake, DAY + timedelta(minutes=minutes), ROLES)
    pinged = [c[2]["allowed_mentions"].get("roles", []) for c in fake.calls]
    assert pinged == [["777"], [], ["779"], ["777"]]


def test_after_a_429_the_rest_of_the_batch_waits(aws, monkeypatch):
    token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fake_token_value_for_tests_only_xx"
    aws["ssm"].put_parameter(Name="/kw/discord/bot-token", Value=token, Type="SecureString")
    calls = []

    def limited(*args, **kwargs):
        calls.append(args)
        raise discordapi.RateLimited("discord 429", 5.0)

    monkeypatch.setattr(discord, "handle_job", limited)
    event = {"Records": [{"messageId": f"m{i}", "body": json.dumps(JOB)} for i in range(3)]}
    assert discord.handler(event, None) == {"batchItemFailures": [{"itemIdentifier": f"m{i}"} for i in range(3)]}
    assert len(calls) == 1


def test_no_scheduled_run_falls_in_quiet_hours():
    # weekdays 09:00-18:40 every 20 minutes, plus 21:00 (infra); quiet hours start at 22:00
    for hour, minute in ((9, 0), (18, 40), (21, 0)):
        assert not discord.is_quiet(datetime(2026, 11, 6, hour, minute, tzinfo=KST))


def test_a_school_edit_on_an_item_without_norm_v_is_a_real_edit():
    today = date(2026, 11, 6)
    old = {**NOTICE, "updated_date": "2026-10-15"}  # stored before norm_v existed
    jobs = dispatcher.jobs_for([
        # the school retitled it; our upsert adds norm_v=2 in the same write
        stream("MODIFY", {**old, "title": "KLAS 보안 강화 안내 (기간 연장)", "norm_v": 2}, old=old),
        # norm_v bumped and the title changed beyond what redaction explains, 수정일 unchanged: real
        stream("MODIFY", {**old, "title": "완전히 다른 제목", "norm_v": 2}, old={**old, "norm_v": 1}),
        # redaction only, but the site's 수정일 moved too: real
        stream("MODIFY", {**old, "title": redact_text("문의 02-940-5114"), "updated_date": "2026-11-06", "norm_v": 2},
               old={**old, "title": "문의 02-940-5114", "norm_v": 1}),
    ], today)
    assert [j.get("quiet", False) for j in jobs] == [False, False, False]

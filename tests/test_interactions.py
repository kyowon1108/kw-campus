import base64
import json
import time

import boto3
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from kw_mcp.agent import parse_invite

from kwcampus import discord, interactions, invites
from kwcampus.routing import TOPICS

BRIDGE = "https://bridge.example.com"

GUILD = "1567793602120261653"
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fake_token_value_for_tests_only_xx"
USER = "123456789012345678"
CFG = {"guild": GUILD, "roles": {"academic": "700000000000000001", "career": "700000000000000003"}}
KEY = Ed25519PrivateKey.generate()
PUBLIC_HEX = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def signed_event(payload: dict, key=KEY, ts: int | None = None) -> dict:
    body = json.dumps(payload)
    stamp = str(ts if ts is not None else int(time.time()))
    return {"body": body, "isBase64Encoded": False, "headers": {
        "X-Signature-Ed25519": key.sign(stamp.encode() + body.encode()).hex(), "X-Signature-Timestamp": stamp}}


def click(custom_id: str, roles: list[str] = (), guild: str = GUILD) -> dict:
    return {"type": 3, "guild_id": guild, "data": {"custom_id": custom_id, "component_type": 2},
            "member": {"user": {"id": USER}, "roles": list(roles)}}


class Recorder:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, method, path, token, body=None, timeout=10):
        self.calls.append((method, path, timeout))
        if self.fail:
            raise self.fail
        return {}


@pytest.fixture
def params(aws):
    aws["ssm"].put_parameter(Name="/kw/discord/public-key", Value=PUBLIC_HEX, Type="String")
    aws["ssm"].put_parameter(Name="/kw/discord/roles", Value=json.dumps(CFG), Type="String")
    aws["ssm"].put_parameter(Name="/kw/discord/bot-token", Value=TOKEN, Type="SecureString")
    return aws


def test_ping_is_answered_only_when_signed(params):
    ok = interactions.handler(signed_event({"type": 1}), None)
    assert ok["statusCode"] == 200 and json.loads(ok["body"]) == {"type": 1}

    forged = signed_event({"type": 1}, key=Ed25519PrivateKey.generate())
    assert interactions.handler(forged, None)["statusCode"] == 401
    tampered = signed_event({"type": 1})
    tampered["body"] = tampered["body"].replace("1", "2")
    assert interactions.handler(tampered, None)["statusCode"] == 401
    assert interactions.handler({"body": "{}", "headers": {}}, None)["statusCode"] == 401
    for age in (3600, 120):  # replays: the window is 60 s
        replayed = signed_event({"type": 1}, ts=int(time.time()) - age)
        assert interactions.handler(replayed, None)["statusCode"] == 401
    encoded = signed_event({"type": 1})
    encoded.update(body=base64.b64encode(encoded["body"].encode()).decode(), isBase64Encoded=True)
    assert json.loads(interactions.handler(encoded, None)["body"]) == {"type": 1}


def test_without_a_public_key_everything_is_rejected(aws):
    assert interactions.handler(signed_event({"type": 1}), None)["statusCode"] == 401


def test_verify_rejects_junk():
    now = time.time()
    assert not interactions.verify("zz", "00", str(int(now)), b"{}", now)
    assert not interactions.verify(PUBLIC_HEX, "not-hex", str(int(now)), b"{}", now)
    assert not interactions.verify(PUBLIC_HEX, "00" * 64, "soon", b"{}", now)


def test_button_turns_its_role_on_and_off():
    send = Recorder()
    response, outcome = interactions.handle(click("kwsub:academic"), CFG, TOKEN, send)
    assert outcome == "on"
    assert send.calls == [("PUT", f"/guilds/{GUILD}/members/{USER}/roles/700000000000000001",
                           interactions.ROLE_TIMEOUT_S)]
    assert response["type"] == 4 and response["data"]["flags"] == 64  # only the clicker sees it
    assert response["data"]["allowed_mentions"] == {"parse": []}

    response, outcome = interactions.handle(click("kwsub:academic", roles=["700000000000000001"]), CFG, TOKEN, send)
    assert outcome == "off" and send.calls[-1][0] == "DELETE"
    assert "껐어요" in response["data"]["content"]


def test_buttons_never_touch_any_other_role():
    send = Recorder()
    for custom_id in ("kwsub:700000000000000009", "kwsub:", "admin", "kwsub:academic/../../roles/1",
                      "kwsub:__class__"):
        _, outcome = interactions.handle(click(custom_id), CFG, TOKEN, send)
        assert outcome == "unknown_button"
    # a known topic without a role yet (setup just ran): the map is re-read once, then "not ready"
    reads = []
    response, outcome = interactions.handle(click("kwsub:scholarship"), CFG, TOKEN, send,
                                            reload=lambda: reads.append(1) or CFG)
    assert outcome == "not_ready" and reads == [1] and "준비 중" in response["data"]["content"]
    _, outcome = interactions.handle(click("kwsub:scholarship"), CFG, TOKEN, send,
                                     reload=lambda: {**CFG, "roles": {"scholarship": "700000000000000002"}})
    assert outcome == "on" and send.calls.pop()[1].endswith("/roles/700000000000000002")
    # a role map pointing at @everyone is ignored
    _, outcome = interactions.handle(click("kwsub:academic"), {"guild": GUILD, "roles": {"academic": GUILD}},
                                     TOKEN, send)
    assert outcome == "not_ready"
    # another server, or no member (a DM)
    assert interactions.handle(click("kwsub:academic", guild="42"), CFG, TOKEN, send)[1] == "wrong_guild"
    dm = {**click("kwsub:academic"), "member": None, "user": {"id": USER}}
    assert interactions.handle(dm, CFG, TOKEN, send)[1] == "wrong_guild"
    assert send.calls == []


def test_list_shows_current_subscriptions_without_calls():
    send = Recorder()
    response, _ = interactions.handle(click("kwsub:list", roles=["700000000000000003", "55"]), CFG, TOKEN, send)
    assert response["data"]["content"].splitlines()[0] == "지금 받는 알림: 취업"
    response, _ = interactions.handle(click("kwsub:list"), CFG, TOKEN, send)
    assert "아직 켠 알림이 없어요" in response["data"]["content"]
    assert send.calls == []


def test_discord_trouble_gets_a_polite_ephemeral_answer():
    for error in (RuntimeError("discord 403 on PUT"), discord.RetryLater("discord unreachable")):
        response, outcome = interactions.handle(click("kwsub:career"), CFG, TOKEN, Recorder(fail=error))
        assert outcome == "role_failed" and response["data"]["flags"] == 64
    assert interactions.handle(click("kwsub:career"), CFG, None, Recorder())[1] == "no_token"


def test_handler_wires_ssm_values(params, monkeypatch):
    monkeypatch.setattr(interactions, "handle", lambda p, cfg, token, **kw: (
        {"type": 4, "data": {"content": f"{cfg['guild']}:{token}"}}, "x"))
    response = interactions.handler(signed_event(click("kwsub:academic")), None)
    assert json.loads(response["body"])["data"]["content"] == f"{GUILD}:{TOKEN}"


def test_handler_answers_politely_whatever_breaks(params, monkeypatch):
    def boom(payload, cfg, token, **kw):
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(interactions, "handle", boom)
    response = interactions.handler(signed_event(click("kwsub:academic")), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"]["flags"] == 64


def test_menu_has_a_button_per_topic_in_rows_of_five():
    menu = interactions.menu_message()
    rows = [[c for c in row["components"]] for row in menu["components"]]
    ids = [c["custom_id"] for row in rows for c in row]
    assert sorted(ids) == sorted([f"kwsub:{t.key}" for t in TOPICS] + ["kwsub:list", "kwreport:open"])
    assert len(rows) <= 5 and all(len(row) <= 5 for row in rows)  # Discord's limits
    assert all(len(c["label"]) <= 80 and len(c["custom_id"]) <= 100 for row in rows for c in row)
    colleges = {f"kwsub:{t.key}" for t in TOPICS if t.college}
    # college buttons get rows of their own, in grey
    assert all({c["custom_id"] in colleges for c in row} in ({True}, {False}) for row in rows)
    assert all(c["style"] == 2 for row in rows for c in row if c["custom_id"] in colleges)
    assert menu["allowed_mentions"] == {"parse": []}


def test_a_huge_timestamp_is_just_rejected(params):
    event = signed_event({"type": 1})
    event["headers"]["X-Signature-Timestamp"] = "9" * 400
    assert interactions.handler(event, None)["statusCode"] == 401
    assert not interactions.verify(PUBLIC_HEX, "00" * 64, "１２３", b"{}", time.time())  # full-width digits


def test_a_public_key_put_in_by_hand_works_at_once(aws):
    assert interactions.handler(signed_event({"type": 1}), None)["statusCode"] == 401  # not there yet
    aws["ssm"].put_parameter(Name="/kw/discord/public-key", Value=PUBLIC_HEX, Type="String")
    assert interactions.handler(signed_event({"type": 1}), None)["statusCode"] == 200  # the miss wasn't cached


# 설치하기 (button in #설치-안내) and /설치: an invite of the member's own, shown once


@pytest.fixture
def bridge(aws, monkeypatch):
    monkeypatch.setenv("KW_BRIDGE_TABLE", "kw-bridge")
    monkeypatch.setenv("KW_BRIDGE_BASE_URL", BRIDGE + "/")
    invites.clear_cache()
    table = boto3.resource("dynamodb").create_table(
        TableName="kw-bridge",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    yield table
    invites.clear_cache()


def setup_command(guild: str = GUILD) -> dict:
    return {"type": 2, "guild_id": guild, "data": {"name": interactions.SETUP_COMMAND, "type": 1},
            "member": {"user": {"id": USER}, "roles": []}}


def invite_in(response: dict) -> str:
    [row] = response["data"]["components"]
    [link] = row["components"]
    assert link["style"] == 5 and link["label"] == "설치 페이지 열기" and len(link["url"]) <= 512
    page, _, invite = link["url"].partition("#")
    assert page == BRIDGE + "/start" and invite.startswith("kwc_")
    return invite


def test_the_button_and_the_command_each_give_an_invite_with_the_install_link(bridge):
    send = Recorder()
    seen = []
    for payload in (click(interactions.SETUP_START), setup_command()):
        response, outcome = interactions.handle(payload, CFG, TOKEN, send)
        assert outcome == "setup_issued"
        assert response["type"] == 4 and response["data"]["flags"] == 64  # only the member sees it
        assert response["data"]["allowed_mentions"] == {"parse": []}
        invite = invite_in(response)
        assert f"```\n{invite}\n```" in response["data"]["content"]  # in a code block, to copy
        assert len(response["data"]["content"]) <= 2000
        server, token = parse_invite(invite)  # what the extension will read
        assert server == BRIDGE and invites.check(token, bridge, ttl_s=0)
        seen.append(invite)
    assert seen[0] != seen[1] and send.calls == []  # a new code each time; no Discord call needed
    assert invites.issued_count(bridge, USER) == 2


def test_the_fourth_invite_is_refused_kindly(bridge):
    for _ in range(invites.MAX_PER_MEMBER):
        assert interactions.handle(click(interactions.SETUP_START), CFG, TOKEN, Recorder())[1] == "setup_issued"
    response, outcome = interactions.handle(setup_command(), CFG, TOKEN, Recorder())
    assert outcome == "setup_too_many" and response["data"]["flags"] == 64
    assert "운영자" in response["data"]["content"] and "kwc_" not in json.dumps(response)
    assert "components" not in response["data"]
    assert invites.issued_count(bridge, USER) == invites.MAX_PER_MEMBER


def test_no_invite_outside_the_notice_server(bridge):
    for payload in (click(interactions.SETUP_START, guild="42"), setup_command(guild="42"),
                    {**setup_command(), "member": None, "user": {"id": USER}}):  # a DM
        response, outcome = interactions.handle(payload, CFG, TOKEN, Recorder())
        assert outcome == "wrong_guild" and "kwc_" not in json.dumps(response)
    assert bridge.scan()["Items"] == []


def test_without_the_bridge_settings_or_the_table_the_member_is_asked_to_come_back(aws, monkeypatch, capsys):
    monkeypatch.delenv("KW_BRIDGE_TABLE", raising=False)
    monkeypatch.delenv("KW_BRIDGE_BASE_URL", raising=False)
    response, outcome = interactions.handle(click(interactions.SETUP_START), CFG, TOKEN, Recorder())
    assert outcome == "setup_not_ready" and "잠시 뒤에" in response["data"]["content"]
    assert '"event": "setup_not_configured"' in capsys.readouterr().out

    monkeypatch.setenv("KW_BRIDGE_TABLE", "kw-bridge")  # set, but the table isn't there
    monkeypatch.setenv("KW_BRIDGE_BASE_URL", BRIDGE)
    response, outcome = interactions.handle(setup_command(), CFG, TOKEN, Recorder())
    assert outcome == "setup_failed" and "잠시 뒤에" in response["data"]["content"]
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line == {"event": "setup_failed", "error": "ResourceNotFoundException"}


def test_an_invite_never_reaches_the_logs(params, bridge, capsys, monkeypatch):
    logged = []
    real_log = interactions.log
    monkeypatch.setattr(interactions, "log", lambda event, **f: logged.append((event, f)) or real_log(event, **f))
    issued = []
    for payload in (click(interactions.SETUP_START), setup_command()):
        response = interactions.handler(signed_event(payload), None)
        assert response["statusCode"] == 200
        issued.append(invite_in(json.loads(response["body"])))
    for _ in range(invites.MAX_PER_MEMBER):  # past the cap too
        interactions.handler(signed_event(setup_command()), None)
    out = capsys.readouterr().out
    assert ("interaction", {"outcome": "setup_issued"}) in logged
    assert ("interaction", {"outcome": "setup_too_many"}) in logged
    for invite in issued:
        _, token = parse_invite(invite)
        assert invite not in out and token not in out and token not in repr(logged)
    assert "kwc_" not in out


def test_install_message_has_the_button_and_the_page():
    message = interactions.install_message(BRIDGE + "/")
    [row] = message["components"]
    start, page = row["components"]
    assert (start["style"], start["label"], start["custom_id"]) == (1, "설치하기", "kwsetup:start")
    assert (page["style"], page["url"]) == (5, BRIDGE + "/start") and "custom_id" not in page
    assert all(len(c["label"]) <= 80 for c in row["components"])
    assert message["allowed_mentions"] == {"parse": []} and len(message["content"]) <= 2000


def test_a_full_day_is_said_plainly(bridge, monkeypatch):
    monkeypatch.setattr(invites, "DAILY_MAX", 1)
    assert interactions.handle(setup_command(), CFG, TOKEN, Recorder())[1] == "setup_issued"
    response, outcome = interactions.handle(click(interactions.SETUP_START), CFG, TOKEN, Recorder())
    assert outcome == "setup_day_full" and response["data"]["flags"] == 64
    assert response["data"]["content"] == "오늘은 초대 코드가 다 나갔어요. 내일 다시 눌러 주세요."
    assert "kwc_" not in json.dumps(response) and invites.issued_count(bridge, USER) == 1

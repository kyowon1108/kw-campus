import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kwcampus import discord, interactions
from kwcampus.routing import TOPICS

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
    assert "껐습니다" in response["data"]["content"]


def test_buttons_never_touch_any_other_role():
    send = Recorder()
    for custom_id in ("kwsub:700000000000000009", "kwsub:", "kwsub:scholarship",  # unmapped topic
                      "admin", "kwsub:academic/../../roles/1", "kwsub:__class__"):
        _, outcome = interactions.handle(click(custom_id), CFG, TOKEN, send)
        assert outcome == "unknown_button"
    # a role map pointing at @everyone is ignored
    _, outcome = interactions.handle(click("kwsub:academic"), {"guild": GUILD, "roles": {"academic": GUILD}},
                                     TOKEN, send)
    assert outcome == "unknown_button"
    # another server, or no member (a DM)
    assert interactions.handle(click("kwsub:academic", guild="42"), CFG, TOKEN, send)[1] == "wrong_guild"
    dm = {**click("kwsub:academic"), "member": None, "user": {"id": USER}}
    assert interactions.handle(dm, CFG, TOKEN, send)[1] == "wrong_guild"
    assert send.calls == []


def test_list_shows_current_subscriptions_without_calls():
    send = Recorder()
    response, _ = interactions.handle(click("kwsub:list", roles=["700000000000000003", "55"]), CFG, TOKEN, send)
    assert response["data"]["content"] == "지금 받는 알림: 취업"
    response, _ = interactions.handle(click("kwsub:list"), CFG, TOKEN, send)
    assert "아직 받는 알림이 없습니다" in response["data"]["content"]
    assert send.calls == []


def test_discord_trouble_gets_a_polite_ephemeral_answer():
    for error in (RuntimeError("discord 403 on PUT"), discord.RetryLater("discord unreachable")):
        response, outcome = interactions.handle(click("kwsub:career"), CFG, TOKEN, Recorder(fail=error))
        assert outcome == "role_failed" and response["data"]["flags"] == 64
    assert interactions.handle(click("kwsub:career"), CFG, None, Recorder())[1] == "no_token"


def test_handler_wires_ssm_values(params, monkeypatch):
    monkeypatch.setattr(interactions, "handle", lambda p, cfg, token: (
        {"type": 4, "data": {"content": f"{cfg['guild']}:{token}"}}, "x"))
    response = interactions.handler(signed_event(click("kwsub:academic")), None)
    assert json.loads(response["body"])["data"]["content"] == f"{GUILD}:{TOKEN}"


def test_handler_answers_politely_whatever_breaks(params, monkeypatch):
    def boom(payload, cfg, token):
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(interactions, "handle", boom)
    response = interactions.handler(signed_event(click("kwsub:academic")), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"]["flags"] == 64


def test_menu_has_a_button_per_topic_in_rows_of_five():
    menu = interactions.menu_message()
    rows = [[c for c in row["components"]] for row in menu["components"]]
    ids = [c["custom_id"] for row in rows for c in row]
    assert sorted(ids) == sorted([f"kwsub:{t.key}" for t in TOPICS] + ["kwsub:list"])
    assert len(rows) <= 5 and all(len(row) <= 5 for row in rows)  # Discord's limits
    assert all(len(c["label"]) <= 80 and len(c["custom_id"]) <= 100 for row in rows for c in row)
    colleges = {f"kwsub:{t.key}" for t in TOPICS if t.college}
    # college buttons get rows of their own, in grey
    assert all({c["custom_id"] in colleges for c in row} in ({True}, {False}) for row in rows)
    assert all(c["style"] == 2 for row in rows for c in row if c["custom_id"] in colleges)
    assert menu["allowed_mentions"] == {"parse": []}

import json

import boto3
import pytest
from kw_mcp.agent import parse_invite

from kwcampus import invites

MEMBER = "123456789012345678"
SERVER = "https://bridge.example.com"


@pytest.fixture
def bridge(aws):
    invites.clear_cache()
    t = boto3.resource("dynamodb").create_table(
        TableName="kw-bridge",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    yield t
    invites.clear_cache()


def test_issue_gives_an_invite_the_pc_can_read_and_stores_only_the_hash(bridge):
    invite = invites.issue(bridge, MEMBER, SERVER + "/", now=1000)
    server, token = parse_invite(invite)  # the PC side's own parser
    assert server == SERVER and invites.TOKEN.fullmatch(token)
    h = invites.digest(token)
    assert invites.check(token, bridge) == h
    items = bridge.scan()["Items"]
    dumped = json.dumps(items, default=str)
    assert token not in dumped and invite not in dumped
    inv = next(i for i in items if i["PK"] == f"INVITE#{h}")
    assert inv["issued_to"] == MEMBER and inv["source"] == "discord" and int(inv["issued_at"]) == 1000
    assert invites.issued_count(bridge, MEMBER) == 1


def test_member_cap(bridge):
    for _ in range(invites.MAX_PER_MEMBER):
        invites.issue(bridge, MEMBER, SERVER)
    with pytest.raises(invites.TooMany):
        invites.issue(bridge, MEMBER, SERVER)
    assert invites.issued_count(bridge, MEMBER) == invites.MAX_PER_MEMBER


def test_hand_made_list_still_counts_and_unknown_tokens_fail(bridge, aws):
    listed = "a" * 30
    aws["ssm"].put_parameter(Name=invites.TOKENS_PARAM, Type="String", Value=json.dumps([invites.digest(listed)]))
    assert invites.check(listed, bridge, ttl_s=0) == invites.digest(listed)
    assert invites.check("b" * 30, bridge, ttl_s=0) is None
    assert invites.check(None, bridge) is None and invites.check("x" * 201, bridge) is None
    assert invites.check("kwc_" + "c" * 30, bridge) is None  # a whole invite is not a token
    assert invites.hash_alive("not-a-hash", bridge) is False


def test_a_devices_counter_alone_is_not_an_invite(bridge):
    """INVITE# items also carry the bridge's PC count for hand-made invites; that alone must not let a token in."""
    token = "c" * 30
    bridge.put_item(Item={"PK": f"INVITE#{invites.digest(token)}", "SK": "META", "devices": 1})
    assert invites.check(token, bridge, ttl_s=0) is None


def test_revoke(bridge):
    _, token = parse_invite(invites.issue(bridge, MEMBER, SERVER))
    h = invites.digest(token)
    assert invites.check(token, bridge) == h
    assert invites.revoke(bridge, h) is True
    assert invites.check(token, bridge) is None
    assert invites.revoke(bridge, "0" * 64) is False


def test_bad_member_id(bridge):
    with pytest.raises(ValueError):
        invites.issue(bridge, "not-an-id", SERVER)


def test_a_failed_invite_write_does_not_use_up_a_slot(bridge, monkeypatch):
    """The count and the invite go in one transaction: if the invite can't be written, the count stays."""
    import secrets as _secrets

    _, token = parse_invite(invites.issue(bridge, MEMBER, SERVER))
    monkeypatch.setattr(_secrets, "token_urlsafe", lambda n: token)  # the same token again: its INVITE# exists
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        invites.issue(bridge, MEMBER, SERVER)
    assert invites.issued_count(bridge, MEMBER) == 1


def test_a_day_has_a_cap_across_members_and_a_refusal_uses_up_nothing(bridge, monkeypatch):
    monkeypatch.setattr(invites, "DAILY_MAX", 2)
    day = 1_794_000_000  # 2026-11-07 (KST)
    invites.issue(bridge, MEMBER, SERVER, now=day)
    invites.issue(bridge, "223456789012345678", SERVER, now=day)
    with pytest.raises(invites.DayFull):
        invites.issue(bridge, "323456789012345678", SERVER, now=day)
    assert invites.issued_count(bridge, "323456789012345678") == 0  # the member's own cap is untouched
    assert len([i for i in bridge.scan()["Items"] if i["PK"].startswith("INVITE#")]) == 2
    counter = bridge.get_item(Key={"PK": "DINVITES#2026-11-07", "SK": "META"})["Item"]
    assert int(counter["n"]) == 2 and int(counter["ttl"]) == day + 8 * 86400  # Monday's digest reads Friday's
    invites.issue(bridge, "323456789012345678", SERVER, now=day + 86400)  # the next day starts again


def test_the_transaction_is_idempotent_on_retry(bridge, monkeypatch):
    """botocore may retry a commit whose answer was lost; the same ClientRequestToken makes DynamoDB answer
    with that commit's success. (moto doesn't model the idempotency itself, so this checks the token.)"""
    sent = []
    real = bridge.meta.client.transact_write_items
    monkeypatch.setattr(bridge.meta.client, "transact_write_items", lambda **kw: sent.append(kw) or real(**kw))
    _, token = parse_invite(invites.issue(bridge, MEMBER, SERVER))
    assert sent[0]["ClientRequestToken"] == invites.digest(token)[:36]


def test_a_withdrawn_invite_stops_within_the_cache_time(bridge, aws, monkeypatch):
    """The answer and the SSM list are each kept half of ttl_s, so a hand-made invite taken off the list
    stops within ttl_s (60 s), not two stacked minutes."""
    import time

    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    token = "d" * 30
    aws["ssm"].put_parameter(Name=invites.TOKENS_PARAM, Type="String", Value=json.dumps([invites.digest(token)]))
    assert invites.check(token, bridge)
    aws["ssm"].put_parameter(Name=invites.TOKENS_PARAM, Type="String", Value="[]", Overwrite=True)
    clock[0] += 29
    assert invites.check(token, bridge)  # both caches still hold it
    clock[0] += 2
    assert invites.check(token, bridge) is None  # 31 s: the answer and the list are read again



def _commit_then_lose_the_answer(bridge, monkeypatch, error):
    real = bridge.meta.client.transact_write_items

    def lost(**kwargs):
        real(**kwargs)
        raise error

    monkeypatch.setattr(bridge.meta.client, "transact_write_items", lost)


def test_a_commit_whose_answer_is_lost_still_gives_the_code(bridge, monkeypatch):
    from botocore.exceptions import ReadTimeoutError

    _commit_then_lose_the_answer(bridge, monkeypatch, ReadTimeoutError(endpoint_url="https://dynamodb"))
    invite = invites.issue(bridge, MEMBER, SERVER)
    _, token = parse_invite(invite)
    assert invites.check(token, bridge, ttl_s=0) == invites.digest(token)
    assert invites.issued_count(bridge, MEMBER) == 1  # the slot it used has its code


def test_a_lost_answer_without_a_commit_is_raised(bridge, monkeypatch):
    from botocore.exceptions import EndpointConnectionError

    def never(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://dynamodb")

    monkeypatch.setattr(bridge.meta.client, "transact_write_items", never)
    with pytest.raises(EndpointConnectionError):
        invites.issue(bridge, MEMBER, SERVER)
    assert invites.issued_count(bridge, MEMBER) == 0
    # a row under that hash issued to someone else is not taken for our commit
    monkeypatch.setattr(invites, "digest", lambda token: "e" * 64)
    bridge.put_item(Item={"PK": "INVITE#" + "e" * 64, "SK": "META", "issued_to": "999999999999999999"})
    with pytest.raises(EndpointConnectionError):
        invites.issue(bridge, MEMBER, SERVER)

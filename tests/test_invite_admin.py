"""One place for invites: the SSM list copied into kw-bridge (scripts/invites_admin.py migrate), the
KW_INVITES_SSM=0 switch, and the admin-only /초대회수 and /초대현황 commands. Made-up ids; AWS is moto."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import test_bridge
from kw_mcp.agent import parse_invite
from test_bridge import FakeDevice, signed

from kwcampus import interactions, invites
from kwcampus.bridge import app as bridge
from kwcampus.bridge import store

_spec = importlib.util.spec_from_file_location("invites_admin", Path(__file__).parents[1] / "scripts" / "invites_admin.py")
admin_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin_script)

env = test_bridge.env
GUILD = "1567793602120261653"
CFG = {"guild": GUILD, "roles": {}}
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.fake_token_value_for_tests_only_xx"
ADMIN, MEMBER, OTHER = "111111111111111111", "123456789012345678", "223456789012345678"


def command(name: str, perms: int | None, target: str | None = None, guild: str = GUILD) -> dict:
    data = {"name": name, "type": 1}
    if target is not None:
        data["options"] = [{"name": "member", "type": 6, "value": target}]
    member = {"user": {"id": ADMIN}, "roles": []}
    if perms is not None:
        member["permissions"] = str(perms)
    return {"type": 2, "guild_id": guild, "data": data, "member": member}


def never_send(*args, **kwargs):
    raise AssertionError("no Discord call is needed")


@pytest.fixture
def table(env, monkeypatch):
    monkeypatch.setenv("KW_BRIDGE_TABLE", "kw-bridge")
    return env["table"]


# --- migrate -------------------------------------------------------------------------------------------

def test_migrate_dry_run_writes_nothing_and_apply_copies_keeping_the_pc_count(table):
    hand_made = [invites.digest("hand-made-token-" + "x" * 10), invites.digest("other-token-" + "y" * 12)]
    with_pcs = hand_made[0]
    table.put_item(Item={"PK": f"INVITE#{with_pcs}", "SK": "META", "devices": 2})  # the bridge's PC count
    invites.issue(table, MEMBER, bridge.BASE_URL)  # a Discord one already there
    before = table.scan()["Items"]

    counts = admin_script.migrate(table, [*hand_made, "not-a-hash"], apply=False, now=1000)
    assert counts == {"listed": 3, "malformed": 1, "already": 0, "copied": 2, "total": 3, "total_revoked": 0}
    assert table.scan()["Items"] == before

    counts = admin_script.migrate(table, [*hand_made, "not-a-hash"], apply=True, now=1000)
    assert counts["copied"] == 2 and counts["total"] == 3
    item = table.get_item(Key={"PK": f"INVITE#{with_pcs}", "SK": "META"})["Item"]
    assert item["issued_to"] == "manual" and item["source"] == "manual" and int(item["issued_at"]) == 1000
    assert int(item["devices"]) == 2
    assert invites.totals(table) == {"n": 3, "revoked": 0}


def test_migrate_is_idempotent_and_leaves_owned_items_alone(table):
    listed = invites.digest("hand-made-token-" + "x" * 10)
    admin_script.migrate(table, [listed], apply=True, now=1000)
    table.update_item(Key={"PK": f"INVITE#{listed}", "SK": "META"}, UpdateExpression="SET devices = :d",
                      ExpressionAttributeValues={":d": 1})  # a PC registered with it since
    snapshot = table.scan()["Items"]
    counts = admin_script.migrate(table, [listed, listed], apply=True, now=9999)
    assert counts == {"listed": 2, "malformed": 0, "already": 1, "copied": 0, "total": 1, "total_revoked": 0}
    assert table.scan()["Items"] == snapshot  # issued_at, devices and the total untouched


def test_migrate_reads_the_ssm_list_and_a_missing_one_is_empty(env, aws):
    assert admin_script.ssm_hashes(aws["ssm"]) == [invites.digest(test_bridge.INVITE)]
    aws["ssm"].delete_parameter(Name=invites.TOKENS_PARAM)
    assert admin_script.ssm_hashes(aws["ssm"]) == []


# --- KW_INVITES_SSM=0 ----------------------------------------------------------------------------------

def test_with_the_switch_off_only_dynamodb_counts(table, aws, monkeypatch):
    token = test_bridge.INVITE  # on the SSM list only (the env fixture put it there)
    assert invites.check(token, table, ttl_s=0)
    monkeypatch.setenv("KW_INVITES_SSM", "0")
    assert invites.check(token, table, ttl_s=0) is None  # the list isn't read any more
    admin_script.migrate(table, admin_script.ssm_hashes(aws["ssm"]), apply=True)
    assert invites.check(token, table, ttl_s=0) == invites.digest(token)  # its INVITE# item carries it
    _, discord_token = parse_invite(invites.issue(table, MEMBER, bridge.BASE_URL))
    assert invites.check(discord_token, table, ttl_s=0)


def test_with_the_switch_off_the_ssm_list_is_never_fetched(table, monkeypatch):
    monkeypatch.setenv("KW_INVITES_SSM", "0")
    monkeypatch.setattr(invites.params, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SSM read")))
    assert invites.check("z" * 30, table, ttl_s=0) is None
    monkeypatch.setenv("KW_INVITES_SSM", "1")
    with pytest.raises(AssertionError, match="SSM read"):
        invites.check("z" * 30, table, ttl_s=0)


def test_a_pc_registered_with_a_hand_made_invite_keeps_working_after_the_switch(env, table, monkeypatch):
    c = env["client"]
    device = FakeDevice()
    r = c.post("/device/register", json={"invite": test_bridge.INVITE, "public_key": device.jwk})
    assert r.status_code == 201, r.text
    device.uid = r.json()["uid"]
    admin_script.migrate(table, [invites.digest(test_bridge.INVITE)], apply=True)
    monkeypatch.setenv("KW_INVITES_SSM", "0")
    invites.clear_cache()
    assert "error" not in c.post("/device/pair", json=signed(device, "pair")).json()
    assert int(table.get_item(Key={"PK": f"INVITE#{invites.digest(test_bridge.INVITE)}", "SK": "META"})
               ["Item"]["devices"]) == 1


# --- /초대회수 and /초대현황 ------------------------------------------------------------------------------

def test_revoke_withdraws_every_invite_of_the_member_and_their_pc_is_refused(env, table):
    c = env["client"]
    _, token = parse_invite(invites.issue(table, MEMBER, bridge.BASE_URL))
    invites.issue(table, MEMBER, bridge.BASE_URL)
    _, other_token = parse_invite(invites.issue(table, OTHER, bridge.BASE_URL))
    device = FakeDevice()
    r = c.post("/device/register", json={"invite": token, "public_key": device.jwk})
    assert r.status_code == 201, r.text
    device.uid = r.json()["uid"]
    store.add_connection(table, device.uid, "conn-1")
    assert "error" not in c.post("/device/pair", json=signed(device, "pair")).json()
    assert c.get(f"/c/{token}/api/notices").status_code == 200

    response, outcome = interactions.handle(command("초대회수", interactions.MANAGE_GUILD, MEMBER), CFG, TOKEN,
                                            never_send)
    assert outcome == "revoked" and response["data"]["flags"] == interactions.EPHEMERAL
    assert response["data"]["content"] == f"<@{MEMBER}> 님 초대 코드 2개를 회수했어요. 1분 안에 연결이 끊겨요."
    assert response["data"]["allowed_mentions"] == {"parse": []}  # the member isn't pinged
    assert c.post("/device/pair", json=signed(device, "pair")).json()["error"] == "invite_revoked"
    assert c.get(f"/c/{token}/api/notices").status_code == 404
    assert invites.check(other_token, table, ttl_s=0)  # someone else's invite is untouched
    assert invites.issued_count(table, MEMBER) == 2  # the slots stay used: no fresh codes for them
    assert invites.totals(table) == {"n": 3, "revoked": 2}

    response, outcome = interactions.handle(command("초대회수", interactions.ADMINISTRATOR, MEMBER), CFG, TOKEN,
                                            never_send)
    assert outcome == "revoke_none" and "이미 다 회수했어요" in response["data"]["content"]
    assert invites.totals(table)["revoked"] == 2
    response, outcome = interactions.handle(command("초대회수", interactions.MANAGE_GUILD, "333333333333333333"),
                                            CFG, TOKEN, never_send)
    assert outcome == "revoke_none" and "받은 초대 코드가 없어요" in response["data"]["content"]


def test_a_cached_answer_elsewhere_ends_within_the_cache_window(table, monkeypatch):
    """Other containers (the bridge, the public API) still hold the old answer up to CACHE_S/2."""
    import time

    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    _, token = parse_invite(invites.issue(table, MEMBER, bridge.BASE_URL))
    assert invites.check(token, table)
    cached = dict(invites._cache)
    invites.revoke_member(table, MEMBER)
    invites._cache.update(cached)  # as if another Lambda had read it a moment before
    assert invites.check(token, table)
    clock[0] += invites.CACHE_S // 2 + 1
    assert invites.check(token, table) is None


def test_non_admins_are_refused_and_nothing_changes(table):
    _, token = parse_invite(invites.issue(table, MEMBER, bridge.BASE_URL))
    for perms in (None, 0, 1 << 10 | 1 << 11, "junk"):  # no field, nothing, view+send, garbage
        payload = command("초대회수", None, MEMBER)
        if perms is not None:
            payload["member"]["permissions"] = str(perms)
        response, outcome = interactions.handle(payload, CFG, TOKEN, never_send)
        assert outcome == "admin_refused" and response["data"]["content"] == "운영자만 쓰는 명령이에요."
        assert response["data"]["flags"] == interactions.EPHEMERAL
        assert interactions.handle(command("초대현황", 0), CFG, TOKEN, never_send)[1] == "admin_refused"
    assert invites.check(token, table, ttl_s=0)
    # an admin in another server is still the wrong server
    assert interactions.handle(command("초대회수", interactions.MANAGE_GUILD, MEMBER, guild="42"), CFG, TOKEN,
                               never_send)[1] == "wrong_guild"


def test_status_shows_today_the_total_and_the_caps(table):
    invites.issue(table, MEMBER, bridge.BASE_URL)
    invites.issue(table, OTHER, bridge.BASE_URL)
    invites.revoke_member(table, OTHER)
    response, outcome = interactions.handle(command("초대현황", interactions.MANAGE_GUILD), CFG, TOKEN, never_send)
    assert outcome == "invite_status" and response["data"]["flags"] == interactions.EPHEMERAL
    assert response["data"]["content"] == (
        f"오늘 2개 · 전체 2개 (회수 1개)\n-# 한 사람당 {invites.MAX_PER_MEMBER}개, 하루 모두 합쳐 "
        f"{invites.DAILY_MAX}개까지 나가요.")


def test_revoke_without_a_member_or_without_the_table(table, monkeypatch):
    response, outcome = interactions.handle(command("초대회수", interactions.MANAGE_GUILD, "not-an-id"), CFG,
                                            TOKEN, never_send)
    assert outcome == "revoke_no_member" and response["data"]["content"] == "회수할 멤버를 골라 주세요."
    monkeypatch.delenv("KW_BRIDGE_TABLE")
    assert interactions.handle(command("초대현황", interactions.MANAGE_GUILD), CFG, TOKEN, never_send)[1] == \
        "admin_not_ready"


def test_a_withdrawal_is_audited_by_admin_without_the_member_or_hashes(env, table, aws, capsys):
    from test_interactions import PUBLIC_HEX, signed_event

    aws["ssm"].put_parameter(Name="/kw/discord/public-key", Value=PUBLIC_HEX, Type="String")
    aws["ssm"].put_parameter(Name="/kw/discord/roles", Value=json.dumps(CFG), Type="String")
    _, token = parse_invite(invites.issue(table, MEMBER, bridge.BASE_URL))
    capsys.readouterr()
    out = interactions.handler(signed_event(command("초대회수", interactions.MANAGE_GUILD, MEMBER)), None)
    assert out["statusCode"] == 200 and "1개를 회수했어요" in json.loads(out["body"])["data"]["content"]
    logs = capsys.readouterr().out
    audit = [json.loads(line) for line in logs.splitlines() if '"invites_revoked"' in line]
    assert audit == [{"event": "invites_revoked", "by": ADMIN, "withdrawn": 1}]
    assert MEMBER not in logs and invites.digest(token) not in logs and token not in logs
    assert '"outcome": "revoked"' in logs


def test_issue_keeps_a_running_total(table):
    for member in (MEMBER, OTHER):
        invites.issue(table, member, bridge.BASE_URL)
    assert invites.totals(table) == {"n": 2, "revoked": 0}
    with pytest.raises(invites.TooMany):
        for _ in range(invites.MAX_PER_MEMBER):
            invites.issue(table, MEMBER, bridge.BASE_URL)
    assert invites.totals(table)["n"] == 1 + invites.MAX_PER_MEMBER  # a refused one isn't counted


def test_admin_commands_are_shaped_for_registration():
    revoke, status = interactions.admin_commands()
    assert (revoke["name"], status["name"]) == ("초대회수", "초대현황")
    for c in (revoke, status):
        assert c["default_member_permissions"] == str(1 << 5) and len(c["description"]) <= 100
    assert revoke["options"] == [{"type": 6, "name": "member", "description": "회수할 멤버", "required": True}]


# --- a migrated hand-made invite, before and after the switch ------------------------------------------

def set_list(aws, hashes: list[str]) -> None:
    aws["ssm"].put_parameter(Name=invites.TOKENS_PARAM, Type="String", Value=json.dumps(hashes), Overwrite=True)


def test_before_the_switch_taking_a_migrated_hash_off_the_list_still_withdraws_it(table, aws, monkeypatch):
    listed = invites.digest(test_bridge.INVITE)
    admin_script.migrate(table, [listed], apply=True)
    assert invites.check(test_bridge.INVITE, table, ttl_s=0) == listed
    set_list(aws, [])  # the owner withdraws it the old way
    assert invites.check(test_bridge.INVITE, table, ttl_s=0) is None  # its manual item doesn't keep it alive
    set_list(aws, [listed])
    assert invites.check(test_bridge.INVITE, table, ttl_s=0) == listed
    monkeypatch.setenv("KW_INVITES_SSM", "0")  # after the switch the item alone decides
    set_list(aws, [])
    assert invites.check(test_bridge.INVITE, table, ttl_s=0) == listed
    assert invites.revoke(table, listed) is True
    assert invites.check(test_bridge.INVITE, table, ttl_s=0) is None


def test_a_discord_invite_is_alive_by_its_item_whatever_the_list_says(table, aws):
    _, token = parse_invite(invites.issue(table, MEMBER, bridge.BASE_URL))
    set_list(aws, [])
    assert invites.check(token, table, ttl_s=0) == invites.digest(token)


def test_once_the_list_is_deleted_a_deploy_without_the_flag_cuts_manual_invites(table, aws, monkeypatch):
    """The runbook warning: invitesSsm=0 must stay once /kw/mcp/tokens is gone."""
    listed = invites.digest(test_bridge.INVITE)
    admin_script.migrate(table, [listed], apply=True)
    aws["ssm"].delete_parameter(Name=invites.TOKENS_PARAM)
    monkeypatch.setenv("KW_INVITES_SSM", "0")
    assert invites.check(test_bridge.INVITE, table, ttl_s=0) == listed
    monkeypatch.delenv("KW_INVITES_SSM")
    assert invites.check(test_bridge.INVITE, table, ttl_s=0) is None


def test_revoke_records_when_and_counts_once(table):
    invites.issue(table, MEMBER, bridge.BASE_URL)
    [code_hash] = table.get_item(Key={"PK": f"DMEMBER#{MEMBER}", "SK": "META"})["Item"]["invites"]
    assert invites.revoke(table, code_hash, now=5000) is True
    assert invites.revoke(table, code_hash, now=6000) is False  # already withdrawn: no second count
    item = table.get_item(Key={"PK": f"INVITE#{code_hash}", "SK": "META"})["Item"]
    assert item["revoked"] is True and int(item["revoked_at"]) == 5000
    assert invites.totals(table) == {"n": 1, "revoked": 1}
    assert invites.revoke(table, "0" * 64) is False and invites.totals(table)["revoked"] == 1


# --- the script's own safety: account check, empty list, revoke --hash -----------------------------------

ACCOUNT = "123456789012"  # moto's


def run_script(capsys, *argv: str) -> tuple[int, str, str]:
    code = admin_script.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_every_run_names_the_account_and_the_table(table, capsys):
    code, out, _ = run_script(capsys, "migrate")
    assert code == 0
    assert out.splitlines()[0] == f"account {ACCOUNT} · table arn:aws:dynamodb:ap-northeast-2:{ACCOUNT}:table/kw-bridge"
    assert "dry run: nothing written" in out


def test_apply_needs_the_expected_account(table, capsys):
    before = table.scan()["Items"]
    for extra in ((), ("--expect-account", "999999999999")):
        code, _, err = run_script(capsys, "migrate", "--apply", *extra)
        assert code == 2 and f"--expect-account {ACCOUNT}" in err
        assert table.scan()["Items"] == before
    code, out, _ = run_script(capsys, "migrate", "--apply", "--expect-account", ACCOUNT)
    assert code == 0 and "copied=1" in out
    item = table.get_item(Key={"PK": f"INVITE#{invites.digest(test_bridge.INVITE)}", "SK": "META"})["Item"]
    assert item["source"] == "manual"


def test_an_empty_or_missing_list_is_refused_and_the_total_kept(table, aws, capsys):
    invites.issue(table, MEMBER, bridge.BASE_URL)
    table.update_item(Key={"PK": invites.TOTAL_PK, "SK": "META"}, UpdateExpression="SET n = :n",
                      ExpressionAttributeValues={":n": 40})
    for prepare in (lambda: set_list(aws, []), lambda: aws["ssm"].delete_parameter(Name=invites.TOKENS_PARAM)):
        prepare()
        code, _, err = run_script(capsys, "migrate", "--apply", "--expect-account", ACCOUNT)
        assert code == 2 and "empty or missing" in err
        assert invites.totals(table)["n"] == 40
    with pytest.raises(admin_script.Refused):
        admin_script.migrate(table, [], apply=False)


def test_revoke_by_hash_dry_run_then_apply(table, aws, capsys):
    listed = invites.digest(test_bridge.INVITE)
    admin_script.migrate(table, [listed], apply=True)
    code, out, _ = run_script(capsys, "revoke", "--hash", listed)
    assert code == 0 and "invite: source=manual revoked=no" in out and "would withdraw it" in out
    assert "still on /kw/mcp/tokens" in out  # before the switch the list keeps it alive
    assert not table.get_item(Key={"PK": f"INVITE#{listed}", "SK": "META"})["Item"].get("revoked")

    assert run_script(capsys, "revoke", "--hash", listed, "--apply")[0] == 2  # no account given
    set_list(aws, [])
    code, out, _ = run_script(capsys, "revoke", "--hash", listed.upper(), "--apply", "--expect-account", ACCOUNT)
    assert code == 0 and "withdrawn" in out and "still on" not in out
    assert table.get_item(Key={"PK": f"INVITE#{listed}", "SK": "META"})["Item"]["revoked"] is True
    code, out, _ = run_script(capsys, "revoke", "--hash", listed, "--apply", "--expect-account", ACCOUNT)
    assert code == 0 and "already withdrawn" in out

    code, _, err = run_script(capsys, "revoke", "--hash", "f" * 64)
    assert code == 2 and "no invite with that hash" in err
    code, _, err = run_script(capsys, "revoke", "--hash", "not-a-hash")
    assert code == 2 and "64 lowercase hex" in err

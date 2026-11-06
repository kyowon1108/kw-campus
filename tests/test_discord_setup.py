import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest

from kwcampus.discord import NotFound
from kwcampus.interactions import menu_message
from kwcampus.routing import TOPICS

_spec = importlib.util.spec_from_file_location("discord_setup", Path(__file__).parents[1] / "scripts" / "discord_setup.py")
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)

GUILD, BOT, BOT_ROLE = "100", "900", "901"
ADD_REACTIONS = 1 << 6
EVERYONE_PERMS = (ds.VIEW_CHANNEL | ds.SEND_MESSAGES | ds.READ_MESSAGE_HISTORY | ds.MENTION_EVERYONE
                  | ds.CREATE_PUBLIC_THREADS | ds.CREATE_PRIVATE_THREADS | ds.SEND_MESSAGES_IN_THREADS)
CHANNELS = {"notice": "201", "academic": "202", "scholarship": "203", "career": "204", "international": "205"}


class FakeDiscord:
    """Just enough of a guild for discord_setup: roles, channels, overwrites, messages."""

    def __init__(self, bot_perms: int = ds.BOT_RUNTIME | ds.MANAGE_CHANNELS, notifications: int = 1,
                 shift: bool = True):
        self.notifications = notifications
        self.shift = shift  # False: new roles share position 1 with whatever is there, as the live server did
        self.roles = [
            {"id": GUILD, "name": "@everyone", "permissions": str(EVERYONE_PERMS), "position": 0},
            {"id": BOT_ROLE, "name": "KW Campus", "permissions": str(bot_perms), "position": 2, "managed": True},
            {"id": "950", "name": "운영진", "permissions": str(ds.ADMINISTRATOR), "position": 3},
        ]
        names = {"201": "학교-공지", "202": "학사", "203": "장학", "204": "취업", "205": "국제"}
        self.channels = [{"id": "300", "name": "공지", "type": 4, "permission_overwrites": []}]
        for cid, name in names.items():
            self.channels.append({"id": cid, "name": name, "type": 0, "parent_id": "300", "guild_id": GUILD,
                                  "permission_overwrites": []})
        # someone already turned reactions off in #학교-공지; that must survive
        self.channels[1]["permission_overwrites"] = [{"id": GUILD, "type": 0, "allow": "0", "deny": str(ADD_REACTIONS)}]
        self.channels.append({"id": "209", "name": "운영", "type": 0, "parent_id": "300", "guild_id": GUILD,
                              "permission_overwrites": [{"id": GUILD, "type": 0, "allow": "0", "deny": "1024"}]})
        self.messages: dict[str, list[dict]] = {}
        self.writes: list[tuple[str, str]] = []
        self._next = 1000

    def _id(self) -> str:
        self._next += 1
        return str(self._next)

    def _channel(self, cid: str) -> dict:
        for c in self.channels:
            if c["id"] == cid:
                return c
        raise NotFound(cid)

    def call(self, method: str, path: str, body: dict | None = None):
        if method != "GET":
            self.writes.append((method, path))
        if (method, path) == ("GET", "/users/@me"):
            return {"id": BOT, "username": "KW Campus"}
        if (method, path) == ("GET", f"/guilds/{GUILD}"):
            return {"id": GUILD, "name": "테스트 서버", "default_message_notifications": self.notifications}
        if (method, path) == ("GET", f"/guilds/{GUILD}/roles"):
            return copy.deepcopy(self.roles)
        if (method, path) == ("GET", f"/guilds/{GUILD}/channels"):
            return copy.deepcopy(self.channels)
        if (method, path) == ("GET", f"/guilds/{GUILD}/members/{BOT}"):
            return {"user": {"id": BOT}, "roles": [BOT_ROLE]}
        if (method, path) == ("POST", f"/guilds/{GUILD}/roles"):
            for r in self.roles[1:] if self.shift else []:
                r["position"] += 1  # new roles go to the bottom, just above @everyone
            role = {"id": self._id(), "position": 1, "managed": False, **body}
            self.roles.append(role)
            return role
        if (method, path) == ("POST", f"/guilds/{GUILD}/channels"):
            channel = {"id": self._id(), "guild_id": GUILD, **body}
            self.channels.append(channel)
            return channel
        if method == "PATCH" and (m := re.fullmatch(rf"/guilds/{GUILD}/roles/(\d+)", path)):
            role = next(r for r in self.roles if r["id"] == m[1])
            role.update(body)
            return role
        if m := re.fullmatch(r"/channels/(\d+)/permissions/(\d+)", path):
            channel = self._channel(m[1])
            rest = [o for o in channel["permission_overwrites"] if o["id"] != m[2]]
            channel["permission_overwrites"] = [*rest, {"id": m[2], **body}]
            return None
        if m := re.fullmatch(r"/channels/(\d+)/messages(?:\?limit=50)?", path):
            if method == "GET":
                return list(reversed(self.messages.get(m[1], [])))
            message = {"id": self._id(), "author": {"id": BOT}, **copy.deepcopy(body)}
            self.messages.setdefault(m[1], []).append(message)
            return message
        if m := re.fullmatch(r"/channels/(\d+)/messages/(\d+)", path):
            for message in self.messages.get(m[1], []):
                if message["id"] == m[2]:
                    if method == "PATCH":
                        message.update(copy.deepcopy(body))
                    return copy.deepcopy(message)
            raise NotFound(path)
        if m := re.fullmatch(r"/channels/(\d+)", path):
            if method == "PATCH":
                self._channel(m[1]).update(body)
            return copy.deepcopy(self._channel(m[1]))
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def ssm(aws):
    aws["ssm"].put_parameter(Name="/kw/discord/channels", Value=json.dumps(CHANNELS), Type="String")
    return aws["ssm"]


def run(api, ssm, apply=False, department=False):
    lines = []
    code = ds.Setup(api, ssm, apply=apply, department=department, out=lines.append).run()
    return code, "\n".join(lines)


def overwrite(api, cid, oid):
    return next((o for o in api._channel(cid)["permission_overwrites"] if o["id"] == oid), None)


def test_dry_run_changes_nothing(ssm):
    api = FakeDiscord()
    code, out = run(api, ssm)
    assert code == 0 and api.writes == []
    assert "dry-run: 아무것도 바꾸지 않았습니다" in out
    assert out.count("역할 만들기") == len(TOPICS) and "#알림-설정 만들기" in out
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/kw/discord/roles")


def test_apply_sets_everything_up_and_a_second_run_is_a_no_op(ssm):
    api = FakeDiscord()
    code, out = run(api, ssm, apply=True)
    assert code == 0, out

    created = {r["name"]: r for r in api.roles if r["name"].endswith("알림")}
    assert set(created) == {t.role for t in TOPICS}
    assert all(r["permissions"] == "0" and r["mentionable"] is False for r in created.values())
    bot_position = next(r["position"] for r in api.roles if r["id"] == BOT_ROLE)
    assert all(r["position"] < bot_position for r in created.values())

    menu_channel = next(c for c in api.channels if c["name"] == "알림-설정")
    assert menu_channel["parent_id"] == "300"
    for cid in [*CHANNELS.values(), menu_channel["id"]]:
        everyone = overwrite(api, cid, GUILD)
        assert int(everyone["allow"]) & ds.EVERYONE_ALLOW == ds.EVERYONE_ALLOW
        assert int(everyone["deny"]) & ds.EVERYONE_DENY == ds.EVERYONE_DENY
        bot = overwrite(api, cid, BOT)
        assert bot["type"] == 1 and int(bot["allow"]) & ds.BOT_ALLOW == ds.BOT_ALLOW
        assert bool(int(bot["allow"]) & ds.MENTION_EVERYONE) == (cid != menu_channel["id"])
        assert int(bot["allow"]) & ds.ADMINISTRATOR == 0
    assert int(overwrite(api, "201", GUILD)["deny"]) & ADD_REACTIONS  # kept
    assert overwrite(api, "209", BOT) is None  # the private channel is untouched

    [menu] = api.messages[menu_channel["id"]]
    assert menu["content"] == menu_message()["content"]
    saved = json.loads(ssm.get_parameter(Name="/kw/discord/roles")["Parameter"]["Value"])
    assert saved == {"guild": GUILD, "roles": {t.key: created[t.role]["id"] for t in TOPICS},
                     "menu": {"channel": menu_channel["id"], "message": menu["id"]}}

    api.writes.clear()
    code, out = run(api, ssm, apply=True)
    assert code == 0 and api.writes == [], api.writes
    assert "바꿀 것이 없습니다" in out


def test_menu_is_edited_in_place_when_it_differs(ssm):
    api = FakeDiscord()
    run(api, ssm, apply=True)
    menu_channel = next(c for c in api.channels if c["name"] == "알림-설정")["id"]
    api.messages[menu_channel][0]["content"] = "옛 문구"
    api.writes.clear()
    run(api, ssm, apply=True)
    message_id = api.messages[menu_channel][0]["id"]
    assert api.writes == [("PATCH", f"/channels/{menu_channel}/messages/{message_id}")]
    assert api.messages[menu_channel][0]["content"] == menu_message()["content"]

    # the saved id is gone (deleted message): found again by scanning, not posted twice
    ssm.put_parameter(Name="/kw/discord/roles", Type="String", Overwrite=True, Value=json.dumps(
        {**json.loads(ssm.get_parameter(Name="/kw/discord/roles")["Parameter"]["Value"]),
         "menu": {"channel": menu_channel, "message": "1"}}))
    api.writes.clear()
    run(api, ssm, apply=True)
    assert len(api.messages[menu_channel]) == 1 and not any(m == "POST" for m, _ in api.writes)


def test_department_channel_is_optional(ssm):
    api = FakeDiscord()
    run(api, ssm, apply=True, department=True)
    dept = next(c for c in api.channels if c["name"] == "학과-공지")
    channels = json.loads(ssm.get_parameter(Name="/kw/discord/channels")["Parameter"]["Value"])
    assert channels == {**CHANNELS, "department": dept["id"]}
    assert int(next(o for o in dept["permission_overwrites"] if o["id"] == BOT)["allow"]) & ds.MENTION_EVERYONE


def test_permission_report_names_what_to_turn_on_and_off(ssm):
    _, out = run(FakeDiscord(bot_perms=ds.VIEW_CHANNEL | ds.SEND_MESSAGES), ssm)
    assert ("--apply 전에 봇 역할에 잠시 켜 둘 권한: 채널 관리(Manage Channels), 링크 첨부(Embed Links), "
            "역할 관리(Manage Roles)") in out
    assert "계속 필요한 권한이 없습니다: 링크 첨부(Embed Links), 역할 관리(Manage Roles)" in out
    assert "@everyone 역할에 '@everyone·모든 역할 멘션'이 켜져 있어" in out

    _, out = run(FakeDiscord(bot_perms=ds.ADMINISTRATOR), ssm)
    assert "봇에 관리자 권한이 있습니다" in out
    assert ds.BOT_RUNTIME == 268520448  # the value given to the user for the bot role


def test_missing_channel_config_stops_early(aws):
    lines = []
    assert ds.Setup(FakeDiscord(), aws["ssm"], apply=True, department=False, out=lines.append).run() == 2
    assert "notice 채널 ID가 없습니다" in lines[0]


def test_a_renamed_topic_renames_its_role_in_place(ssm):
    api = FakeDiscord()
    run(api, ssm, apply=True)
    topic = next(t for t in TOPICS if t.key == "kwlife")
    role = next(r for r in api.roles if r["name"] == topic.role)
    role["name"] = "옛 이름 알림"  # as if topics.TOPICS had just been renamed
    api.writes.clear()
    _, out = run(api, ssm, apply=True)
    assert api.writes == [("PATCH", f"/guilds/{GUILD}/roles/{role['id']}")]
    assert role["name"] == topic.role and f"'옛 이름 알림' → '{topic.role}'" in out


def test_an_unsafe_existing_role_stops_everything(ssm):
    api = FakeDiscord()
    api.roles.append({"id": "960", "name": "취업 알림", "permissions": str(ds.MENTION_EVERYONE), "position": 1})
    api.roles[1]["position"] = 5  # the bot is above it
    code, out = run(api, ssm, apply=True)
    assert code == 1 and api.writes == []
    assert "'취업 알림' (960)은 버튼으로 나눠 줄 수 없습니다" in out
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/kw/discord/roles")


def test_roles_sharing_the_bots_position_rank_by_id(ssm):
    # the live server: the bot role at 1, every new role also at 1 (newer, so below the bot role)
    api = FakeDiscord(shift=False)
    api.roles[1]["position"] = 1
    code, out = run(api, ssm, apply=True)
    assert code == 0, out
    assert {r["position"] for r in api.roles if r["name"].endswith("알림")} == {1}
    api.writes.clear()
    code, out = run(api, ssm, apply=True)
    assert code == 0 and api.writes == [] and "바꿀 것이 없습니다" in out, out

    # a role at the same position but older than the bot role ranks above it
    api = FakeDiscord()
    api.roles[1]["position"] = 1
    api.roles.append({"id": "800", "name": "학사 알림", "permissions": "0", "position": 1})
    code, out = run(api, ssm, apply=True)
    assert code == 1 and "'학사 알림' (800)은 버튼으로 나눠 줄 수 없습니다" in out


def test_a_channel_hidden_from_everyone_stays_hidden(ssm):
    api = FakeDiscord()
    api._channel("202")["permission_overwrites"] = [
        {"id": GUILD, "type": 0, "allow": "0", "deny": str(ds.VIEW_CHANNEL)},
        {"id": "950", "type": 0, "allow": str(ds.VIEW_CHANNEL), "deny": "0"},  # a verified role sees it
    ]
    _, out = run(api, ssm, apply=True)
    everyone = overwrite(api, "202", GUILD)
    assert int(everyone["deny"]) & ds.VIEW_CHANNEL and not int(everyone["allow"]) & ds.VIEW_CHANNEL
    assert int(everyone["deny"]) & ds.SEND_MESSAGES and int(everyone["allow"]) & ds.READ_MESSAGE_HISTORY
    assert "#학사: @everyone에게 숨겨진 채널" in out


def test_channels_with_the_same_name_are_both_made_read_only(ssm):
    api = FakeDiscord()
    api._channel("203")["name"] = "학사"  # same name as 202, another category
    run(api, ssm, apply=True)
    assert overwrite(api, "202", GUILD) and overwrite(api, "203", GUILD)


class RecordingSsm:
    """The moto SSM client, with put_parameter calls logged into the fake Discord's write list."""

    def __init__(self, ssm, api, fail=False):
        self._ssm, self._api, self._fail = ssm, api, fail
        self.exceptions = ssm.exceptions

    def get_parameter(self, **kw):
        return self._ssm.get_parameter(**kw)

    def put_parameter(self, **kw):
        self._api.writes.append(("SSM", kw["Name"]))
        if self._fail:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutParameter")
        return self._ssm.put_parameter(**kw)


def test_an_administrator_bot_changes_nothing(ssm):
    api = FakeDiscord(bot_perms=ds.ADMINISTRATOR)
    code, out = run(api, ssm, apply=True)
    assert code == 2 and api.writes == []
    assert "관리자 권한이 있는 봇으로는 아무것도 바꾸지 않습니다" in out
    code, out = run(api, ssm)  # the dry-run still shows the whole plan
    assert code == 0 and "역할 만들기" in out and "봇에 관리자 권한이 있습니다" in out


def test_a_department_channel_set_by_hand_is_made_read_only(ssm):
    api = FakeDiscord()
    api.channels.append({"id": "206", "name": "학과", "type": 0, "parent_id": "300", "guild_id": GUILD,
                         "permission_overwrites": []})
    ssm.put_parameter(Name="/kw/discord/channels", Type="String", Overwrite=True,
                      Value=json.dumps({**CHANNELS, "department": "206"}))
    run(api, ssm, apply=True)
    assert int(overwrite(api, "206", BOT)["allow"]) & ds.MENTION_EVERYONE
    assert int(overwrite(api, "206", GUILD)["deny"]) & ds.SEND_MESSAGES

    ssm.put_parameter(Name="/kw/discord/channels", Type="String", Overwrite=True,
                      Value=json.dumps({**CHANNELS, "department": "999"}))
    _, out = run(FakeDiscord(), ssm)
    assert "'department' 채널(999)을 서버에서 찾지 못했습니다" in out


def test_new_channels_inherit_their_category_and_view_is_never_forced(ssm):
    api = FakeDiscord()
    api._channel("300")["permission_overwrites"] = [
        {"id": GUILD, "type": 0, "allow": "0", "deny": str(ds.VIEW_CHANNEL)},
        {"id": "950", "type": 0, "allow": str(ds.VIEW_CHANNEL), "deny": "0"},  # the verified role
    ]
    run(api, ssm, apply=True)
    menu = next(c for c in api.channels if c["name"] == "알림-설정")
    everyone = overwrite(api, menu["id"], GUILD)
    assert int(everyone["deny"]) & ds.VIEW_CHANNEL and not int(everyone["allow"]) & ds.VIEW_CHANNEL
    assert overwrite(api, menu["id"], "950")["allow"] == str(ds.VIEW_CHANNEL)
    assert not int(overwrite(api, "201", GUILD)["allow"]) & ds.VIEW_CHANNEL  # nor on existing channels


def test_a_same_named_channel_elsewhere_is_not_adopted(ssm):
    api = FakeDiscord()
    api.channels.append({"id": "400", "name": "알림-설정", "type": 0, "parent_id": "399", "guild_id": GUILD,
                         "permission_overwrites": []})
    run(api, ssm, apply=True)
    assert len([c for c in api.channels if c["name"] == "알림-설정"]) == 2
    assert overwrite(api, "400", GUILD) is None


def test_mentionable_roles_are_turned_off(ssm):
    api = FakeDiscord()
    run(api, ssm, apply=True)
    role = next(r for r in api.roles if r["name"] == "학사 알림")
    role["mentionable"] = True
    api.writes.clear()
    _, out = run(api, ssm, apply=True)
    assert api.writes == [("PATCH", f"/guilds/{GUILD}/roles/{role['id']}")] and role["mentionable"] is False
    assert "'학사 알림' 멤버 멘션 끄기" in out


def test_dangerous_and_unnamed_bot_permissions_are_reported(ssm):
    _, out = run(FakeDiscord(bot_perms=ds.BOT_RUNTIME | ds.BAN_MEMBERS | (1 << 45)), ssm)
    assert "위험한 권한이 있습니다: 멤버 차단(Ban Members)" in out
    assert f"기타 권한 비트 {hex(1 << 45)}" in out


def test_the_role_map_is_written_before_the_buttons_change(ssm):
    api = FakeDiscord()
    code, _ = run(api, RecordingSsm(ssm, api), apply=True)
    assert code == 0
    order = [w for w in api.writes if w[0] == "SSM" or "/messages" in w[1]]
    assert order[0] == ("SSM", "/kw/discord/roles") and order[1][0] == "POST"
    saved = json.loads(ssm.get_parameter(Name="/kw/discord/roles")["Parameter"]["Value"])
    assert saved["menu"]["message"]  # and the new message id afterwards


def test_an_ssm_write_failure_shows_the_command_to_run(ssm):
    api = FakeDiscord()
    code, out = run(api, RecordingSsm(ssm, api, fail=True), apply=True)
    assert code == 1
    assert "aws ssm put-parameter --region ap-northeast-2 --name /kw/discord/roles --type String" in out
    assert "MTIz" not in out  # no token anywhere


def test_the_default_notification_level_is_checked(ssm):
    _, out = run(FakeDiscord(notifications=0), ssm)
    assert "기본 알림 설정을 '@멘션만'으로" in out
    _, out = run(FakeDiscord(notifications=1), ssm)
    assert "@멘션만" not in out


def test_a_stale_channel_topic_is_refreshed(ssm):
    api = FakeDiscord()
    run(api, ssm, apply=True, department=True)
    dept = next(c for c in api.channels if c["name"] == "학과-공지")
    dept["topic"] = "인공지능융합대학·전자정보공과대학 학과 공지. 봇만 글을 씁니다."
    api.writes.clear()
    run(api, ssm, apply=True, department=True)
    assert api.writes == [("PATCH", f"/channels/{dept['id']}")] and dept["topic"] == ds.DEPARTMENT_TOPIC

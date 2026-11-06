"""Set up the Discord server for notice subscriptions. Dry-run unless --apply.

    uv run --with 'botocore[crt]' python scripts/discord_setup.py            # prints the plan only
    uv run --with 'botocore[crt]' python scripts/discord_setup.py --apply    # does it

What --apply does, each step only when something is missing or different (safe to run again):
1. one role per topic (routing.TOPICS): no permissions, not mentionable by members
2. #알림-설정 next to #학교-공지 (and #학과-공지 with --department-channel)
3. notice channels read-only for @everyone (no messages, no threads), the bot may post and
   mention roles there; other permission bits already set on those channels are kept
4. the button message in #알림-설정, posted once and edited when the buttons change
5. /kw/discord/roles in SSM (guild, role ids, menu message); with --department-channel also the
   "department" key of /kw/discord/channels

The bot token is read from SSM and never printed. Channel ids come from /kw/discord/channels.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from kwcampus.config import (
    DISCORD_CHANNELS_PARAM,
    DISCORD_ROLES_PARAM,
    DISCORD_TOKEN_PARAM,
)
from kwcampus.discord import API, USER_AGENT, NotFound, token_problem
from kwcampus.fetch import TLS
from kwcampus.interactions import PREFIX, menu_message
from kwcampus.routing import TOPICS

REGION = "ap-northeast-2"

ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
MENTION_EVERYONE = 1 << 17  # also "mention any role"; granted to the bot only inside notice channels
MANAGE_ROLES = 1 << 28
CREATE_PUBLIC_THREADS = 1 << 35
CREATE_PRIVATE_THREADS = 1 << 36
SEND_MESSAGES_IN_THREADS = 1 << 38

PERMISSION_NAMES = {
    ADMINISTRATOR: "관리자(Administrator)",
    MANAGE_CHANNELS: "채널 관리(Manage Channels)",
    VIEW_CHANNEL: "채널 보기(View Channels)",
    SEND_MESSAGES: "메시지 보내기(Send Messages)",
    EMBED_LINKS: "링크 첨부(Embed Links)",
    READ_MESSAGE_HISTORY: "메시지 기록 보기(Read Message History)",
    MENTION_EVERYONE: "@everyone·모든 역할 멘션(Mention All Roles)",
    MANAGE_ROLES: "역할 관리(Manage Roles)",
    CREATE_PUBLIC_THREADS: "공개 스레드 만들기(Create Public Threads)",
    CREATE_PRIVATE_THREADS: "비공개 스레드 만들기(Create Private Threads)",
    SEND_MESSAGES_IN_THREADS: "스레드에서 메시지 보내기(Send Messages in Threads)",
}

# Members read notice channels but never write in them.
EVERYONE_ALLOW = VIEW_CHANNEL | READ_MESSAGE_HISTORY
EVERYONE_DENY = SEND_MESSAGES | CREATE_PUBLIC_THREADS | CREATE_PRIVATE_THREADS | SEND_MESSAGES_IN_THREADS
BOT_ALLOW = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY
# What the bot keeps at server level once setup is done (buttons need Manage Roles).
BOT_RUNTIME = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY | MANAGE_ROLES

NOTICE_CHANNEL_KEYS = ("notice", "academic", "scholarship", "career", "international")
MENU_CHANNEL = "알림-설정"
MENU_TOPIC = "버튼을 눌러 받고 싶은 공지 알림을 고르세요. 이 채널에는 봇만 글을 씁니다."
DEPARTMENT_CHANNEL = "학과-공지"
DEPARTMENT_TOPIC = "인공지능융합대학·전자정보공과대학 학과 공지. 봇만 글을 씁니다."
TEXT_CHANNEL = 0
NEW = "<새로 만듦>"


def names(bits: int) -> str:
    return ", ".join(name for bit, name in PERMISSION_NAMES.items() if bits & bit) or "없음"


class DiscordAPI:
    """Bot-token REST calls. 429s wait and retry; the token never appears in output or errors."""

    def __init__(self, token: str, sleep=time.sleep):
        self._token = token
        self._sleep = sleep

    def call(self, method: str, path: str, body: dict | None = None):
        for _ in range(5):
            request = urllib.request.Request(
                f"{API}{path}", method=method,
                data=None if body is None else json.dumps(body).encode(),
                headers={"Authorization": f"Bot {self._token}", "Content-Type": "application/json",
                         "User-Agent": USER_AGENT, "X-Audit-Log-Reason": "kw-campus discord_setup"},
            )
            try:
                with urllib.request.urlopen(request, timeout=15, context=TLS) as response:
                    return json.loads(response.read() or b"null")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = float((json.loads(e.read() or b"{}") or {}).get("retry_after", 1))
                    self._sleep(min(wait, 10) + 0.2)
                    continue
                if e.code == 404:
                    raise NotFound(f"{method} {path}: 404") from None
                hint = " (봇 권한이 부족합니다)" if e.code == 403 else ""
                raise RuntimeError(f"Discord {e.code} on {method} {path}{hint}") from None
        raise RuntimeError(f"Discord kept rate-limiting {method} {path}")


def _get_param(ssm, name: str, decrypt: bool = False) -> str | None:
    try:
        return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def _merge(existing: dict | None, allow: int, deny: int) -> tuple[int, int]:
    """The overwrite after setup: our bits forced, every other bit left as it was."""
    old_allow = int(existing["allow"]) if existing else 0
    old_deny = int(existing["deny"]) if existing else 0
    return (old_allow | allow) & ~deny, (old_deny | deny) & ~allow


def _overwrites_for(guild_id: str, bot_id: str, mention: bool) -> list[tuple[str, int, int, int, str]]:
    """(target id, type, allow, deny, label) — type 0 is a role (@everyone), 1 a member (the bot).
    The bot's own overwrite goes first: if a later call fails, the bot can still post there."""
    bot_allow = BOT_ALLOW | (MENTION_EVERYONE if mention else 0)
    return [(bot_id, 1, bot_allow, 0, "봇"), (guild_id, 0, EVERYONE_ALLOW, EVERYONE_DENY, "@everyone")]


def _button_ids(message: dict) -> list[tuple]:
    return [(c.get("type"), c.get("style"), c.get("label"), c.get("custom_id"))
            for row in message.get("components") or [] for c in row.get("components") or []]


def _is_menu(message: dict, bot_id: str) -> bool:
    return (message.get("author") or {}).get("id") == bot_id and any(
        str(cid or "").startswith(PREFIX) for *_, cid in _button_ids(message))


class Setup:
    def __init__(self, api: DiscordAPI, ssm, *, apply: bool, department: bool, out=print):
        self.api, self.ssm, self.apply, self.department, self.out = api, ssm, apply, department, out
        self.changes = 0
        self.needed = MANAGE_ROLES  # server-level bot permissions this run needs

    def step(self, mark: str, text: str) -> None:
        if mark in "+~":
            self.changes += 1
        self.out(f"  {mark} {text}")

    def run(self) -> int:
        channels_cfg = json.loads(_get_param(self.ssm, DISCORD_CHANNELS_PARAM) or "{}")
        saved = json.loads(_get_param(self.ssm, DISCORD_ROLES_PARAM) or "{}")
        if not channels_cfg.get("notice"):
            self.out(f"! {DISCORD_CHANNELS_PARAM}에 notice 채널 ID가 없습니다. 먼저 채널 ID를 넣어 주세요.")
            return 2

        me = self.api.call("GET", "/users/@me")
        bot_id = str(me["id"])
        notice = self.api.call("GET", f"/channels/{channels_cfg['notice']}")
        guild_id = str(notice["guild_id"])
        guild = self.api.call("GET", f"/guilds/{guild_id}")
        roles = self.api.call("GET", f"/guilds/{guild_id}/roles")
        channels = self.api.call("GET", f"/guilds/{guild_id}/channels")
        member = self.api.call("GET", f"/guilds/{guild_id}/members/{bot_id}")

        role_by_id = {str(r["id"]): r for r in roles}
        bot_roles = [role_by_id[str(r)] for r in member.get("roles", []) if str(r) in role_by_id]
        everyone = int(role_by_id.get(guild_id, {}).get("permissions", 0))
        own = 0
        for r in bot_roles:
            own |= int(r["permissions"])
        top = max((int(r["position"]) for r in bot_roles), default=0)
        mode = "적용(--apply)" if self.apply else "계획만 보기(dry-run)"
        self.out(f"Discord 서버: {guild.get('name')} ({guild_id}) · 봇: {me.get('username')} ({bot_id}) · {mode}")

        role_ids = self._roles(guild_id, roles, saved, top)
        by_id = {str(c["id"]): c for c in channels}
        targets = self._channels(guild_id, bot_id, notice, channels_cfg, saved, by_id, channels)
        self._permissions(guild_id, bot_id, targets, by_id)
        menu_channel = targets[MENU_CHANNEL][0]
        message_id = self._menu(menu_channel, bot_id, saved)
        self._ssm(guild_id, role_ids, menu_channel, message_id, saved, channels_cfg, targets)
        self._report_permissions(everyone, own)

        if self.apply:
            self.out(f"\n완료: {self.changes}건 바꿨습니다." if self.changes else "\n바꿀 것이 없습니다.")
        else:
            self.out(f"\ndry-run: 아무것도 바꾸지 않았습니다. 바뀔 항목 {self.changes}건. 적용하려면 --apply")
        return 0

    def _roles(self, guild_id: str, roles: list[dict], saved: dict, top: int) -> dict[str, str]:
        self.out("\n[구독 역할]")
        by_id = {str(r["id"]): r for r in roles}
        by_name = {r["name"]: r for r in roles}
        ids = {}
        for topic in TOPICS:
            role = by_id.get(str((saved.get("roles") or {}).get(topic.key))) or by_name.get(topic.role)
            if role is None:
                self.step("+", f"'{topic.role}' 역할 만들기 (권한 없음, 멤버가 멘션할 수 없음)")
                if self.apply:
                    role = self.api.call("POST", f"/guilds/{guild_id}/roles", {
                        "name": topic.role, "permissions": "0", "mentionable": False, "hoist": False})
                ids[topic.key] = str(role["id"]) if role else NEW
                continue
            ids[topic.key] = str(role["id"])
            self.step("=", f"'{role['name']}' 있음 ({role['id']})")
            if int(role.get("permissions", 0)) or role.get("managed"):
                self.step("!", f"'{role['name']}'에 권한이 있거나 연동 역할입니다. 구독 역할은 권한 없는 역할이어야 합니다.")
            if int(role.get("position", 0)) >= top:
                self.step("!", f"'{role['name']}'이 봇 역할보다 위에 있습니다. 서버 설정 → 역할에서 봇 역할을 위로 끌어 주세요.")
        return ids

    def _find_or_create(self, guild_id: str, notice: dict, name: str, topic: str, known_id: str | None,
                        by_id: dict, channels: list[dict], overwrites: list) -> tuple[str, bool]:
        channel = by_id.get(str(known_id)) or next(
            (c for c in channels if c.get("name") == name and c.get("type") == TEXT_CHANNEL), None)
        if channel:
            self.step("=", f"#{channel['name']} 있음 ({channel['id']})")
            return str(channel["id"]), False
        self.needed |= MANAGE_CHANNELS
        for *_, allow, deny, _who in overwrites:
            self.needed |= allow | deny
        self.step("+", f"#{name} 만들기 (#{notice.get('name')}과 같은 분류, 읽기 전용 권한 포함)")
        if not self.apply:
            return NEW, True
        made = self.api.call("POST", f"/guilds/{guild_id}/channels", {
            "name": name, "type": TEXT_CHANNEL, "topic": topic, "parent_id": notice.get("parent_id"),
            "permission_overwrites": [{"id": oid, "type": kind, "allow": str(allow), "deny": str(deny)}
                                      for oid, kind, allow, deny, _ in overwrites]})
        by_id[str(made["id"])] = made
        return str(made["id"]), True

    def _channels(self, guild_id, bot_id, notice, channels_cfg, saved, by_id, channels) -> dict:
        """{label: (channel id, mention allowed, created now)} of every channel made read-only."""
        self.out("\n[채널]")
        targets = {}
        for key in NOTICE_CHANNEL_KEYS:
            cid = str(channels_cfg.get(key) or "")
            if cid not in by_id:
                self.step("!", f"{DISCORD_CHANNELS_PARAM}의 '{key}' 채널을 서버에서 찾지 못했습니다. 건너뜁니다.")
                continue
            self.step("=", f"#{by_id[cid]['name']} ({key})")
            targets[by_id[cid]["name"]] = (cid, True, False)
        menu_id, created = self._find_or_create(
            guild_id, notice, MENU_CHANNEL, MENU_TOPIC, (saved.get("menu") or {}).get("channel"), by_id, channels,
            _overwrites_for(guild_id, bot_id, mention=False))
        targets[MENU_CHANNEL] = (menu_id, False, created)
        if self.department:
            dept_id, created = self._find_or_create(
                guild_id, notice, DEPARTMENT_CHANNEL, DEPARTMENT_TOPIC, channels_cfg.get("department"), by_id,
                channels, _overwrites_for(guild_id, bot_id, mention=True))
            targets[DEPARTMENT_CHANNEL] = (dept_id, True, created)
        elif channels_cfg.get("department"):
            self.step("=", f"학과 공지 채널 ({channels_cfg['department']})")
        else:
            self.step("=", "학과 공지는 계속 #학교-공지로 갑니다 (--department-channel로 #학과-공지를 만들 수 있음)")
        return targets

    def _permissions(self, guild_id: str, bot_id: str, targets: dict, by_id: dict) -> None:
        self.out("\n[채널 권한] @everyone: 보기·기록 보기 허용, 메시지·스레드 거부 / 봇: 보기·보내기·링크 첨부·기록 보기 허용")
        for label, (cid, mention, created) in targets.items():
            if created:
                continue  # made with the right overwrites just now (or in this plan)
            existing = {str(o["id"]): o for o in (by_id.get(cid) or {}).get("permission_overwrites") or []}
            for oid, kind, allow, deny, who in _overwrites_for(guild_id, bot_id, mention):
                old = existing.get(oid)
                new_allow, new_deny = _merge(old, allow, deny)
                if old and (int(old["allow"]), int(old["deny"])) == (new_allow, new_deny):
                    self.step("=", f"#{label} {who}: 이미 맞음")
                    continue
                self.needed |= allow | deny
                extra = " + 역할 멘션" if who == "봇" and mention else ""
                self.step("~", f"#{label} {who}: 권한 덮어쓰기 설정{extra}")
                if self.apply:
                    self.api.call("PUT", f"/channels/{cid}/permissions/{oid}",
                                  {"type": kind, "allow": str(new_allow), "deny": str(new_deny)})

    def _menu(self, channel_id: str, bot_id: str, saved: dict) -> str:
        self.out(f"\n[#{MENU_CHANNEL} 버튼 메시지]")
        want = menu_message()
        current = None
        if channel_id != NEW:
            saved_id = (saved.get("menu") or {}).get("message")
            if saved_id and (saved.get("menu") or {}).get("channel") == channel_id:
                try:
                    current = self.api.call("GET", f"/channels/{channel_id}/messages/{saved_id}")
                except NotFound:
                    current = None
            if current is None:
                current = next((m for m in self.api.call("GET", f"/channels/{channel_id}/messages?limit=50")
                                if _is_menu(m, bot_id)), None)
        if current is None:
            labels = " / ".join(t.label for t in TOPICS)
            self.step("+", f"버튼 메시지 올리기: {labels} / 내 알림 보기")
            return str(self.api.call("POST", f"/channels/{channel_id}/messages", want)["id"]) if self.apply else NEW
        if current.get("content") == want["content"] and _button_ids(current) == _button_ids(want):
            self.step("=", f"버튼 메시지 그대로 ({current['id']})")
        else:
            self.step("~", f"버튼 메시지 고치기 ({current['id']})")
            if self.apply:
                self.api.call("PATCH", f"/channels/{channel_id}/messages/{current['id']}", want)
        return str(current["id"])

    def _ssm(self, guild_id, role_ids, menu_channel, message_id, saved, channels_cfg, targets) -> None:
        self.out("\n[SSM]")
        new = {"guild": guild_id, "roles": role_ids, "menu": {"channel": menu_channel, "message": message_id}}
        if new == saved:
            self.step("=", f"{DISCORD_ROLES_PARAM} 그대로")
        else:
            self.step("~", f"{DISCORD_ROLES_PARAM} ← {json.dumps(new, ensure_ascii=False)}")
            if self.apply:
                self.ssm.put_parameter(Name=DISCORD_ROLES_PARAM, Type="String", Overwrite=True,
                                       Value=json.dumps(new, ensure_ascii=False))
        if self.department:
            dept_id = targets[DEPARTMENT_CHANNEL][0]
            if channels_cfg.get("department") == dept_id:
                self.step("=", f"{DISCORD_CHANNELS_PARAM} department 그대로")
            else:
                self.step("~", f"{DISCORD_CHANNELS_PARAM}에 department={dept_id} 추가 (학과 공지가 #학과-공지로 감)")
                if self.apply:
                    self.ssm.put_parameter(Name=DISCORD_CHANNELS_PARAM, Type="String", Overwrite=True,
                                           Value=json.dumps({**channels_cfg, "department": dept_id}))

    def _report_permissions(self, everyone: int, own: int) -> None:
        """everyone: the @everyone role's server permissions; own: the bot's own roles'."""
        self.out("\n[봇 권한]")
        perms = everyone | own
        if perms & ADMINISTRATOR:
            self.step("!", "봇에 관리자 권한이 있습니다. 서버 설정 → 역할 → 봇 역할에서 꺼 주세요.")
            return
        if missing := self.needed & ~perms:
            self.step("!", f"--apply 전에 봇 역할에 잠시 켜 둘 권한: {names(missing)}")
        if missing_runtime := BOT_RUNTIME & ~perms:
            self.step("!", f"버튼·알림에 계속 필요한 권한이 없습니다: {names(missing_runtime)}")
        if extra := own & ~BOT_RUNTIME & sum(PERMISSION_NAMES):
            self.step("=", f"설정이 끝나면 봇 역할에서 꺼도 되는 권한: {names(extra)}")
        self.step("=", f"봇 역할에 계속 둘 권한: {names(BOT_RUNTIME)} (값 {BOT_RUNTIME})")
        if everyone & MENTION_EVERYONE:
            self.step("!", "@everyone 역할에 '@everyone·모든 역할 멘션'이 켜져 있어 멤버 누구나 @everyone과 구독 역할을 "
                           "멘션할 수 있습니다. 서버 설정 → 역할 → @everyone에서 끄는 것을 권합니다(봇은 공지 채널에서 따로 허용됨).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제로 바꿉니다 (없으면 계획만 출력)")
    parser.add_argument("--department-channel", action="store_true", help="#학과-공지를 만들고 학과 공지를 그리로 보냅니다")
    parser.add_argument("--region", default=REGION)
    args = parser.parse_args(argv)

    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    ssm = boto3.client("ssm", region_name=args.region)
    try:
        token = _get_param(ssm, DISCORD_TOKEN_PARAM, decrypt=True)
    except (BotoCoreError, ClientError) as e:
        print(f"! SSM을 읽지 못했습니다 ({type(e).__name__}). `aws login` 후 다시 실행하세요.")
        return 2
    if problem := token_problem(token):
        print(f"! {DISCORD_TOKEN_PARAM}: {problem}")
        return 2
    try:
        return Setup(DiscordAPI(token), ssm, apply=args.apply, department=args.department_channel).run()
    except (RuntimeError, NotFound) as e:
        print(f"! 중단: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

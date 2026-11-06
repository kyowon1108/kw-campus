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
from kwcampus.discordapi import API, USER_AGENT, NotFound, clean_token, token_problem
from kwcampus.fetch import TLS
from kwcampus.interactions import PREFIX, menu_message
from kwcampus.topics import TOPICS

REGION = "ap-northeast-2"

KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
MANAGE_MESSAGES = 1 << 13
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
MENTION_EVERYONE = 1 << 17  # also "mention any role"; granted to the bot only inside notice channels
MANAGE_NICKNAMES = 1 << 27
MANAGE_ROLES = 1 << 28
MANAGE_WEBHOOKS = 1 << 29
MANAGE_GUILD_EXPRESSIONS = 1 << 30
CREATE_PUBLIC_THREADS = 1 << 35
CREATE_PRIVATE_THREADS = 1 << 36
SEND_MESSAGES_IN_THREADS = 1 << 38
MODERATE_MEMBERS = 1 << 40

PERMISSION_NAMES = {
    ADMINISTRATOR: "관리자(Administrator)",
    KICK_MEMBERS: "멤버 추방(Kick Members)",
    BAN_MEMBERS: "멤버 차단(Ban Members)",
    MANAGE_GUILD: "서버 관리(Manage Server)",
    MANAGE_CHANNELS: "채널 관리(Manage Channels)",
    MANAGE_MESSAGES: "메시지 관리(Manage Messages)",
    MANAGE_NICKNAMES: "별명 관리(Manage Nicknames)",
    MANAGE_WEBHOOKS: "웹후크 관리(Manage Webhooks)",
    MANAGE_GUILD_EXPRESSIONS: "이모지·스티커 관리(Manage Expressions)",
    MODERATE_MEMBERS: "멤버 타임아웃(Moderate Members)",
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
# What a stolen bot token must not be able to do; the bot should never hold these.
DANGEROUS = (ADMINISTRATOR | KICK_MEMBERS | BAN_MEMBERS | MANAGE_GUILD | MANAGE_MESSAGES | MANAGE_NICKNAMES
             | MANAGE_WEBHOOKS | MANAGE_GUILD_EXPRESSIONS | MODERATE_MEMBERS)

# Members read notice channels but never write in them. View is never forced: a channel or category
# hidden from @everyone (behind a verified role) stays hidden.
EVERYONE_ALLOW = READ_MESSAGE_HISTORY
EVERYONE_DENY = SEND_MESSAGES | CREATE_PUBLIC_THREADS | CREATE_PRIVATE_THREADS | SEND_MESSAGES_IN_THREADS
BOT_ALLOW = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY
# What the bot keeps at server level once setup is done (buttons need Manage Roles).
BOT_RUNTIME = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | READ_MESSAGE_HISTORY | MANAGE_ROLES
ONLY_MENTIONS = 1  # guild default_message_notifications: 0 = all messages, 1 = only @mentions

NOTICE_CHANNEL_KEYS = ("notice", "academic", "scholarship", "career", "international")
MENU_CHANNEL = "알림-설정"
MENU_TOPIC = "버튼을 눌러 받고 싶은 공지 알림을 고르세요. 이 채널에는 봇만 글을 씁니다."
DEPARTMENT_CHANNEL = "학과-공지"
DEPARTMENT_TOPIC = "단과대·학과 게시판과 학과 블로그 공지. 봇만 글을 씁니다."
TEXT_CHANNEL = 0
NEW = "<새로 만듦>"


def names(bits: int) -> str:
    known = [name for bit, name in PERMISSION_NAMES.items() if bits & bit]
    if unknown := bits & ~sum(PERMISSION_NAMES):
        known.append(f"기타 권한 비트 {hex(unknown)}")
    return ", ".join(known) or "없음"


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
            except ValueError:  # http.client quotes a bad header value, token included
                raise RuntimeError(f"invalid request or reply on {method} {path}") from None
        raise RuntimeError(f"Discord kept rate-limiting {method} {path}")


def _get_param(ssm, name: str, decrypt: bool = False) -> str | None:
    try:
        return ssm.get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None


def _rank(role: dict) -> tuple[int, int]:
    """Discord's role order: a higher position is above; at the same position the older role (smaller id) is.
    New roles can land at the bot role's own position, which still leaves them below it."""
    return int(role.get("position", 0)), -int(role["id"])


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
        self.ssm_failed = False

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
        top = max((_rank(r) for r in bot_roles), default=(0, 0))
        mode = "적용(--apply)" if self.apply else "계획만 보기(dry-run)"
        self.out(f"Discord 서버: {guild.get('name')} ({guild_id}) · 봇: {me.get('username')} ({bot_id}) · {mode}")

        if (everyone | own) & ADMINISTRATOR and self.apply:
            self._report_permissions(everyone, own, guild)
            self.out("\n중단: 관리자 권한이 있는 봇으로는 아무것도 바꾸지 않습니다. 봇 역할에서 관리자를 끄고 다시 실행하세요.")
            return 2
        role_ids = self._roles(guild_id, roles, saved, top)
        if role_ids is None:
            self.out("\n중단: 위 역할 문제를 고치기 전에는 아무것도 바꾸지 않습니다.")
            return 1
        by_id = {str(c["id"]): c for c in channels}
        targets, menu_channel, dept_channel = self._channels(guild_id, bot_id, notice, channels_cfg, saved, by_id,
                                                             channels)
        self._permissions(guild_id, bot_id, targets, by_id)
        # The role map goes in before the buttons change, so no button points at a role the Lambda can't see.
        self.out("\n[SSM]")
        known_message = (saved.get("menu") or {}).get("message") if (saved.get("menu") or {}).get(
            "channel") == menu_channel else None
        roles_value = {**saved, "guild": guild_id, "roles": role_ids,
                       "menu": {"channel": menu_channel, "message": known_message}}
        self._put_if_changed(DISCORD_ROLES_PARAM, saved, roles_value, quiet_if_same=False)
        message_id = self._menu(menu_channel, bot_id, saved)
        if message_id != known_message:
            final = {**roles_value, "menu": {"channel": menu_channel, "message": message_id}}
            self._put_if_changed(DISCORD_ROLES_PARAM, roles_value, final, label="(버튼 메시지 ID)")
        if self.department:
            if channels_cfg.get("department") == dept_channel:
                self.step("=", f"{DISCORD_CHANNELS_PARAM} department 그대로")
            else:
                self._put_if_changed(DISCORD_CHANNELS_PARAM, channels_cfg, {**channels_cfg, "department": dept_channel},
                                     label="(학과 공지가 #학과-공지로 감)")
        self._report_permissions(everyone, own, guild)

        if self.ssm_failed:
            self.out("\n일부 SSM 값을 쓰지 못했습니다. 위의 aws 명령을 직접 실행해 주세요.")
            return 1
        if self.apply:
            self.out(f"\n완료: {self.changes}건 바꿨습니다." if self.changes else "\n바꿀 것이 없습니다.")
        else:
            self.out(f"\ndry-run: 아무것도 바꾸지 않았습니다. 바뀔 항목 {self.changes}건. 적용하려면 --apply")
        return 0

    def _put_if_changed(self, name: str, before: dict, after: dict, label: str = "", quiet_if_same: bool = True):
        if after == before:
            if not quiet_if_same:
                self.step("=", f"{name} 그대로")
            return
        value = json.dumps(after, ensure_ascii=False)
        self.step("~", f"{name} {label} ← {value}".replace("  ", " "))
        if not self.apply:
            return
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.ssm.put_parameter(Name=name, Type="String", Overwrite=True, Value=value)
        except (BotoCoreError, ClientError) as e:
            self.ssm_failed = True  # no secret in this value, so the command can be shown
            self.step("!", f"{name}을 쓰지 못했습니다({type(e).__name__}). SSM 쓰기 권한이 있는 자격 증명으로 실행하세요:")
            self.out(f"    aws ssm put-parameter --region {REGION} --name {name} --type String --overwrite "
                     f"--value '{value}'")

    def _roles(self, guild_id: str, roles: list[dict], saved: dict, top: tuple[int, int]) -> dict[str, str] | None:
        """Topic → role id. None (and nothing written) when an existing role is unsafe to hand out:
        anyone who clicks its button would get it."""
        self.out("\n[구독 역할]")
        by_id = {str(r["id"]): r for r in roles}
        by_name = {r["name"]: r for r in roles}
        found = {t.key: by_id.get(str((saved.get("roles") or {}).get(t.key))) or by_name.get(t.role) for t in TOPICS}
        unsafe = [(t, r) for t in TOPICS if (r := found[t.key]) and (
            int(r.get("permissions", 0)) or r.get("managed") or str(r["id"]) == guild_id
            or _rank(r) >= top)]
        for topic, role in unsafe:
            self.step("!", f"'{role['name']}' ({role['id']})은 버튼으로 나눠 줄 수 없습니다: 권한이 있거나, 연동 역할이거나, "
                           f"봇 역할보다 위에 있습니다. 권한을 비우고 봇 역할 아래로 내리거나 이름을 바꾼 뒤 다시 실행하세요.")
        if unsafe:
            return None
        ids = {}
        for topic in TOPICS:
            role = found[topic.key]
            if role is None:
                self.step("+", f"'{topic.role}' 역할 만들기 (권한 없음, 멤버가 멘션할 수 없음)")
                if self.apply:
                    role = self.api.call("POST", f"/guilds/{guild_id}/roles", {
                        "name": topic.role, "permissions": "0", "mentionable": False, "hoist": False})
                ids[topic.key] = str(role["id"]) if role else NEW
                continue
            ids[topic.key] = str(role["id"])
            fix, what = {}, []
            if role["name"] != topic.role:
                # a renamed college (2028: 참빛인재대학 → 산업융합대학) is one line in topics.TOPICS
                fix["name"] = topic.role
                what.append(f"'{role['name']}' → '{topic.role}' 이름 바꾸기")
            if role.get("mentionable"):
                # a mentionable role lets any member ping every subscriber
                fix["mentionable"] = False
                what.append(f"'{role['name']}' 멤버 멘션 끄기")
            if fix:
                self.step("~", f"{', '.join(what)} ({role['id']})")
                if self.apply:
                    self.api.call("PATCH", f"/guilds/{guild_id}/roles/{role['id']}", fix)
            else:
                self.step("=", f"'{role['name']}' 있음 ({role['id']})")
        return ids

    def _new_overwrites(self, parent: dict | None, guild_id: str, bot_id: str, mention: bool) -> list[dict]:
        """A new channel starts from its category's overwrites (a verified-role gate stays), plus ours."""
        base = {str(o["id"]): o for o in (parent or {}).get("permission_overwrites") or []}
        out = []
        for oid, kind, allow, deny, _who in _overwrites_for(guild_id, bot_id, mention):
            new_allow, new_deny = _merge(base.pop(oid, None), allow, deny)
            out.append({"id": oid, "type": kind, "allow": str(new_allow), "deny": str(new_deny)})
        out += [{"id": str(o["id"]), "type": o["type"], "allow": str(o["allow"]), "deny": str(o["deny"])}
                for o in base.values()]
        for o in out:
            self.needed |= int(o["allow"]) | int(o["deny"])
        return out

    def _find_or_create(self, guild_id: str, bot_id: str, notice: dict, name: str, topic: str,
                        known_id: str | None, by_id: dict, channels: list[dict], mention: bool) -> tuple[str, bool]:
        parent_id = notice.get("parent_id")
        # by its saved id, else by name next to #학교-공지 only (never adopt a same-named channel elsewhere)
        channel = by_id.get(str(known_id)) or next(
            (c for c in channels if c.get("name") == name and c.get("type") == TEXT_CHANNEL
             and c.get("parent_id") == parent_id), None)
        if channel:
            self.step("=", f"#{channel['name']} 있음 ({channel['id']})")
            if channel.get("topic") != topic:
                self.needed |= MANAGE_CHANNELS
                self.step("~", f"#{channel['name']} 채널 주제 고치기: {topic}")
                if self.apply:
                    self.api.call("PATCH", f"/channels/{channel['id']}", {"topic": topic})
            return str(channel["id"]), False
        self.needed |= MANAGE_CHANNELS
        overwrites = self._new_overwrites(by_id.get(str(parent_id)), guild_id, bot_id, mention)
        self.step("+", f"#{name} 만들기 (#{notice.get('name')} 채널과 같은 분류, 그 분류의 권한 + 읽기 전용)")
        if not self.apply:
            return NEW, True
        made = self.api.call("POST", f"/guilds/{guild_id}/channels", {
            "name": name, "type": TEXT_CHANNEL, "topic": topic, "parent_id": parent_id,
            "permission_overwrites": overwrites})
        by_id[str(made["id"])] = made
        return str(made["id"]), True

    def _channels(self, guild_id, bot_id, notice, channels_cfg, saved, by_id, channels) -> tuple:
        """([(label, channel id, mention allowed, created now)], menu channel id, department channel id)."""
        self.out("\n[채널]")
        targets, dept_id = [], None
        for key in NOTICE_CHANNEL_KEYS:
            cid = str(channels_cfg.get(key) or "")
            if cid not in by_id:
                self.step("!", f"{DISCORD_CHANNELS_PARAM}의 '{key}' 채널을 서버에서 찾지 못했습니다. 건너뜁니다.")
                continue
            self.step("=", f"#{by_id[cid]['name']} ({key})")
            targets.append((by_id[cid]["name"], cid, True, False))
        menu_id, created = self._find_or_create(guild_id, bot_id, notice, MENU_CHANNEL, MENU_TOPIC,
                                                (saved.get("menu") or {}).get("channel"), by_id, channels, False)
        targets.append((MENU_CHANNEL, menu_id, False, created))
        configured = str(channels_cfg.get("department") or "")
        if self.department:
            dept_id, created = self._find_or_create(guild_id, bot_id, notice, DEPARTMENT_CHANNEL, DEPARTMENT_TOPIC,
                                                    configured or None, by_id, channels, True)
            targets.append((DEPARTMENT_CHANNEL, dept_id, True, created))
        elif configured in by_id:
            # set by hand: still read-only, and the bot may mention college roles there
            dept_id = configured
            self.step("=", f"#{by_id[configured]['name']} (department)")
            targets.append((by_id[configured]["name"], configured, True, False))
        elif configured:
            self.step("!", f"{DISCORD_CHANNELS_PARAM}의 'department' 채널({configured})을 서버에서 찾지 못했습니다. "
                           "학과 공지가 가지 않습니다. --department-channel로 다시 만들 수 있습니다.")
        else:
            self.step("=", "학과 공지는 계속 #학교-공지로 갑니다 (--department-channel로 #학과-공지를 만들 수 있음)")
        return targets, menu_id, dept_id

    def _permissions(self, guild_id: str, bot_id: str, targets: list, by_id: dict) -> None:
        self.out("\n[채널 권한] @everyone: 기록 보기 허용, 메시지·스레드 거부(보기는 건드리지 않음) / "
                 "봇: 보기·보내기·링크 첨부·기록 보기 허용")
        for label, cid, mention, created in targets:
            if created:
                continue  # made with the right overwrites just now (or in this plan)
            existing = {str(o["id"]): o for o in (by_id.get(cid) or {}).get("permission_overwrites") or []}
            for oid, kind, allow, deny, who in _overwrites_for(guild_id, bot_id, mention):
                old = existing.get(oid)
                if who == "@everyone" and old and int(old["deny"]) & VIEW_CHANNEL:
                    self.step("=", f"#{label}: @everyone에게 숨겨진 채널입니다(그대로 둠).")
                if who == "봇" and old and (risky := int(old["allow"]) & DANGEROUS & ~allow):
                    self.step("!", f"#{label}의 봇 덮어쓰기에 필요 없는 권한이 있습니다: {names(risky)}. 채널 설정에서 빼 주세요.")
                new_allow, new_deny = _merge(old, allow, deny)
                if old and (int(old["allow"]), int(old["deny"])) == (new_allow, new_deny):
                    self.step("=", f"#{label} {who}: 이미 맞음")
                    continue
                # Discord may check every bit sent, so the bits kept from before count too
                self.needed |= new_allow | new_deny
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

    def _report_permissions(self, everyone: int, own: int, guild: dict) -> None:
        """everyone: the @everyone role's server permissions; own: the bot's own roles'."""
        self.out("\n[봇 권한]")
        perms = everyone | own
        if perms & ADMINISTRATOR:
            self.step("!", "봇에 관리자 권한이 있습니다. 서버 설정 → 역할 → 봇 역할에서 꺼 주세요. --apply는 그 전까지 멈춥니다.")
        if risky := own & DANGEROUS & ~ADMINISTRATOR:
            self.step("!", f"봇 역할에 필요 없는 위험한 권한이 있습니다: {names(risky)}. 꺼 주세요.")
        if missing := self.needed & ~perms:
            self.step("!", f"--apply 전에 봇 역할에 잠시 켜 둘 권한: {names(missing)}")
        if missing_runtime := BOT_RUNTIME & ~perms:
            self.step("!", f"버튼·알림에 계속 필요한 권한이 없습니다: {names(missing_runtime)}")
        if extra := own & ~BOT_RUNTIME & ~DANGEROUS:
            self.step("=", f"설정이 끝나면 봇 역할에서 꺼도 되는 권한: {names(extra)}")
        self.step("=", f"봇 역할에 계속 둘 권한: {names(BOT_RUNTIME)} (값 {BOT_RUNTIME})")
        if everyone & MENTION_EVERYONE:
            self.step("!", "@everyone 역할에 '@everyone·모든 역할 멘션'이 켜져 있어 멤버 누구나 @everyone과 구독 역할을 "
                           "멘션할 수 있습니다. --apply 뒤에 서버 설정 → 역할 → @everyone에서 끄는 것을 권합니다"
                           "(봇은 공지 채널에서 따로 허용됨).")
        if int(guild.get("default_message_notifications", 0)) != ONLY_MENTIONS:
            self.step("!", "서버 기본 알림이 '모든 메시지'입니다. 그대로면 멤버가 모든 공지와 수정 답글에 알림을 받습니다. "
                           "서버 설정 → 개요 → 기본 알림 설정을 '@멘션만'으로 바꿔 주세요.")


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
        token = clean_token(_get_param(ssm, DISCORD_TOKEN_PARAM, decrypt=True))
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
    except (BotoCoreError, ClientError) as e:
        print(f"! 중단: SSM 오류 ({type(e).__name__}). 자격 증명과 권한을 확인하세요.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

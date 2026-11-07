"""Lambda behind POST /discord/interactions: the subscription buttons in #알림-설정, 제보 (the 제보하기
button or the /제보 command opens a form; what is sent goes to the operators' channel), and 설치 (the
설치하기 button in #설치-안내 or the /설치 command gives the member an invite of their own).

Discord signs every interaction with the app's Ed25519 key. A request that fails the check gets
401 (Discord itself sends a few of those when the endpoint URL is saved in the Developer Portal).
PING gets PONG. A button click adds or removes exactly the role that /kw/discord/roles maps to the
button's topic, never a role id taken from the request, and the answer is visible only to the
person who clicked.

This function reads the bot token, the public key and the role map, and writes invites to the
kw-bridge table (env KW_BRIDGE_TABLE; invites.py keeps only their hashes). An invite is shown once,
to the member who asked, and never logged. A report is kept nowhere but in the operators' Discord
channel.
"""

from __future__ import annotations

import base64
import json
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kwcampus import invites, params
from kwcampus.config import (
    DISCORD_PUBLIC_KEY_PARAM,
    DISCORD_ROLES_PARAM,
    DISCORD_TOKEN_PARAM,
)
from kwcampus.discordapi import (
    NotFound,
    RetryLater,
    clean_token,
    is_snowflake,
    token_problem,
)
from kwcampus.discordapi import call as _discord
from kwcampus.obs import log, metrics
from kwcampus.topics import TOPIC_BY_KEY, TOPICS

PING, APPLICATION_COMMAND, MESSAGE_COMPONENT, MODAL_SUBMIT = 1, 2, 3, 5
PONG, CHANNEL_MESSAGE, MODAL = 1, 4, 9
EPHEMERAL = 1 << 6
PREFIX = "kwsub:"  # button custom_id = PREFIX + topic key
LIST_ID = "kwsub:list"
REPORT_OPEN = "kwreport:open"  # the 제보하기 button
REPORT_SUBMIT = "kwreport:submit"  # the form it opens
REPORT_COMMAND = "제보"  # the guild slash command (scripts/discord_setup.py registers it)
REPORT_FIELDS = (("what", "무엇이 틀렸거나 바뀌었나요?", 2, 5, 1000, True,
                  "예: 마이크로전공 이수학점이 바뀌었어요, 이 공지의 마감일이 달라요"),
                 ("source", "근거가 되는 링크나 규정 (선택)", 1, 0, 300, False, "예: 학교 공지 주소, 학칙 조항 이름"),
                 ("where", "어디서 봤나요? (선택)", 1, 0, 100, False, "예: Claude 답변, 디스코드 공지"))
SETUP_START = "kwsetup:start"  # the 설치하기 button in #설치-안내
SETUP_COMMAND = "설치"  # the guild slash command beside /제보 (scripts/discord_setup.py registers it)
START_PATH = "/start"  # the bridge's install page; an invite rides in its #fragment, never sent to a server
MAX_BODY = 64 * 1024
MAX_SKEW_S = 60  # a signed request older than this is a replay
ROLE_TIMEOUT_S = 2.0  # Discord wants the answer within 3 s
# The invite is two small DynamoDB writes; within Discord's 3 s, so no 60-second default timeouts.
BRIDGE_DB = Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 2, "mode": "standard"})

# Keep in step with the collector schedule (infra: weekdays 09:00-18:40 every 20 min, plus 21:00).
MENU_CONTENT = (
    "## 🔔 공지 알림 받기\n"
    "주제 버튼을 누르면 새 공지가 올라올 때 멘션해 드려요. 한 번 더 누르면 꺼져요.\n"
    "\n"
    "- **긴급**: 휴강·휴교·정전·시스템 장애\n"
    "- **학사 · 장학 · 취업 · 국제 · 일반**: 학교 전체 공지\n"
    "- **회색 버튼**: 단과대·학과 게시판 공지\n"
    "\n"
    "-# 평일 09:00~18:40 20분마다, 21:00에 한 번 확인해요. 틀린 정보는 **제보하기**로 알려 주세요."
)

INSTALL_CONTENT = (
    "## 💻 KW CAMPUS 설치하기\n"
    "Claude에게 내 과제·공지·학식을 물어볼 수 있게 해 줘요.\n"
    "\n"
    "**1.** **설치하기**를 눌러 내 초대 코드 받기\n"
    "**2.** 설치 페이지에서 `kw-campus.mcpb` 받아 더블클릭\n"
    "**3.** Claude 데스크톱에 학번·비밀번호·초대 코드 넣기\n"
    "\n"
    "-# 학번·비밀번호는 내 PC에만 저장돼요."
)
SETUP_LATER = "지금은 초대 코드를 못 만들어요. 잠시 뒤에 다시 눌러 주세요."
SETUP_DAY_FULL = "오늘은 초대 코드가 다 나갔어요. 내일 다시 눌러 주세요."
SORRY_LATER = "지금은 안 돼요. 잠시 뒤에 다시 해 주세요."


def _rows(buttons: list[dict]) -> list[dict]:
    return [{"type": 1, "components": buttons[i : i + 5]} for i in range(0, len(buttons), 5)]


def menu_message() -> dict:
    """The one bot message in #알림-설정 (scripts/discord_setup.py posts or updates it).
    Topics first, then the colleges in rows of their own; Discord allows 5 rows of 5 buttons."""
    def button(t, style: int) -> dict:
        return {"type": 2, "style": style, "label": t.label, "custom_id": PREFIX + t.key}

    topics = [button(t, 1) for t in TOPICS if not t.college]
    topics.append({"type": 2, "style": 3, "label": "내 알림 보기", "custom_id": LIST_ID})
    topics.append({"type": 2, "style": 2, "label": "제보하기", "custom_id": REPORT_OPEN, "emoji": {"name": "📝"}})
    rows = _rows(topics) + _rows([button(t, 2) for t in TOPICS if t.college])
    return {"content": MENU_CONTENT, "components": rows, "allowed_mentions": {"parse": []}}


def _open_page(url: str) -> dict:
    return {"type": 2, "style": 5, "label": "설치 페이지 열기", "url": url}


def install_message(bridge_url: str) -> dict:
    """The one bot message in #설치-안내 (scripts/discord_setup.py posts or updates it): the 설치하기
    button, and the install page itself for anyone who wants to look first."""
    buttons = [{"type": 2, "style": 1, "label": "설치하기", "custom_id": SETUP_START},
               _open_page(bridge_url.rstrip("/") + START_PATH)]
    return {"content": INSTALL_CONTENT, "components": _rows(buttons), "allowed_mentions": {"parse": []}}


def verify(public_key_hex: str | None, signature_hex: str | None, timestamp: str | None,
           body: bytes, now: float) -> bool:
    if not (public_key_hex and signature_hex and timestamp):
        return False
    # Unix seconds; a 400-digit "timestamp" must not reach int() and float arithmetic
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
        return False
    try:
        if abs(now - int(timestamp)) > MAX_SKEW_S:
            return False
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
    except (ValueError, OverflowError, InvalidSignature):
        return False
    return True


def _say(text: str) -> dict:
    return {"type": CHANNEL_MESSAGE, "data": {"content": text, "flags": EPHEMERAL, "allowed_mentions": {"parse": []}}}


def report_form() -> dict:
    """The 제보 form: what is wrong (required), the source and where it was seen (optional)."""
    rows = [{"type": 1, "components": [{"type": 4, "custom_id": cid, "label": label, "style": style,
                                        "min_length": low, "max_length": high, "required": required,
                                        "placeholder": hint}]}
            for cid, label, style, low, high, required, hint in REPORT_FIELDS]
    return {"type": MODAL, "data": {"custom_id": REPORT_SUBMIT, "title": "틀린 정보 제보하기", "components": rows}}


def _form_values(data: dict) -> dict[str, str]:
    values = {}
    for row in data.get("components") or []:
        for field in (row or {}).get("components") or []:
            if isinstance(field, dict) and isinstance(field.get("custom_id"), str):
                values[field["custom_id"]] = str(field.get("value") or "").strip()
    return values


def _quote(text: str, limit: int) -> str:
    """Report text as a quote block: a reporter's code fence or heading can't take over the post."""
    text = text.replace("`" * 3, "'" * 3)[:limit]
    return "\n".join("> " + line for line in text.splitlines()) or "> (없음)"


def _report(cfg: dict, user: str, data: dict, token: str | None, send) -> tuple[dict, str]:
    channel = str((cfg.get("report") or {}).get("channel") or "")
    if not is_snowflake(channel):
        return _say("제보 받을 채널이 아직 준비 중이라 못 보냈어요. 잠시 뒤에 다시 보내 주세요."), "report_not_ready"
    if token_problem(token):
        return _say("지금은 제보를 못 보냈어요. 잠시 뒤에 다시 보내 주세요."), "no_token"
    values = _form_values(data)
    what = values.get("what") or ""
    if len(what) < 5:
        return _say("내용이 너무 짧아요. 무엇이 틀렸는지 조금 더 적어서 다시 보내 주세요."), "report_too_short"
    lines = [f"📝 **새 제보** · <@{user}>", "**내용**", _quote(what, 1000)]
    if values.get("source"):
        lines += ["**근거**", _quote(values["source"], 300)]
    if values.get("where"):
        lines += ["**어디서**", _quote(values["where"], 100)]
    body = {"content": "\n".join(lines)[:1900], "allowed_mentions": {"parse": []}}
    try:
        send("POST", f"/channels/{channel}/messages", token, body, timeout=ROLE_TIMEOUT_S)
    except (RetryLater, NotFound, RuntimeError) as e:
        log("report_failed", error=type(e).__name__, detail=str(e)[:80])
        return _say("지금은 제보를 못 보냈어요. 잠시 뒤에 다시 보내 주세요."), "report_failed"
    return _say("제보 고마워요! 운영자가 확인해 볼게요."), "reported"


def _setup(user: str) -> tuple[dict, str]:
    """설치하기 / /설치: a new invite for this member, and the install page with the invite in its
    #fragment (a browser keeps the fragment to itself, so the code reaches no server log). invites.issue
    stops at MAX_PER_MEMBER per member and DAILY_MAX a day for everyone. The invite goes into this one
    reply and nowhere else, logs included."""
    table_name = os.environ.get("KW_BRIDGE_TABLE", "")
    base = os.environ.get("KW_BRIDGE_BASE_URL", "").rstrip("/")
    if not table_name or not base.startswith("https://"):
        log("setup_not_configured", table=bool(table_name), base_url=bool(base))
        return _say(SETUP_LATER), "setup_not_ready"
    try:
        invite = invites.issue(boto3.resource("dynamodb", config=BRIDGE_DB).Table(table_name), user, base)
    except invites.TooMany:
        return _say(f"초대 코드는 {invites.MAX_PER_MEMBER}개까지 받을 수 있어요. 더 필요하면 운영자에게 말해 주세요."), \
            "setup_too_many"
    except invites.DayFull:
        return _say(SETUP_DAY_FULL), "setup_day_full"
    except ClientError as e:
        log("setup_failed", error=str((e.response.get("Error") or {}).get("Code") or "ClientError")[:60])
        return _say(SETUP_LATER), "setup_failed"
    except (BotoCoreError, ValueError) as e:
        log("setup_failed", error=type(e).__name__)
        return _say(SETUP_LATER), "setup_failed"
    reply = _say(
        "🔑 **내 초대 코드** (지금만 보여요)\n"
        f"```\n{invite}\n```\n"
        "**설치 페이지 열기**를 누르면 이어서 안내해 드려요.")
    reply["data"]["components"] = _rows([_open_page(f"{base}{START_PATH}#{invite}")])
    return reply, "setup_issued"


def handle(payload: dict, cfg: dict, token: str | None, send=_discord, reload=None) -> tuple[dict, str]:
    """(interaction response, outcome for the log). reload() re-reads the role map, bypassing the
    cache, for a known topic whose role isn't in the cached map yet (setup ran a moment ago)."""
    kind = payload.get("type")
    if kind == PING:
        return {"type": PONG}, "pong"
    if kind not in (APPLICATION_COMMAND, MESSAGE_COMPONENT, MODAL_SUBMIT):
        return _say("이 요청은 처리하지 못했어요. 버튼을 다시 눌러 주세요."), "unsupported"

    guild = str(payload.get("guild_id") or "")
    member = payload.get("member") or {}
    user = str((member.get("user") or {}).get("id") or "")
    if not cfg.get("guild") or guild != str(cfg["guild"]) or not is_snowflake(user):
        return _say("공지 알림 서버 안에서만 돼요. 서버 채널에서 다시 해 주세요."), "wrong_guild"
    data = payload.get("data") or {}
    if kind == APPLICATION_COMMAND:
        if data.get("name") == REPORT_COMMAND:
            return report_form(), "report_form"
        if data.get("name") == SETUP_COMMAND:
            return _setup(user)
        return _say(f"지금은 쓰지 않는 명령이에요. `/{SETUP_COMMAND}`나 `/{REPORT_COMMAND}`를 써 주세요."), "unsupported"
    if kind == MODAL_SUBMIT:
        if data.get("custom_id") == REPORT_SUBMIT:
            return _report(cfg, user, data, token, send)
        return _say("지금은 쓰지 않는 입력 창이에요. **제보하기**를 다시 눌러 주세요."), "unsupported"
    roles = cfg.get("roles") or {}
    have = {str(r) for r in member.get("roles") or []}
    custom_id = str(data.get("custom_id") or "")

    if custom_id == REPORT_OPEN:
        return report_form(), "report_form"

    if custom_id == SETUP_START:
        return _setup(user)

    if custom_id == LIST_ID:
        mine = [t.label for t in TOPICS if str(roles.get(t.key) or "") in have]
        text = (f"지금 받는 알림: {', '.join(mine)}\n-# 끄려면 그 주제 버튼을 한 번 더 누르면 돼요." if mine
                else "아직 켠 알림이 없어요. 위에서 받고 싶은 주제 버튼을 눌러 주세요.")
        return _say(text), "listed"

    topic = TOPIC_BY_KEY.get(custom_id.removeprefix(PREFIX)) if custom_id.startswith(PREFIX) else None
    if topic is None:
        return _say("지금은 쓰지 않는 버튼이에요. 다른 버튼을 눌러 주세요."), "unknown_button"
    role_id = str(roles.get(topic.key) or "")
    if not is_snowflake(role_id) and reload is not None:
        roles = (reload() or {}).get("roles") or {}
        role_id = str(roles.get(topic.key) or "")
    if not is_snowflake(role_id) or role_id == guild:  # never @everyone
        return _say(f"**{topic.label}** 알림은 아직 준비 중이에요. 잠시 뒤에 다시 눌러 주세요."), "not_ready"
    if token_problem(token):
        return _say("지금은 알림을 못 바꿨어요. 잠시 뒤에 다시 눌러 주세요."), "no_token"

    turn_on = role_id not in have
    try:
        send("PUT" if turn_on else "DELETE", f"/guilds/{guild}/members/{user}/roles/{role_id}", token,
             None, timeout=ROLE_TIMEOUT_S)
    except (RetryLater, NotFound, RuntimeError) as e:
        log("interaction_role_failed", topic=topic.key, error=type(e).__name__, detail=str(e)[:80])
        return _say("지금은 알림을 못 바꿨어요. 잠시 뒤에 다시 눌러 주세요."), "role_failed"
    if turn_on:
        return _say(f"**{topic.label}** 알림을 켰어요. 새 공지가 올라오면 멘션으로 알려 드릴게요."), "on"
    return _say(f"**{topic.label}** 알림을 껐어요. 다시 받으려면 버튼을 한 번 더 눌러 주세요."), "off"


def _http(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def handler(event, context):
    body = event.get("body") or ""
    if len(body) > MAX_BODY:
        return _http(413, {"error": "too large"})
    try:
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    except ValueError:
        return _http(400, {"error": "bad body"})
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    # one SSM call: the 3-second answer window includes a cold start
    values = params.get_many([DISCORD_PUBLIC_KEY_PARAM, DISCORD_ROLES_PARAM, DISCORD_TOKEN_PARAM], decrypt=True)
    public_key = values[DISCORD_PUBLIC_KEY_PARAM]
    if not verify(public_key, headers.get("x-signature-ed25519"), headers.get("x-signature-timestamp"),
                  raw, time.time()):
        log("interaction_rejected", reason="no_public_key" if not public_key else "bad_signature")
        metrics(InteractionRejected=1)
        return _http(401, {"error": "invalid request signature"})
    try:
        payload = json.loads(raw)
    except ValueError:
        return _http(400, {"error": "bad json"})

    try:
        cfg = json.loads(values[DISCORD_ROLES_PARAM] or "{}")
        response, outcome = handle(payload, cfg, clean_token(values[DISCORD_TOKEN_PARAM]),
                                   reload=lambda: json.loads(params.get(DISCORD_ROLES_PARAM, ttl_s=0) or "{}"))
    except Exception as e:  # noqa: BLE001 - a signed click always gets a polite answer, never a 500
        log("interaction_error", error=type(e).__name__)
        response, outcome = _say(SORRY_LATER), "error"
    log("interaction", outcome=outcome)
    failed = outcome in ("role_failed", "no_token", "not_ready", "error", "report_failed", "report_not_ready",
                         "setup_not_ready", "setup_failed")
    metrics(InteractionOk=0 if failed else 1, InteractionFailed=1 if failed else 0)
    return _http(200, response)

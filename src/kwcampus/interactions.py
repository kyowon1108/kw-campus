"""Lambda behind POST /discord/interactions: the subscription buttons in #알림-설정.

Discord signs every interaction with the app's Ed25519 key. A request that fails the check gets
401 (Discord itself sends a few of those when the endpoint URL is saved in the Developer Portal).
PING gets PONG. A button click adds or removes exactly the role that /kw/discord/roles maps to the
button's topic, never a role id taken from the request, and the answer is visible only to the
person who clicked.

This function only reads the bot token, the public key and the role map; it has no table access.
"""

from __future__ import annotations

import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kwcampus import params
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

PING, MESSAGE_COMPONENT = 1, 3
PONG, CHANNEL_MESSAGE = 1, 4
EPHEMERAL = 1 << 6
PREFIX = "kwsub:"  # button custom_id = PREFIX + topic key
LIST_ID = "kwsub:list"
MAX_BODY = 64 * 1024
MAX_SKEW_S = 60  # a signed request older than this is a replay
ROLE_TIMEOUT_S = 2.0  # Discord wants the answer within 3 s

# Keep in step with the collector schedule (infra: weekdays 09:00-18:40 every 20 min, plus 21:00).
MENU_CONTENT = (
    "## 🔔 공지 알림 받기\n"
    "받고 싶은 **주제 버튼**을 누르세요. 그 주제의 새 공지가 올라오면 멘션으로 알려 드립니다.\n"
    "-# 한 번 더 누르면 꺼집니다. 켜 둔 주제는 **내 알림 보기**에서 확인하세요.\n"
    "\n"
    "**버튼 안내**\n"
    "- **긴급**: 휴강·휴교·출입 통제·정전·단수·시스템 장애\n"
    "- **학사 · 장학 · 취업 · 국제 · 일반**: 학교 전체 공지\n"
    "- **회색 버튼**: 단과대와 소속 학과 게시판 공지\n"
    "\n"
    "**알림 시간**\n"
    "- 평일 09:00~18:40 (20분마다), 21:00\n"
    "- 밤·주말에 올라온 글은 다음 확인 때 올라옵니다\n"
    "- 같은 주제 멘션은 30분에 한 번 (긴급은 매번)"
)


def _rows(buttons: list[dict]) -> list[dict]:
    return [{"type": 1, "components": buttons[i : i + 5]} for i in range(0, len(buttons), 5)]


def menu_message() -> dict:
    """The one bot message in #알림-설정 (scripts/discord_setup.py posts or updates it).
    Topics first, then the colleges in rows of their own; Discord allows 5 rows of 5 buttons."""
    def button(t, style: int) -> dict:
        return {"type": 2, "style": style, "label": t.label, "custom_id": PREFIX + t.key}

    topics = [button(t, 1) for t in TOPICS if not t.college]
    topics.append({"type": 2, "style": 3, "label": "내 알림 보기", "custom_id": LIST_ID})
    rows = _rows(topics) + _rows([button(t, 2) for t in TOPICS if t.college])
    return {"content": MENU_CONTENT, "components": rows, "allowed_mentions": {"parse": []}}


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


def handle(payload: dict, cfg: dict, token: str | None, send=_discord, reload=None) -> tuple[dict, str]:
    """(interaction response, outcome for the log). reload() re-reads the role map, bypassing the
    cache, for a known topic whose role isn't in the cached map yet (setup ran a moment ago)."""
    if payload.get("type") == PING:
        return {"type": PONG}, "pong"
    if payload.get("type") != MESSAGE_COMPONENT:
        return _say("지원하지 않는 요청입니다."), "unsupported"

    guild = str(payload.get("guild_id") or "")
    member = payload.get("member") or {}
    user = str((member.get("user") or {}).get("id") or "")
    if not cfg.get("guild") or guild != str(cfg["guild"]) or not is_snowflake(user):
        return _say("이 버튼은 공지 서버 안에서만 쓸 수 있습니다."), "wrong_guild"
    roles = cfg.get("roles") or {}
    have = {str(r) for r in member.get("roles") or []}
    custom_id = str((payload.get("data") or {}).get("custom_id") or "")

    if custom_id == LIST_ID:
        mine = [t.label for t in TOPICS if str(roles.get(t.key) or "") in have]
        text = f"지금 받는 알림: {', '.join(mine)}" if mine else "아직 받는 알림이 없습니다. 위 버튼에서 골라 주세요."
        return _say(text), "listed"

    topic = TOPIC_BY_KEY.get(custom_id.removeprefix(PREFIX)) if custom_id.startswith(PREFIX) else None
    if topic is None:
        return _say("이 버튼은 이제 쓰지 않습니다. 다른 버튼을 눌러 주세요."), "unknown_button"
    role_id = str(roles.get(topic.key) or "")
    if not is_snowflake(role_id) and reload is not None:
        roles = (reload() or {}).get("roles") or {}
        role_id = str(roles.get(topic.key) or "")
    if not is_snowflake(role_id) or role_id == guild:  # never @everyone
        return _say("아직 준비 중인 버튼입니다. 잠시 뒤 다시 눌러 주세요."), "not_ready"
    if token_problem(token):
        return _say("지금은 바꾸지 못했습니다. 잠시 뒤 다시 눌러 주세요."), "no_token"

    turn_on = role_id not in have
    try:
        send("PUT" if turn_on else "DELETE", f"/guilds/{guild}/members/{user}/roles/{role_id}", token,
             None, timeout=ROLE_TIMEOUT_S)
    except (RetryLater, NotFound, RuntimeError) as e:
        log("interaction_role_failed", topic=topic.key, error=type(e).__name__, detail=str(e)[:80])
        return _say("지금은 바꾸지 못했습니다. 잠시 뒤 다시 눌러 주세요."), "role_failed"
    if turn_on:
        return _say(f"**{topic.label}** 알림을 켰습니다. 새 공지가 올라오면 멘션으로 알려 드립니다."), "on"
    return _say(f"**{topic.label}** 알림을 껐습니다."), "off"


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
        response, outcome = _say("지금은 바꾸지 못했습니다. 잠시 뒤 다시 눌러 주세요."), "error"
    log("interaction", outcome=outcome)
    failed = outcome in ("role_failed", "no_token", "not_ready", "error")
    metrics(InteractionOk=0 if failed else 1, InteractionFailed=1 if failed else 0)
    return _http(200, response)

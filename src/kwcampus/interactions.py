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
from kwcampus.discordapi import NotFound, RetryLater, token_problem
from kwcampus.discordapi import call as _discord
from kwcampus.obs import log, metrics
from kwcampus.topics import TOPIC_BY_KEY, TOPICS

PING, MESSAGE_COMPONENT = 1, 3
PONG, CHANNEL_MESSAGE = 1, 4
EPHEMERAL = 1 << 6
PREFIX = "kwsub:"  # button custom_id = PREFIX + topic key
LIST_ID = "kwsub:list"
MAX_BODY = 64 * 1024
MAX_SKEW_S = 300  # a signed request older than this is a replay
ROLE_TIMEOUT_S = 2.0  # Discord wants the answer within 3 s

MENU_CONTENT = (
    "**공지 알림 받기**\n"
    "받고 싶은 주제를 누르면 그 주제의 새 공지가 올라올 때 멘션으로 알려 드립니다. 한 번 더 누르면 꺼집니다.\n"
    "회색 버튼은 단과대입니다. 그 단과대와 소속 학과 게시판의 공지를 알려 드립니다.\n"
    "밤 10시부터 아침 8시 사이에는 멘션 없이 올라오고, 긴급(휴강·출입 통제·강의실 변경·정전·단수·시스템 장애)만 "
    "밤에도 알려 드립니다."
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
    try:
        if abs(now - int(timestamp)) > MAX_SKEW_S:
            return False
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
    except (ValueError, InvalidSignature):
        return False
    return True


def _snowflake(value: str) -> bool:
    return value.isascii() and value.isdigit() and 0 < len(value) <= 21


def _say(text: str) -> dict:
    return {"type": CHANNEL_MESSAGE, "data": {"content": text, "flags": EPHEMERAL, "allowed_mentions": {"parse": []}}}


def handle(payload: dict, cfg: dict, token: str | None, send=_discord) -> tuple[dict, str]:
    """(interaction response, outcome for the log)."""
    if payload.get("type") == PING:
        return {"type": PONG}, "pong"
    if payload.get("type") != MESSAGE_COMPONENT:
        return _say("지원하지 않는 요청입니다."), "unsupported"

    guild = str(payload.get("guild_id") or "")
    member = payload.get("member") or {}
    user = str((member.get("user") or {}).get("id") or "")
    if not cfg.get("guild") or guild != str(cfg["guild"]) or not _snowflake(user):
        return _say("이 버튼은 공지 서버 안에서만 쓸 수 있습니다."), "wrong_guild"
    roles = cfg.get("roles") or {}
    have = {str(r) for r in member.get("roles") or []}
    custom_id = str((payload.get("data") or {}).get("custom_id") or "")

    if custom_id == LIST_ID:
        mine = [t.label for t in TOPICS if str(roles.get(t.key) or "") in have]
        text = f"지금 받는 알림: {', '.join(mine)}" if mine else "아직 받는 알림이 없습니다. 위 버튼에서 골라 주세요."
        return _say(text), "listed"

    topic = TOPIC_BY_KEY.get(custom_id.removeprefix(PREFIX)) if custom_id.startswith(PREFIX) else None
    role_id = str(roles.get(topic.key) or "") if topic else ""
    if topic is None or not _snowflake(role_id) or role_id == guild:  # never @everyone
        return _say("이 버튼은 이제 쓰지 않습니다. 다른 버튼을 눌러 주세요."), "unknown_button"
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
    public_key = params.get(DISCORD_PUBLIC_KEY_PARAM, decrypt=True)
    if not verify(public_key, headers.get("x-signature-ed25519"), headers.get("x-signature-timestamp"),
                  raw, time.time()):
        log("interaction_rejected", reason="no_public_key" if not public_key else "bad_signature")
        metrics(InteractionRejected=1)
        return _http(401, {"error": "invalid request signature"})
    try:
        payload = json.loads(raw)
    except ValueError:
        return _http(400, {"error": "bad json"})

    cfg, token = {}, None
    if payload.get("type") == MESSAGE_COMPONENT:
        cfg = json.loads(params.get(DISCORD_ROLES_PARAM) or "{}")
        token = params.get(DISCORD_TOKEN_PARAM, decrypt=True)
    response, outcome = handle(payload, cfg, token)
    log("interaction", outcome=outcome)
    metrics(InteractionOk=1)
    return _http(200, response)

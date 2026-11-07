"""Invite codes members get for themselves in Discord (the 설치하기 button or /설치), beside the
hand-made list in SSM /kw/mcp/tokens. An invite is "kwc_" + base64url({"s": bridge URL, "t": token})
(kw_mcp.agent.make_invite/parse_invite); the token is both the public-data token the extension sends
(/c/<token>/...) and what a PC registers with, so every check takes the token and accepts either source.

kw-bridge table:
  INVITE#<sha256>     META   issued_to (Discord user id), issued_at, source "discord"; revoked once
                             withdrawn. The bridge's per-invite PC count (devices) lives on the same item.
  DMEMBER#<user id>   META   invites: hashes issued to that member (at most MAX_PER_MEMBER), last_at
  DINVITES#<date>     META   n: invites issued that day (KST) across members, at most DAILY_MAX (TTL 2 days)

Only hashes are stored. The code itself is shown once, to the member who asked, and never again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import UTC, datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from kwcampus import params
from kwcampus.config import KST

TOKENS_PARAM = "/kw/mcp/tokens"  # String: JSON list of sha256 hex digests, put in by hand
BRIDGE_TABLE = os.environ.get("KW_BRIDGE_TABLE", "kw-bridge")
PREFIX = "kwc_"
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
MAX_PER_MEMBER = 3
DAILY_MAX = 50  # across all members: a burst of new accounts can't take more than this in a day
CACHE_S = 60
CACHE_MAX = 2048

_cache: dict[str, tuple[float, bool]] = {}


class TooMany(Exception):
    """The member already has MAX_PER_MEMBER invites."""


class DayFull(Exception):
    """DAILY_MAX invites have gone out today."""


def table(config=None):
    """The bridge table; `config` (a botocore Config) lets the Discord Lambda keep its short timeouts."""
    return boto3.resource("dynamodb", config=config).Table(BRIDGE_TABLE)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_invite(server: str, token: str) -> str:
    """The same string kw_mcp.agent.make_invite builds (kept here: the Discord Lambda stays light)."""
    raw = json.dumps({"s": server.rstrip("/"), "t": token}, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _key(pk: str) -> dict:
    return {"PK": pk, "SK": "META"}


def _on_list(code_hash: str, ttl_s: int) -> bool:
    known = json.loads(params.get(TOKENS_PARAM, ttl_s=ttl_s) or "[]")
    return any(hmac.compare_digest(code_hash, str(k)) for k in known)


def _issued(t, code_hash: str) -> bool:
    item = t.get_item(Key=_key(f"INVITE#{code_hash}"), ConsistentRead=True).get("Item") or {}
    return bool(item.get("issued_to")) and not item.get("revoked")


def hash_alive(code_hash: str, t=None, ttl_s: int = CACHE_S) -> bool:
    """True when this invite hash is on the SSM list or was issued in Discord and not withdrawn.
    A withdrawn invite stops within ttl_s (0: always ask again): the answer and the SSM list are each
    kept half of it, as the two caches stack."""
    if not isinstance(code_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", code_hash):
        return False
    now, keep = time.monotonic(), ttl_s // 2
    hit = _cache.get(code_hash)
    if keep and hit and now - hit[0] < keep:
        return hit[1]
    alive = _on_list(code_hash, keep) or _issued(t if t is not None else table(), code_hash)
    if len(_cache) >= CACHE_MAX:  # unknown tokens tried on the public API must not grow it forever
        _cache.clear()
    _cache[code_hash] = (now, alive)
    return alive


def check(token: object, t=None, ttl_s: int = CACHE_S) -> str | None:
    """The token's hash when it belongs to a live invite, else None. A value not shaped like a
    token is refused without a lookup."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    code_hash = digest(token)
    return code_hash if hash_alive(code_hash, t, ttl_s) else None


def issue(t, member_id: str, server: str, now: int | None = None) -> str:
    """A new invite (the whole kwc_... string) for this Discord member, pointing at `server` (the
    bridge's base URL); raises TooMany past MAX_PER_MEMBER and DayFull past DAILY_MAX today. The
    member's count, the day's count and the invite are written in one transaction: quick clicks can't
    pass either cap. The transaction's ClientRequestToken comes from the code's hash, so botocore
    retrying a commit whose answer timed out gets that commit's success instead of a second write. When
    every attempt's answer is lost, one consistent read of the invite tells whether the commit happened:
    if it did, the code is handed out rather than leaving the member a slot used up and no code."""
    if not re.fullmatch(r"\d{15,22}", str(member_id)):
        raise ValueError("not a Discord user id")
    now = int(now if now is not None else time.time())
    token = secrets.token_urlsafe(24)
    code_hash = digest(token)
    day = datetime.fromtimestamp(now, UTC).astimezone(KST).date().isoformat()
    try:
        t.meta.client.transact_write_items(ClientRequestToken=code_hash[:36], TransactItems=[
            {"Update": {
                "TableName": t.name, "Key": _key(f"DMEMBER#{member_id}"),
                "UpdateExpression": "SET invites = list_append(if_not_exists(invites, :empty), :one), last_at = :now",
                "ConditionExpression": "attribute_not_exists(invites) OR size(invites) < :max",
                "ExpressionAttributeValues": {":empty": [], ":one": [code_hash], ":now": now, ":max": MAX_PER_MEMBER},
            }},
            {"Update": {
                "TableName": t.name, "Key": _key(f"INVITE#{code_hash}"),
                "UpdateExpression": "SET issued_to = :who, issued_at = :now, #src = :src",
                "ConditionExpression": "attribute_not_exists(issued_to)",
                "ExpressionAttributeNames": {"#src": "source"},
                "ExpressionAttributeValues": {":who": str(member_id), ":now": now, ":src": "discord"},
            }},
            {"Update": {
                "TableName": t.name, "Key": _key(f"DINVITES#{day}"),
                "UpdateExpression": "SET n = if_not_exists(n, :zero) + :one, #ttl = :ttl",
                "ConditionExpression": "attribute_not_exists(n) OR n < :max",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": {":zero": 0, ":one": 1, ":max": DAILY_MAX, ":ttl": now + 2 * 86400},
            }},
        ])
    except BotoCoreError:  # timeouts, dropped connections: the commit may still have happened
        try:
            item = t.get_item(Key=_key(f"INVITE#{code_hash}"), ConsistentRead=True).get("Item") or {}
        except (BotoCoreError, ClientError):
            item = {}
        if item.get("issued_to") != str(member_id):
            raise  # the lost answer itself (the look above has been handled)
    except ClientError as e:
        reasons = [r.get("Code") for r in e.response.get("CancellationReasons") or []]
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            if reasons[:1] == ["ConditionalCheckFailed"]:
                raise TooMany(member_id) from None
            if reasons[2:3] == ["ConditionalCheckFailed"]:
                raise DayFull(day) from None
        raise
    _cache.pop(code_hash, None)
    return make_invite(server, token)


def issued_count(t, member_id: str) -> int:
    item = t.get_item(Key=_key(f"DMEMBER#{member_id}"), ConsistentRead=True).get("Item") or {}
    return len(item.get("invites") or [])


def revoke(t, code_hash: str) -> bool:
    """Withdraw a Discord-issued invite (operator use): PCs and connectors that came in with it stop."""
    try:
        t.update_item(Key=_key(f"INVITE#{code_hash}"), UpdateExpression="SET revoked = :yes",
                      ConditionExpression="attribute_exists(issued_to)", ExpressionAttributeValues={":yes": True})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    _cache.pop(code_hash, None)
    return True


def clear_cache() -> None:
    _cache.clear()

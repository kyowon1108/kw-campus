"""Invite codes members get for themselves in Discord (the 설치하기 button or /설치), beside the
hand-made list in SSM /kw/mcp/tokens. An invite is "kwc_" + base64url({"s": bridge URL, "t": token})
(kw_mcp.agent.make_invite/parse_invite); the token is both the public-data token the extension sends
(/c/<token>/...) and what a PC registers with, so every check takes the token and accepts either source.

kw-bridge table:
  INVITE#<sha256>     META   issued_to (Discord user id, or "manual" for a hand-made one copied over by
                             scripts/invites_admin.py migrate), issued_at, source "discord" or "manual";
                             revoked once withdrawn. The bridge's per-invite PC count (devices) lives on the
                             same item.
  DMEMBER#<user id>   META   invites: hashes issued to that member (at most MAX_PER_MEMBER), last_at
  DINVITES#<date>     META   n: invites issued that day (KST) across members, at most DAILY_MAX (TTL 8 days,
                             so Monday's ops digest still reads Friday's)
  DINVITES#total      META   n: invites issued so far (Discord and migrated manual ones), revoked: how many
                             were withdrawn (revoke: /초대회수 or invites_admin.py revoke; running counters,
                             no Scan needed)

Only hashes are stored. The code itself is shown once, to the member who asked, and never again.

The hand-made SSM list is read too until the switch-over: env KW_INVITES_SSM=0 (set by the stack's
invitesSsm=0 context) makes DynamoDB the only place an invite lives. Until then the list still decides
for a migrated manual invite (its INVITE# item counts only while its hash is on the list), so taking a
hash off the list withdraws it as before. Once the list is deleted, a deploy without invitesSsm=0 cuts
every manual invite.
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
SSM_SWITCH = "KW_INVITES_SSM"  # "0": the SSM list above is no longer read
BRIDGE_TABLE = os.environ.get("KW_BRIDGE_TABLE", "kw-bridge")
TOTAL_PK = "DINVITES#total"
MANUAL = "manual"  # issued_to and source of a hand-made invite copied from the SSM list
DAY_TTL_S = 8 * 86400
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


def ssm_list_read() -> bool:
    """False once KW_INVITES_SSM=0: every invite is an INVITE# item then."""
    return os.environ.get(SSM_SWITCH, "1").strip() != "0"


def _on_list(code_hash: str, ttl_s: int) -> bool:
    if not ssm_list_read():
        return False
    known = json.loads(params.get(TOKENS_PARAM, ttl_s=ttl_s) or "[]")
    return any(hmac.compare_digest(code_hash, str(k)) for k in known)


def _issued(t, code_hash: str, ttl_s: int) -> bool:
    """An INVITE# item with an owner, not withdrawn. While the SSM list is still read, a migrated manual
    item counts only if its hash is also on that list: taking a hash off the list still withdraws it.
    After the switch (KW_INVITES_SSM=0) the item alone decides."""
    item = t.get_item(Key=_key(f"INVITE#{code_hash}"), ConsistentRead=True).get("Item") or {}
    if not item.get("issued_to") or item.get("revoked"):
        return False
    if item.get("source") == MANUAL and ssm_list_read():
        return _on_list(code_hash, ttl_s)
    return True


def hash_alive(code_hash: str, t=None, ttl_s: int = CACHE_S) -> bool:
    """True when this invite hash is on the SSM list (until KW_INVITES_SSM=0) or is an INVITE# item with an
    owner (Discord-issued, or a migrated manual one) and not withdrawn.
    A withdrawn invite stops within ttl_s (0: always ask again): the answer and the SSM list are each
    kept half of it, as the two caches stack."""
    if not isinstance(code_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", code_hash):
        return False
    now, keep = time.monotonic(), ttl_s // 2
    hit = _cache.get(code_hash)
    if keep and hit and now - hit[0] < keep:
        return hit[1]
    alive = _on_list(code_hash, keep) or _issued(t if t is not None else table(), code_hash, keep)
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
    member's count, the day's count, the running total and the invite are written in one transaction: quick clicks can't
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
                "ExpressionAttributeValues": {":zero": 0, ":one": 1, ":max": DAILY_MAX, ":ttl": now + DAY_TTL_S},
            }},
            {"Update": {
                "TableName": t.name, "Key": _key(TOTAL_PK),
                "UpdateExpression": "SET n = if_not_exists(n, :zero) + :one",
                "ExpressionAttributeValues": {":zero": 0, ":one": 1},
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


def revoke(t, code_hash: str, now: int | None = None) -> bool:
    """Withdraw an invite (operator use: /초대회수, scripts/invites_admin.py revoke): PCs and connectors that
    came in with it stop within CACHE_S. The item gets revoked and revoked_at, and DINVITES#total its
    revoked count, in one transaction. False when there is no such invite or it is already withdrawn.
    A manual invite still on the SSM list stays alive until KW_INVITES_SSM=0 (take it off the list too)."""
    now = int(now if now is not None else time.time())
    try:
        t.meta.client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": t.name, "Key": _key(f"INVITE#{code_hash}"),
                "UpdateExpression": "SET revoked = :yes, revoked_at = :now",
                "ConditionExpression": "attribute_exists(issued_to) AND attribute_not_exists(revoked)",
                "ExpressionAttributeValues": {":yes": True, ":now": now},
            }},
            {"Update": {
                "TableName": t.name, "Key": _key(TOTAL_PK),
                "UpdateExpression": "ADD revoked :one",
                "ExpressionAttributeValues": {":one": 1},
            }},
        ])
    except ClientError as e:
        reasons = [r.get("Code") for r in e.response.get("CancellationReasons") or []]
        if e.response["Error"]["Code"] == "TransactionCanceledException" and reasons[:1] == ["ConditionalCheckFailed"]:
            return False
        raise
    _cache.pop(code_hash, None)
    return True


def revoke_member(t, member_id: str, now: int | None = None) -> tuple[int, int]:
    """Withdraw every invite issued to this Discord member (/초대회수): (withdrawn now, issued to them).
    Already withdrawn ones don't count again. The member's DMEMBER# list stays, so the slots stay used
    and the member can't take new codes. PCs and connectors that came in with them stop within CACHE_S."""
    item = t.get_item(Key=_key(f"DMEMBER#{member_id}"), ConsistentRead=True).get("Item") or {}
    hashes = [str(h) for h in item.get("invites") or []]
    return sum(revoke(t, code_hash, now) for code_hash in hashes), len(hashes)


def day_count(t, day: str) -> int:
    """Invites issued on this KST day (YYYY-MM-DD)."""
    item = t.get_item(Key=_key(f"DINVITES#{day}")).get("Item") or {}
    return int(item.get("n") or 0)


def totals(t) -> dict[str, int]:
    """{"n": issued so far, "revoked": withdrawn with /초대회수} from the running counter."""
    item = t.get_item(Key=_key(TOTAL_PK)).get("Item") or {}
    return {"n": int(item.get("n") or 0), "revoked": int(item.get("revoked") or 0)}


def add_manual(t, code_hash: str, now: int | None = None) -> bool:
    """Make a hand-made invite (an SSM list hash) an INVITE# item: issued_to and source "manual". False
    when the item already has an owner. Only those fields are set: a PC count (devices) stays as it was."""
    now = int(now if now is not None else time.time())
    try:
        t.update_item(Key=_key(f"INVITE#{code_hash}"),
                      UpdateExpression="SET issued_to = :manual, issued_at = :now, #src = :manual",
                      ConditionExpression="attribute_not_exists(issued_to)",
                      ExpressionAttributeNames={"#src": "source"},
                      ExpressionAttributeValues={":manual": MANUAL, ":now": now})
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    _cache.pop(code_hash, None)
    return True


def clear_cache() -> None:
    _cache.clear()

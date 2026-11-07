"""kw-bridge table: devices, live connections, OAuth bookkeeping, short-lived results.

  DEV#<uid>        META            public key (JWK), created_at, invite_hash, last_seen
  DEV#<uid>        CONN#<conn>     connected_at, agent protocol version (TTL 3 h)
  DEV#<uid>        GRANT#<gid>     one connector approved with a connection code: client_id, client_name,
                                   redirect_host, created, last_used, expires (TTL: GRANT_TTL after approval)
  GRANT#<gid>      RT#<sha256>     refresh token of that grant; a used one keeps rotated_at until the grant
                                   ends, so a stolen copy used later is noticed (TTL: the grant's expiry)
  CONN#<conn>      META            uid (TTL 3 h; for $disconnect cleanup)
  CLIENT#<id>      META            client_name, redirect_uris (DCR; TTL 7 d until first token, then 365 d
                                   from the last token exchange)
  AUTHREQ#<nonce>  META            pending authorize request (TTL 30 min, single use)
  CODE#<sha256>    META            authorization code, with its grant id (TTL 5 min, single use)
  REFRESH#<sha256> META            refresh token from before grants: taken once and moved into a new grant
  PAIR#<sha256>    META            one-time connection code from the PC agent, with net: a keyed hash of the
                                   PC's network address (TTL 15 min); /connect only looks at it, /oauth/approve
                                   takes it
  PAIRS#<uid>#<hour> META          connection codes this device got in that hour (TTL 2 h)
  RESULT#<req>     META            slot opened per request; the PC agent's AES-GCM ciphertext until the bridge
                                   reads it, then only its hash, so a retried post is answered (TTL 2 min)
  INFLIGHT#<uid>   META            agent requests open right now for this device (TTL 60 s)
  RATE#<uid>#<min> META            agent requests sent in that minute (TTL 2 min)
  SIG#<sha256>     META            device signature already used, keyed by its r value (TTL 10 min)

No KLAS credentials, student ids, or plaintext personal data are ever written here.
"""

from __future__ import annotations

import hashlib
import os
import time

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

BRIDGE_TABLE = os.environ.get("KW_BRIDGE_TABLE", "kw-bridge")

CONN_TTL = 3 * 3600
AUTHREQ_TTL = 1800  # the student may first install Claude or find the connector settings
CODE_TTL = 300
GRANT_TTL = 120 * 86400  # about one semester, counted from the approval; refreshing doesn't extend it
PAIR_TTL = 900
MAX_CODE_ATTEMPTS = 5
RESULT_TTL = 120
CLIENT_PENDING_TTL = 7 * 86400  # a registration nobody finished authorizing goes away on its own
CLIENT_TTL = 365 * 86400
MAX_RESULT_CHARS = 300_000  # DynamoDB items stop at 400 KB
INFLIGHT_TTL = 60  # longer than any one agent request (SNAPSHOT_WAIT_S), so a lost release heals itself


def table():
    return boto3.resource("dynamodb").Table(BRIDGE_TABLE)


def _now() -> int:
    return int(time.time())


def _conditional(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _delete_partition(t, pk: str, prefix: str | None = None) -> None:
    cond = Key("PK").eq(pk) & Key("SK").begins_with(prefix) if prefix else Key("PK").eq(pk)
    kwargs = {"KeyConditionExpression": cond, "ProjectionExpression": "PK, SK"}
    with t.batch_writer() as batch:
        while True:
            page = t.query(**kwargs)
            for row in page["Items"]:
                batch.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# devices ------------------------------------------------------------------------

def put_device(t, uid: str, jwk: dict, invite_hash: str | None = None) -> None:
    item = {"PK": f"DEV#{uid}", "SK": "META", "jwk": {k: jwk[k] for k in ("kty", "crv", "x", "y")},
            "created_at": _now()}
    if invite_hash:  # which invite let this PC in, so withdrawing that invite also stops it
        item["invite_hash"] = invite_hash
    t.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")


def get_device(t, uid: str) -> dict | None:
    return t.get_item(Key={"PK": f"DEV#{uid}", "SK": "META"}).get("Item")


def device_key(t, uid: str) -> dict | None:
    item = get_device(t, uid)
    return dict(item["jwk"]) if item else None


def touch_device(t, uid: str) -> None:
    _conditional(t.update_item, Key={"PK": f"DEV#{uid}", "SK": "META"}, UpdateExpression="SET last_seen = :now",
                 ConditionExpression="attribute_exists(PK)", ExpressionAttributeValues={":now": _now()})


def delete_device(t, uid: str) -> None:
    """Everything of this PC: its key, connections, grants and every refresh token of those grants."""
    rows = t.query(KeyConditionExpression=Key("PK").eq(f"DEV#{uid}") & Key("SK").begins_with("GRANT#"))["Items"]
    for row in rows:
        _delete_partition(t, f"GRANT#{row['SK'][len('GRANT#'):]}")
    _delete_partition(t, f"DEV#{uid}")


# connections --------------------------------------------------------------------

def add_connection(t, uid: str, conn: str, version: int = 1) -> None:
    now = _now()
    t.put_item(Item={"PK": f"DEV#{uid}", "SK": f"CONN#{conn}", "connected_at": now, "version": version,
                     "ttl": now + CONN_TTL})
    t.put_item(Item={"PK": f"CONN#{conn}", "SK": "META", "uid": uid, "ttl": now + CONN_TTL})


def remove_connection(t, conn: str) -> None:
    item = t.get_item(Key={"PK": f"CONN#{conn}", "SK": "META"}).get("Item")
    if item:
        t.delete_item(Key={"PK": f"DEV#{item['uid']}", "SK": f"CONN#{conn}"})
    t.delete_item(Key={"PK": f"CONN#{conn}", "SK": "META"})


def latest_connection_info(t, uid: str) -> tuple[str, int] | None:
    """(connection id, agent protocol version) of the device's newest live connection."""
    rows = t.query(KeyConditionExpression=Key("PK").eq(f"DEV#{uid}") & Key("SK").begins_with("CONN#"))["Items"]
    rows = [r for r in rows if int(r.get("ttl", 0)) > _now()]
    if not rows:
        return None
    row = max(rows, key=lambda r: int(r["connected_at"]))
    return row["SK"][len("CONN#"):], int(row.get("version", 1))


def latest_connection(t, uid: str) -> str | None:
    info = latest_connection_info(t, uid)
    return info[0] if info else None


# grants -------------------------------------------------------------------------

def put_grant(t, uid: str, gid: str, client_id: str, client_name: str, redirect_host: str,
              invite_hash: str | None = None) -> dict:
    now = _now()
    item = {"PK": f"DEV#{uid}", "SK": f"GRANT#{gid}", "uid": uid, "grant_id": gid, "client_id": client_id,
            "client_name": client_name, "redirect_host": redirect_host, "created": now,
            "expires": now + GRANT_TTL, "ttl": now + GRANT_TTL}
    if invite_hash:
        item["invite_hash"] = invite_hash
    t.put_item(Item=item)
    return item


def get_grant(t, uid: str, gid: str) -> dict | None:
    item = t.get_item(Key={"PK": f"DEV#{uid}", "SK": f"GRANT#{gid}"}).get("Item")
    return item if item and int(item.get("expires", 0)) > _now() else None


def list_grants(t, uid: str) -> list[dict]:
    rows = t.query(KeyConditionExpression=Key("PK").eq(f"DEV#{uid}") & Key("SK").begins_with("GRANT#"))["Items"]
    return [r for r in rows if int(r.get("expires", 0)) > _now()]


def touch_grant(t, uid: str, gid: str) -> None:
    _conditional(t.update_item, Key={"PK": f"DEV#{uid}", "SK": f"GRANT#{gid}"}, UpdateExpression="SET last_used = :now",
                 ConditionExpression="attribute_exists(PK)", ExpressionAttributeValues={":now": _now()})


def revoke_grant(t, uid: str, gid: str) -> bool:
    """End one grant of this device now: its row (access tokens check it) and all its refresh tokens.
    The refresh tokens sit under GRANT#<gid> alone, so they go only when this device's own row did:
    another PC that learned the grant id (it is in notifications and the token prefix) gets False
    and leaves the grant alone."""
    if not _conditional(t.delete_item, Key={"PK": f"DEV#{uid}", "SK": f"GRANT#{gid}"},
                        ConditionExpression="attribute_exists(PK)"):
        return False
    _delete_partition(t, f"GRANT#{gid}")
    return True


# OAuth --------------------------------------------------------------------------

def put_client(t, client_id: str, name: str, redirect_uris: list[str]) -> None:
    t.put_item(Item={"PK": f"CLIENT#{client_id}", "SK": "META", "client_name": name,
                     "redirect_uris": redirect_uris, "created_at": _now(), "ttl": _now() + CLIENT_PENDING_TTL})


def get_client(t, client_id: str) -> dict | None:
    item = t.get_item(Key={"PK": f"CLIENT#{client_id}", "SK": "META"}).get("Item")
    return item if item and int(item.get("ttl", 0)) > _now() else None


def touch_client(t, client_id: str) -> None:
    """A client that just received tokens stays registered for CLIENT_TTL from now."""
    _conditional(t.update_item, Key={"PK": f"CLIENT#{client_id}", "SK": "META"}, UpdateExpression="SET #ttl = :ttl",
                 ConditionExpression="attribute_exists(PK)", ExpressionAttributeNames={"#ttl": "ttl"},
                 ExpressionAttributeValues={":ttl": _now() + CLIENT_TTL})


def put_authreq(t, nonce: str, params: dict) -> None:
    t.put_item(Item={"PK": f"AUTHREQ#{nonce}", "SK": "META", **params, "ttl": _now() + AUTHREQ_TTL})


def take_authreq(t, nonce: str) -> dict | None:
    """Single use: delete and return in one conditional step."""
    try:
        item = t.delete_item(Key={"PK": f"AUTHREQ#{nonce}", "SK": "META"}, ReturnValues="ALL_OLD").get("Attributes")
    except ClientError:
        return None
    if not item or int(item.get("ttl", 0)) < _now():
        return None
    return item


def peek_authreq(t, nonce: str) -> dict | None:
    item = t.get_item(Key={"PK": f"AUTHREQ#{nonce}", "SK": "META"}).get("Item")
    return item if item and int(item.get("ttl", 0)) >= _now() else None


def count_attempt(t, nonce: str) -> int:
    """Count a wrong connection code against a pending authorize request."""
    try:
        out = t.update_item(Key={"PK": f"AUTHREQ#{nonce}", "SK": "META"}, UpdateExpression="ADD attempts :one",
                            ConditionExpression="attribute_exists(PK)", ExpressionAttributeValues={":one": 1},
                            ReturnValues="UPDATED_NEW")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return MAX_CODE_ATTEMPTS
        raise
    return int(out["Attributes"]["attempts"])


def put_pair(t, code_hash: str, uid: str, net: str | None = None) -> None:
    item = {"PK": f"PAIR#{code_hash}", "SK": "META", "uid": uid, "ttl": _now() + PAIR_TTL}
    if net:  # which network asked for it: /connect/claim sets its cookie only for a browser on the same one
        item["net"] = net
    t.put_item(Item=item)


def count_pair(t, uid: str, per_hour: int) -> bool:
    """True while this device has had fewer than per_hour connection codes this hour."""
    now = _now()
    return _conditional(t.update_item, Key={"PK": f"PAIRS#{uid}#{now // 3600}", "SK": "META"},
                        UpdateExpression="SET n = if_not_exists(n, :zero) + :one, #ttl = :exp",
                        ConditionExpression="attribute_not_exists(n) OR n < :max",
                        ExpressionAttributeNames={"#ttl": "ttl"},
                        ExpressionAttributeValues={":one": 1, ":zero": 0, ":max": per_hour, ":exp": now + 7200})


def peek_pair(t, code_hash: str) -> dict | None:
    """The pending connection code's row (uid, ttl) without using it up."""
    item = t.get_item(Key={"PK": f"PAIR#{code_hash}", "SK": "META"}).get("Item")
    return item if item and int(item.get("ttl", 0)) >= _now() else None


def take_pair(t, code_hash: str) -> str | None:
    item = t.delete_item(Key={"PK": f"PAIR#{code_hash}", "SK": "META"}, ReturnValues="ALL_OLD").get("Attributes")
    if not item or int(item.get("ttl", 0)) < _now():
        return None
    return item["uid"]


def put_code(t, code_hash: str, grant: dict) -> None:
    t.put_item(Item={"PK": f"CODE#{code_hash}", "SK": "META", **grant, "ttl": _now() + CODE_TTL})


def take_code(t, code_hash: str) -> dict | None:
    item = t.delete_item(Key={"PK": f"CODE#{code_hash}", "SK": "META"}, ReturnValues="ALL_OLD").get("Attributes")
    if not item or int(item.get("ttl", 0)) < _now():
        return None
    return item


def put_refresh(t, gid: str, token_hash: str, uid: str, client_id: str, expires: int) -> None:
    t.put_item(Item={"PK": f"GRANT#{gid}", "SK": f"RT#{token_hash}", "uid": uid, "client_id": client_id,
                     "ttl": int(expires)})


def get_refresh(t, gid: str, token_hash: str) -> dict | None:
    item = t.get_item(Key={"PK": f"GRANT#{gid}", "SK": f"RT#{token_hash}"}).get("Item")
    return item if item and int(item.get("ttl", 0)) > _now() else None


def mark_refresh_used(t, gid: str, token_hash: str) -> int | None:
    """Record that a refresh token was exchanged. None when this call was the first use; otherwise
    when it was first used (0 if the row is gone), for the caller's grace-or-theft decision."""
    key = {"PK": f"GRANT#{gid}", "SK": f"RT#{token_hash}"}
    if _conditional(t.update_item, Key=key, UpdateExpression="SET rotated_at = :now",
                    ConditionExpression="attribute_exists(PK) AND attribute_not_exists(rotated_at)",
                    ExpressionAttributeValues={":now": _now()}):
        return None
    item = t.get_item(Key=key).get("Item") or {}
    return int(item.get("rotated_at", 0))


def take_legacy_refresh(t, token_hash: str) -> dict | None:
    """A refresh token issued before grants existed (single use, as it was then)."""
    item = t.delete_item(Key={"PK": f"REFRESH#{token_hash}", "SK": "META"}, ReturnValues="ALL_OLD").get("Attributes")
    if not item or int(item.get("ttl", 0)) < _now():
        return None
    return item


# results ------------------------------------------------------------------------

def open_result(t, request_id: str) -> None:
    t.put_item(Item={"PK": f"RESULT#{request_id}", "SK": "META", "ttl": _now() + RESULT_TTL})


def _result_hash(iv: str, ciphertext: str) -> str:
    return hashlib.sha256(f"{iv}.{ciphertext}".encode()).hexdigest()


def put_result(t, request_id: str, iv: str, ciphertext: str) -> bool:
    """Fill a slot the bridge opened, once. Unknown request ids are refused (no free writes). The
    same body posted again (the agent retrying after a lost response) is accepted, before or after
    the bridge read it; a different body for a filled slot is not."""
    if len(ciphertext) > MAX_RESULT_CHARS:
        return False
    digest = _result_hash(iv, ciphertext)
    key = {"PK": f"RESULT#{request_id}", "SK": "META"}
    if _conditional(t.update_item, Key=key, UpdateExpression="SET iv = :iv, ct = :ct, h = :h",
                    ConditionExpression="attribute_exists(PK) AND attribute_not_exists(ct) AND attribute_not_exists(taken)",
                    ExpressionAttributeValues={":iv": iv, ":ct": ciphertext, ":h": digest}):
        return True
    item = t.get_item(Key=key).get("Item") or {}
    return item.get("h") == digest


def take_result(t, request_id: str, *, close: bool = False) -> dict | None:
    """The agent's answer, once; the slot then keeps only its hash. close=True also shuts a slot that
    is still empty, so an answer arriving later is refused (the caller gave up waiting)."""
    condition = "attribute_exists(PK)" if close else "attribute_exists(ct)"
    try:
        old = t.update_item(Key={"PK": f"RESULT#{request_id}", "SK": "META"},
                            UpdateExpression="REMOVE iv, ct SET taken = :yes", ConditionExpression=condition,
                            ExpressionAttributeValues={":yes": True}, ReturnValues="ALL_OLD").get("Attributes")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return old if old and old.get("ct") else None


# limits per device ---------------------------------------------------------------

def open_request(t, uid: str, limit: int) -> bool:
    """Count one more agent request in flight for this device, unless `limit` already are."""
    now, key = _now(), {"PK": f"INFLIGHT#{uid}", "SK": "META"}
    names, values = {"#ttl": "ttl"}, {":one": 1, ":zero": 0, ":max": limit, ":now": now, ":exp": now + INFLIGHT_TTL}
    if _conditional(t.update_item, Key=key, UpdateExpression="SET n = if_not_exists(n, :zero) + :one, #ttl = :exp",
                    ConditionExpression="attribute_not_exists(n) OR (n < :max AND #ttl >= :now)",
                    ExpressionAttributeNames=names, ExpressionAttributeValues=values):
        return True
    # A count nobody has touched for INFLIGHT_TTL is left over from a crashed request: start again.
    return _conditional(t.update_item, Key=key, UpdateExpression="SET n = :one, #ttl = :exp",
                        ConditionExpression="#ttl < :now", ExpressionAttributeNames=names,
                        ExpressionAttributeValues={":one": 1, ":now": now, ":exp": now + INFLIGHT_TTL})


def close_request(t, uid: str) -> None:
    _conditional(t.update_item, Key={"PK": f"INFLIGHT#{uid}", "SK": "META"}, UpdateExpression="SET n = n - :one",
                 ConditionExpression="n > :zero", ExpressionAttributeValues={":one": 1, ":zero": 0})


def count_request(t, uid: str, per_minute: int) -> bool:
    """True while this device has sent fewer than per_minute agent requests this minute."""
    now = _now()
    return _conditional(t.update_item, Key={"PK": f"RATE#{uid}#{now // 60}", "SK": "META"},
                        UpdateExpression="SET n = if_not_exists(n, :zero) + :one, #ttl = :exp",
                        ConditionExpression="attribute_not_exists(n) OR n < :max",
                        ExpressionAttributeNames={"#ttl": "ttl"},
                        ExpressionAttributeValues={":one": 1, ":zero": 0, ":max": per_minute, ":exp": now + 120})


def use_once(t, key: str, ttl_s: int) -> bool:
    """True the first time `key` is seen within ttl_s."""
    return _conditional(t.put_item, Item={"PK": f"SIG#{key}", "SK": "META", "ttl": _now() + ttl_s},
                        ConditionExpression="attribute_not_exists(PK)")

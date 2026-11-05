"""kw-bridge table: devices, live connections, OAuth bookkeeping, short-lived results.

  DEV#<uid>        META            public key (JWK), created_at
  DEV#<uid>        CONN#<conn>     connected_at, agent protocol version (TTL 3 h)
  CONN#<conn>      META            uid (TTL 3 h; for $disconnect cleanup)
  CLIENT#<id>      META            client_name, redirect_uris (dynamic client registration)
  AUTHREQ#<nonce>  META            pending authorize request (TTL 10 min, single use)
  CODE#<sha256>    META            authorization code (TTL 5 min, single use)
  REFRESH#<sha256> META            refresh token (TTL 180 d, rotated on use)
  PAIR#<sha256>    META            one-time connection code from the PC agent (TTL 15 min)
  BADLOGIN#<cid>   META            KLAS rejected this 간편 연결's password: stop using it (TTL 180 d)
  RESULT#<req>     META            slot opened per request; the PC agent's AES-GCM ciphertext (TTL 2 min)
  SIG#<sha256>     META            device signature already used (TTL 10 min, replay guard)

No KLAS credentials, student ids, or plaintext personal data are ever written here. (간편 연결
keeps the sealed login inside the tokens Claude/ChatGPT hold; rows store only their hashes.)
"""

from __future__ import annotations

import os
import time

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

BRIDGE_TABLE = os.environ.get("KW_BRIDGE_TABLE", "kw-bridge")

CONN_TTL = 3 * 3600
AUTHREQ_TTL = 600
CODE_TTL = 300
REFRESH_TTL = 180 * 86400
PAIR_TTL = 900
MAX_CODE_ATTEMPTS = 5
RESULT_TTL = 120
CLIENT_TTL = 365 * 86400
MAX_RESULT_CHARS = 300_000  # DynamoDB items stop at 400 KB


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


# devices ------------------------------------------------------------------------

def put_device(t, uid: str, jwk: dict) -> None:
    t.put_item(Item={"PK": f"DEV#{uid}", "SK": "META", "jwk": {k: jwk[k] for k in ("kty", "crv", "x", "y")},
                     "created_at": _now()}, ConditionExpression="attribute_not_exists(PK)")


def device_key(t, uid: str) -> dict | None:
    item = t.get_item(Key={"PK": f"DEV#{uid}", "SK": "META"}).get("Item")
    return dict(item["jwk"]) if item else None


def delete_device(t, uid: str) -> None:
    rows = t.query(KeyConditionExpression=Key("PK").eq(f"DEV#{uid}"))["Items"]
    for row in rows:
        t.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})


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


# OAuth --------------------------------------------------------------------------

def put_client(t, client_id: str, name: str, redirect_uris: list[str]) -> None:
    t.put_item(Item={"PK": f"CLIENT#{client_id}", "SK": "META", "client_name": name,
                     "redirect_uris": redirect_uris, "created_at": _now(), "ttl": _now() + CLIENT_TTL})


def get_client(t, client_id: str) -> dict | None:
    return t.get_item(Key={"PK": f"CLIENT#{client_id}", "SK": "META"}).get("Item")


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


def put_pair(t, code_hash: str, uid: str) -> None:
    t.put_item(Item={"PK": f"PAIR#{code_hash}", "SK": "META", "uid": uid, "ttl": _now() + PAIR_TTL})


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


def put_refresh(t, token_hash: str, grant: dict) -> None:
    t.put_item(Item={"PK": f"REFRESH#{token_hash}", "SK": "META", **grant, "ttl": _now() + REFRESH_TTL})


def take_refresh(t, token_hash: str) -> dict | None:
    item = t.delete_item(Key={"PK": f"REFRESH#{token_hash}", "SK": "META"}, ReturnValues="ALL_OLD").get("Attributes")
    if not item or int(item.get("ttl", 0)) < _now():
        return None
    return item


def mark_bad_login(t, cid: str) -> None:
    t.put_item(Item={"PK": f"BADLOGIN#{cid}", "SK": "META", "ttl": _now() + REFRESH_TTL})


def is_bad_login(t, cid: str) -> bool:
    return "Item" in t.get_item(Key={"PK": f"BADLOGIN#{cid}", "SK": "META"})


# results ------------------------------------------------------------------------

def open_result(t, request_id: str) -> None:
    t.put_item(Item={"PK": f"RESULT#{request_id}", "SK": "META", "ttl": _now() + RESULT_TTL})


def close_result(t, request_id: str) -> None:
    t.delete_item(Key={"PK": f"RESULT#{request_id}", "SK": "META"})


def put_result(t, request_id: str, iv: str, ciphertext: str) -> bool:
    """Fill a slot the bridge opened, once. Unknown request ids are refused (no free writes)."""
    if len(ciphertext) > MAX_RESULT_CHARS:
        return False
    return _conditional(t.update_item, Key={"PK": f"RESULT#{request_id}", "SK": "META"},
                        UpdateExpression="SET iv = :iv, ct = :ct",
                        ConditionExpression="attribute_exists(PK) AND attribute_not_exists(ct)",
                        ExpressionAttributeValues={":iv": iv, ":ct": ciphertext})


def take_result(t, request_id: str) -> dict | None:
    try:
        return t.delete_item(Key={"PK": f"RESULT#{request_id}", "SK": "META"}, ReturnValues="ALL_OLD",
                             ConditionExpression="attribute_exists(ct)").get("Attributes")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


def use_once(t, key: str, ttl_s: int) -> bool:
    """True the first time `key` is seen within ttl_s."""
    return _conditional(t.put_item, Item={"PK": f"SIG#{key}", "SK": "META", "ttl": _now() + ttl_s},
                        ConditionExpression="attribute_not_exists(PK)")

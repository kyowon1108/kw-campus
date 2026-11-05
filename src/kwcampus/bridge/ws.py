"""Lambda for the bridge WebSocket API: the extension's always-on connection.

$connect requires ?uid=&ts=&sig= where sig is the device's ECDSA signature over
"kw-connect:<uid>:<ts>" and ts is within five minutes. $default answers pings.
"""

from __future__ import annotations

import time

from kwcampus.bridge import crypto, store
from kwcampus.obs import log

MAX_SKEW_S = 300


def handler(event, context):  # noqa: ARG001
    ctx = event.get("requestContext", {})
    route, conn = ctx.get("routeKey"), ctx.get("connectionId")
    t = store.table()
    if route == "$connect":
        q = event.get("queryStringParameters") or {}
        uid, ts, sig = q.get("uid", ""), q.get("ts", ""), q.get("sig", "")
        try:
            fresh = abs(time.time() - int(ts)) <= MAX_SKEW_S
        except ValueError:
            fresh = False
        jwk = store.device_key(t, uid) if fresh else None
        if not jwk or not crypto.verify_device_signature(jwk, f"kw-connect:{uid}:{ts}", sig) \
                or not store.use_once(t, crypto.sha256_hex(sig), MAX_SKEW_S * 2):
            log("ws_connect_denied", fresh=fresh, known_device=bool(jwk))
            return {"statusCode": 401}
        store.add_connection(t, uid, conn)
        log("ws_connected")
        return {"statusCode": 200}
    if route == "$disconnect":
        store.remove_connection(t, conn)
        return {"statusCode": 200}
    return {"statusCode": 200, "body": '{"type":"pong"}'}

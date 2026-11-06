"""Lambda for the bridge WebSocket API: the PC agent's always-on connection.

$connect requires ?uid=&ts=&sig=&v= where sig is the device's ECDSA signature over
"kw-connect:<uid>:<ts>:<v>" (v is the agent's protocol version) and ts is within five minutes.
Refusals use the status codes in signing.py. $default answers pings.
"""

from __future__ import annotations

from kwcampus.bridge import signing, store
from kwcampus.obs import log


def handler(event, context):
    ctx = event.get("requestContext", {})
    route, conn = ctx.get("routeKey"), ctx.get("connectionId")
    t = store.table()
    if route == "$connect":
        q = event.get("queryStringParameters") or {}
        uid, ts, v = q.get("uid", ""), q.get("ts", ""), q.get("v", "")
        # TODO(2027-03-01): drop the unversioned form. Agents before 1.0 sign "kw-connect:<uid>:<ts>",
        # leaving v outside the signature; they update themselves within a day of a release.
        messages = [f"kw-connect:{uid}:{ts}:{v}", f"kw-connect:{uid}:{ts}"]
        try:
            signing.check(t, uid, ts, q.get("sig", ""), messages)
        except signing.Refused as e:
            log("ws_connect_denied", reason=e.error)
            return {"statusCode": e.status}
        try:
            version = max(1, min(int(v or "1"), 99))
        except ValueError:
            version = 1
        store.add_connection(t, uid, conn, version)
        store.touch_device(t, uid)
        log("ws_connected")
        return {"statusCode": 200}
    if route == "$disconnect":
        store.remove_connection(t, conn)
        return {"statusCode": 200}
    return {"statusCode": 200, "body": '{"type":"pong"}'}

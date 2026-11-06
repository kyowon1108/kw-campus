"""Signed requests from a PC agent: the device's ECDSA P-256 signature over "kw-<purpose>:...:<ts>".

Shared by the HTTP device endpoints (app.py) and the WebSocket $connect (ws.py). Each refusal has
its own status and error, so an agent can tell a wrong PC clock from a removed device:

  400 bad_request       uid, ts or sig missing or malformed
  401 stale_timestamp   ts more than MAX_SKEW_S from the server clock (the PC clock is off)
  403 unknown_device    no such device (unlinked, or never registered)
  403 bad_signature     the device exists but the signature doesn't verify
  409 replayed          this signature was already used
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

from kwcampus.bridge import crypto, store

UID = re.compile(r"u_[A-Za-z0-9_-]{22}")
MAX_SKEW_S = 300


class Refused(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


def check(t, uid: object, ts: object, sig: object, messages: Iterable[str]) -> dict:
    """The device row when sig is a fresh, unused signature by uid's key over one of messages."""
    if not (isinstance(uid, str) and UID.fullmatch(uid) and isinstance(ts, str) and re.fullmatch(r"[0-9]{1,12}", ts)
            and isinstance(sig, str)):
        raise Refused(400, "bad_request")
    if abs(time.time() - int(ts)) > MAX_SKEW_S:
        raise Refused(401, "stale_timestamp")
    device = store.get_device(t, uid)
    if not device:
        raise Refused(403, "unknown_device")
    if not any(crypto.verify_device_signature(dict(device["jwk"]), m, sig) for m in messages):
        raise Refused(403, "bad_signature")
    # Keyed on r, not on the string: re-spelled or (r, n - s) copies of a used signature are replays too.
    if not store.use_once(t, crypto.signature_replay_key(sig), MAX_SKEW_S * 2):
        raise Refused(409, "replayed")
    return device

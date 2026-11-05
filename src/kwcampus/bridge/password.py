"""간편 연결: the connector holds the KLAS login, sealed; the bridge logs in when asked.

The approval page sends the KLAS id/password once. The bridge checks them against KLAS, then
seals them (AES-256-GCM, key in SSM, bound to the OAuth client) into the authorization code,
refresh token and access tokens it hands to Claude/ChatGPT. Nothing about the login is written
to the table: rows hold only a random connection id (cid), token hashes and a "password was
rejected" flag.

On a tool call the access token is unsealed in memory, a KLAS session is opened (or reused
while this Lambda instance stays warm, up to IDLE_S), and the snapshot is built with the same
code as the PC agent (kw_mcp.service.KlasService.snapshot).
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from kw_mcp.klas import KlasBadPassword, KlasClient, KlasError, KlasLoginBlocked, KlasLoginError
from kw_mcp.service import KlasService

from kwcampus import params
from kwcampus.bridge import crypto
from kwcampus.obs import log

SEAL_KEY_PARAM = "/kw/bridge/seal-key"
IDLE_S = 900  # drop an unused KLAS session (and the login it holds) from memory after 15 minutes
MAX_SESSIONS = 50


@dataclass(frozen=True)
class Login:
    cid: str
    login_id: str = field(repr=False)
    password: str = field(repr=False)


def _key() -> bytes:
    raw = params.get(SEAL_KEY_PARAM, decrypt=True, ttl_s=600)
    if not raw:
        raise RuntimeError("bridge seal key is not configured")
    return crypto.b64u_decode(raw)


def _aad(client_id: str) -> bytes:
    return f"kw-seal:{client_id}".encode()


def seal(login: Login, client_id: str) -> str:
    iv = os.urandom(12)
    plain = json.dumps({"c": login.cid, "i": login.login_id, "p": login.password}, ensure_ascii=False).encode()
    return crypto.b64u(iv + AESGCM(_key()).encrypt(iv, plain, _aad(client_id)))


def unseal(blob: str, client_id: str) -> Login | None:
    try:
        raw = crypto.b64u_decode(blob)
        data = json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], _aad(client_id)))
        return Login(str(data["c"]), str(data["i"]), str(data["p"]))
    except (ValueError, KeyError, TypeError, InvalidTag):
        return None


# --- KLAS sessions kept only in this instance's memory ----------------------------------

@dataclass
class _Session:
    used: float
    login: Login
    client: KlasClient
    service: KlasService


_SESSIONS: OrderedDict[str, _Session] = OrderedDict()


async def _drop(cid: str) -> None:
    session = _SESSIONS.pop(cid, None)
    if session:
        await session.client.aclose()


async def _session(login: Login) -> _Session:
    now = time.monotonic()
    for cid in [c for c, s in _SESSIONS.items() if now - s.used > IDLE_S]:
        await _drop(cid)
    session = _SESSIONS.get(login.cid)
    if session and session.login != login:
        await _drop(login.cid)
        session = None
    if session is None:
        client = KlasClient(login.login_id, login.password)
        session = _Session(now, login, client, KlasService(client))
        _SESSIONS[login.cid] = session
        while len(_SESSIONS) > MAX_SESSIONS:
            await _drop(next(iter(_SESSIONS)))
    session.used = now
    _SESSIONS.move_to_end(login.cid)
    return session


def _problem(e: Exception) -> dict[str, Any]:
    if isinstance(e, KlasBadPassword):
        return {"error": "klas_bad_password", "message": str(e).split(" — ")[0]}
    if isinstance(e, KlasLoginBlocked):
        return {"error": "klas_blocked", "message": str(e)}
    if isinstance(e, KlasLoginError):
        return {"error": "klas_error", "message": str(e)}
    log("klas_request_failed", error=e.__class__.__name__)
    return {"error": "klas_error"}


async def check(login: Login) -> dict[str, Any] | None:
    """Log in now (approval page). None when it worked, else an error dict like snapshot()'s."""
    session = await _session(login)
    try:
        await session.client.login()
    except (KlasError, httpx.HTTPError) as e:
        await _drop(login.cid)
        return _problem(e)
    return None


async def snapshot(login: Login) -> dict[str, Any]:
    session = await _session(login)
    try:
        return await session.service.snapshot()
    except (KlasError, LookupError, httpx.HTTPError) as e:
        if isinstance(e, KlasLoginError):
            await _drop(login.cid)
        return _problem(e)

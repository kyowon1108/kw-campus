"""Discord REST calls with the bot token, shared by the sender, the button Lambda and the setup script.

No heavy imports: the button Lambda must answer within 3 s, cold start included.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request

from kwcampus.fetch import TLS

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/kyowon1108/kw-campus, 1.0)"
# Three base64url parts joined by dots. Anything else (a newline, a space, the wrong text pasted)
# is refused before it can reach an HTTP header, where http.client would echo it into an error.
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{17,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{20,}")


class RetryLater(Exception):
    """Discord asked us to slow down or failed transiently; let SQS redeliver."""


class RateLimited(RetryLater):
    """429: Discord says how long to wait (seconds)."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class NotFound(Exception):
    """Discord answered 404: the message (or member, role, channel) is gone."""


def clean_token(raw: str | None) -> str | None:
    """The token as stored, minus the surrounding whitespace a paste may add."""
    return raw.strip() if raw else None


def token_problem(token: str | None) -> str | None:
    if not token:
        return "bot token not set"
    if len(token) > 200 or not _TOKEN.fullmatch(token):
        return "bot token is not a Discord token (expected three dot-separated parts)"
    return None


def is_snowflake(value: object) -> bool:
    text = str(value or "")
    return text.isascii() and text.isdigit() and 0 < len(text) <= 21


def _seconds(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _retry_after(e: urllib.error.HTTPError) -> float:
    """Seconds to wait after a 429: the JSON body's retry_after, else the Retry-After header (a 429
    from Cloudflare in front of Discord has an HTML body), else X-RateLimit-Reset-After, else 1."""
    try:
        body = json.loads(e.read() or b"{}")
    except (ValueError, TypeError, OSError):
        body = None
    candidates = [body.get("retry_after") if isinstance(body, dict) else None]
    headers = e.headers
    if headers is not None:
        candidates += [headers.get("Retry-After"), headers.get("X-RateLimit-Reset-After")]
    for value in candidates:
        seconds = _seconds(value)
        if seconds is not None:
            return seconds
    return 1.0


def call(method: str, path: str, token: str, body: dict | None = None, timeout: float = 10) -> dict:
    if body is None and method not in ("GET", "DELETE"):
        body = {}  # PUT/POST/PATCH always carry a JSON body (and a Content-Length)
    request = urllib.request.Request(
        f"{API}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(f"discord 404 on {method}") from None
        if e.code == 429:
            raise RateLimited("discord 429", _retry_after(e)) from None
        if e.code >= 500:
            raise RetryLater(f"discord {e.code}") from None
        raise RuntimeError(f"discord {e.code} on {method}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RetryLater("discord unreachable") from e
    except ValueError:  # a bad header value (http.client quotes it, token included) or a bad reply
        raise RuntimeError(f"invalid request or reply on {method}") from None



"""Discord REST calls with the bot token, shared by the sender, the button Lambda and the setup script.

No heavy imports: the button Lambda must answer within 3 s, cold start included.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from kwcampus.fetch import TLS

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/kyowon1108/kw-notice-mcp, 0.1)"


class RetryLater(Exception):
    """Discord asked us to slow down or failed transiently; let SQS redeliver."""


class NotFound(Exception):
    """Discord answered 404: the message (or member, role, channel) is gone."""


def token_problem(token: str | None) -> str | None:
    # Discord tokens are plain ASCII; anything else means the wrong text was pasted into SSM.
    if not token:
        return "bot token not set"
    if not token.isascii() or " " in token.strip():
        return "bot token is not a Discord token (non-ASCII or spaces)"
    return None


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
        if e.code == 429 or e.code >= 500:
            raise RetryLater(f"discord {e.code}") from None
        raise RuntimeError(f"discord {e.code} on {method}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RetryLater("discord unreachable") from e



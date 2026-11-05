"""One polite GET to a university list page. No cookies, no redirects, no retries."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

import certifi

from kwcampus.config import USER_AGENT

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 15

# Same markers kw-notice-mcp treats as a CAPTCHA/WAF challenge (robots.py).
CHALLENGE_MARKERS = (
    b"captcha",
    b"recaptcha",
    b"robot check",
    b"web application firewall",
    b"cloudflare",
    b"cf-chl-",
    b"incapsula",
    b"imperva",
    b"sucuri",
    b"akamai",
    b"access denied",
    b"security check",
)


@dataclass(frozen=True)
class FetchResult:
    html: str | None
    block_reason: str | None  # None when html is usable
    status: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None  # a redirect surfaces as an HTTPError (3xx) and is treated as a block


# Explicit CA bundle so local runs and Lambda verify certificates the same way.
TLS = ssl.create_default_context(cafile=certifi.where())
_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=TLS))


def _has_challenge(body: bytes) -> bool:
    head = body[:4096].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def fetch_list_page(url: str) -> FetchResult:
    request = urllib.request.Request(  # noqa: S310 - URL comes from source.build_list_url
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "ko"},
    )
    try:
        with _opener.open(request, timeout=TIMEOUT_S) as response:
            status = response.status
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        reason = {403: "forbidden", 429: "rate_limit"}.get(e.code)
        if reason is None:
            reason = "redirect" if 300 <= e.code < 400 else "http_failure"
        return FetchResult(None, reason, e.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return FetchResult(None, "transport_failure", 0)

    if len(body) > MAX_BYTES:
        return FetchResult(None, "oversized", status)
    if _has_challenge(body):
        return FetchResult(None, "captcha", status)
    if status != 200 or content_type != "text/html":
        return FetchResult(None, "markup", status)
    try:
        return FetchResult(body.decode("utf-8", errors="strict"), None, status)
    except UnicodeDecodeError:
        return FetchResult(None, "markup", status)

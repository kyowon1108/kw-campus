"""One polite GET to a university list page. No cookies, no redirects, no retries."""

from __future__ import annotations

import http.client
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import certifi

from kwcampus.config import USER_AGENT

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 15  # per socket operation (connect, each read)
DEADLINE_S = 30  # the whole body; a server trickling bytes must not use up the Lambda
CHUNK = 64 * 1024
# Lambda seconds one request may take at worst (connect + headers, then the body's deadline); a
# collector with less time left skips the request instead of being cut off in the middle of it.
WORST_CASE_S = TIMEOUT_S + DEADLINE_S + 5

# Signatures of a challenge or block page, looked for only when the page has none of the content
# a collector expects (see is_challenge). Plain words such as "cloudflare" or "akamai" are not
# enough: sites load scripts and analytics from those companies on normal pages.
_CHALLENGE = re.compile(
    rb"<title>\s*(?:just a moment|attention required|access denied|security check|robot check)"
    rb"|cf-chl-|challenge-platform|g-recaptcha|h-captcha|hcaptcha\.com|recaptcha/api"
    rb"|_incapsula_resource|incapsula incident|sucuri website firewall|web application firewall"
    rb"|reference #\d+\.[0-9a-f]+\.\d+",  # Akamai's access-denied page
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FetchResult:
    html: str | None
    block_reason: str | None  # None when html is usable
    status: int
    retry_after: int = 0  # seconds the server asked us to wait (429/503 Retry-After)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect surfaces as an HTTPError (3xx): the page moved


# Explicit CA bundle so local runs and Lambda verify certificates the same way.
TLS = ssl.create_default_context(cafile=certifi.where())
_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=TLS))


def is_challenge(page: str | bytes | None) -> bool:
    """A challenge/WAF page signature in the first 16 KB. Collectors ask only after a page parsed to
    nothing, so a normal list page with a third-party script can never count as a block."""
    if not page:
        return False
    head = (page.encode("utf-8", "replace") if isinstance(page, str) else page)[:16384]
    return _CHALLENGE.search(head) is not None


def empty_reason(page: str | None) -> str:
    """Why an answered page gave no rows: a challenge page (a hard block) or just nothing we know."""
    return "captcha" if is_challenge(page) else "no_rows"


def _retry_after(value: str | None, now: datetime | None = None) -> int:
    """Seconds from a Retry-After header: delta-seconds or an HTTP date."""
    if not value:
        return 0
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0, int((when - (now or datetime.now(UTC))).total_seconds()))


def _challenge_body(error: urllib.error.HTTPError) -> bool:
    try:
        return is_challenge(error.read(16384))
    except (OSError, http.client.HTTPException, ValueError):
        return False


def _read(response, deadline: float) -> bytes | None:
    """The body, at most MAX_BYTES + 1 of it; None when the deadline passed first."""
    chunks, size = [], 0
    while size <= MAX_BYTES:
        chunk = response.read1(CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if time.monotonic() > deadline:
            return None
    return b"".join(chunks)


def fetch_list_page(url: str, form: dict[str, str] | None = None, encoding: str = "utf-8",
                    errors: str = "strict", types: tuple[str, ...] = ("text/html",)) -> FetchResult:
    """GET the page, or POST a small form (the academic calendar loads its year that way).

    By default the page must be clean UTF-8. Department boards pass errors="replace": some site
    templates carry stray EUC-KR bytes (elcomm's menu), and EUC-KR boards ("cp949") cut long
    titles at a byte count, leaving half a character. RSS feeds pass their own content types.

    Block reasons: forbidden (403), rate_limit (429), server_error (5xx), http_failure (other 4xx),
    redirect (3xx), transport_failure (no answer, or a broken one), slow (the body took longer than
    DEADLINE_S), oversized, markup (not a 200 page of an expected type, or not decodable).
    """
    headers = {"User-Agent": USER_AGENT, "Accept": ", ".join(types), "Accept-Language": "ko"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers)  # fixed allowlisted URLs
    deadline = time.monotonic() + DEADLINE_S
    try:
        with _opener.open(request, timeout=TIMEOUT_S) as response:
            status = response.status
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = _read(response, deadline)
    except urllib.error.HTTPError as e:
        wait = _retry_after(e.headers.get("Retry-After") if e.headers else None) if e.code in (429, 503) else 0
        reason = {403: "forbidden", 429: "rate_limit"}.get(e.code)
        if reason is None:
            reason = ("redirect" if 300 <= e.code < 400 else "server_error" if e.code >= 500 else "http_failure")
        if reason == "server_error" and _challenge_body(e):  # Cloudflare answers its challenge with 503
            reason = "captcha"
        return FetchResult(None, reason, e.code, wait)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return FetchResult(None, "transport_failure", 0)

    if body is None:
        return FetchResult(None, "slow", status)
    if len(body) > MAX_BYTES:
        return FetchResult(None, "oversized", status)
    if status != 200 or content_type not in types:
        return FetchResult(None, "captcha" if is_challenge(body) else "markup", status)
    try:
        return FetchResult(body.decode(encoding, errors=errors), None, status)
    except UnicodeDecodeError:
        return FetchResult(None, "markup", status)

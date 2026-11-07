"""A notice's own images and attachments, read for extraction. The collector's page runs (page_run) and
the operator's backfill (scripts/collector_admin.py extract-backfill) share this code.

kw_mcp.notices reads them: only the collectors' hosts (and Wix's file host, which the gate here
refuses), redirects checked hop by hop, size and time caps, HTTP/1.1, no cookies. The gate here adds
the collectors' rules: robots records, the per-server gate and daily cap in kw-public, and only what
that cap has to spare after today's list runs (extract.spare). What a server answers counts for its
gate (WatchedTransport). The same rules hold for both callers:
- up to MAX_IMAGES images the notice itself shows (posters), IMAGE_BYTES at most together; kw_mcp
  takes PNG, JPEG, GIF and WebP only, up to 1.5 MB each, skips icons and downscales nothing;
- the text of up to MAX_ATTACHMENTS files (PDF, HWP, HWPX, DOCX, XLSX, read by kw_mcp.attachments),
  ATTACHMENT_CHARS at most each, masked like the body (extract.compose);
- a notice whose title may name people (routing.name_risk: rosters, prizes, results) gets its body
  only, and a file named like a roster is skipped: images can't be masked.

A page run has already fetched the page with the collector's own fetch: kw_mcp gets it from memory
(PageInHand), so only the images and files go out. They follow the page politeness.ASSET_GAP_S apart
(a browser opening the notice loads them all at once), each through politeness.acquire, so each counts
against the server's daily cap. They are read only when the page's server has a spare request for every
planned one; otherwise the body goes alone (ExtractAssetsSkipped). A notice makes at most its planned
requests plus ASSET_SLACK (an image kw_mcp finds to be an icon, a redirect), at most 1 + 8 with the
page, and never one the gate's spare check refuses. Then each server asked gets its full minute again
(politeness.rest). The reading stops at ASSET_DEADLINE_S, or earlier when the Lambda needs the time
for the model call (extract.MODEL_S).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from kwcampus import extract, politeness
from kwcampus.obs import log, metrics
from kwcampus.routing import name_risk

MIB = 1024 * 1024
MAX_IMAGES, MAX_ATTACHMENTS = 4, 2
ATTACHMENT_CHARS = 30_000  # about 30 pages of text
IMAGE_BYTES = 4 * MIB  # all images of one model request together (5.3 MB as base64)
ASSET_SLACK = 2
ASSET_DEADLINE_S = 60.0
ASSET_MIN_S = 15.0  # less time than this left for the images and files: the body goes alone
FILE_BYTES = 15 * MIB  # one attachment in the Lambda, as on the bridge (the PC takes 30 MB)
_FILE_OK = re.compile(r"\.(pdf|hwp|hwpx|docx|xlsx)$", re.IGNORECASE)
_ROSTER_FILE = re.compile(r"명단|명부|합격|결과|수상|당선|선발자|대상자|입상")


def file_wanted(name: str) -> bool:
    """A file whose text is read: a document kw_mcp.attachments reads, not named like a roster."""
    return bool(_FILE_OK.search(name)) and not _ROSTER_FILE.search(name)


class PoliteGate:
    """kw_mcp.notices' gate: the collectors' per-server gate in kw-public. Waits out the gap between two
    requests to a server; refuses a host the collectors don't read, a path robots.txt disallows, and a
    server whose daily cap has nothing to spare after today's list runs.

    A page run also passes `fetched` (the page it already has: kw_mcp asks for it, PageInHand hands it
    over, nothing goes out), `budget` (requests this notice may still make) and `deadline`
    (time.monotonic() by which any waiting must be over)."""

    def __init__(self, public, now: Callable[[], datetime], sleep=asyncio.sleep, refused=Exception,
                 extra_today: int = 0, gap_s: float | None = None, fetched: str | None = None,
                 budget: int | None = None, deadline: float | None = None):
        self.public, self.now, self.sleep, self.refused = public, now, sleep, refused
        self.extra_today = max(0, extra_today)
        # an operator's catch-up run, or a notice's own images and files, may come closer than the minute
        self.interval = timedelta(seconds=max(1.0, gap_s)) if gap_s else None
        self.fetched, self.budget, self.deadline = fetched, budget, deadline
        self.origins: set[str] = set()  # servers a request went to through this gate

    def extra(self, now: datetime) -> int:
        """Requests above our own daily cap, only while no list run is left today (weekends, or after the
        last weekday run): the collectors check the plain cap, so an extra taken earlier would cost them."""
        return self.extra_today if extract.runs_left(now) == 0 else 0

    async def before(self, url: str) -> None:
        if self.fetched is not None and url == self.fetched:
            return
        host = urlsplit(url).hostname or ""
        page = extract.page_for_host(host)
        if page is None or not politeness.robots_allows(url):
            raise self.refused(f"{host}: 수집기가 읽지 않는 주소입니다.")
        if self.budget is not None and self.budget < 1:
            raise self.refused(f"{host}: 이 공지에 쓸 요청을 다 썼습니다.")
        for _ in range(10):
            now = self.now()
            extra = self.extra(now)
            if extract.spare(self.public, page, now) + extra < 1:
                raise self.refused(f"{host}: 오늘 이 서버에 보낼 수 있는 요청을 다 썼습니다.")
            permit = politeness.acquire(self.public, page.origin, now, daily_cap=page.daily_cap + extra,
                                        interval=self.interval)
            if permit.granted:
                self.origins.add(page.origin)
                if self.budget is not None:
                    self.budget -= 1
                return
            if permit.reason not in ("too_soon", "raced"):
                raise self.refused(f"{host}: {permit.reason}")
            wait = int(politeness.state(self.public, page.origin).get("next_at", 0)) - now.timestamp()
            if self.deadline is not None and time.monotonic() + wait > self.deadline - 2:
                raise self.refused(f"{host}: 차례를 기다릴 시간이 없습니다.")
            await self.sleep(max(1.0, wait + 1))
        raise self.refused(f"{host}: 차례를 얻지 못했습니다.")

    def rest(self) -> None:
        """Every server asked through this gate waits its full minute from now (after a shorter gap)."""
        for origin in self.origins:
            politeness.rest(self.public, origin, self.now())


class WatchedTransport(httpx.AsyncBaseTransport):
    """What a server answers counts for its gate as on the collectors' requests: 403/429 are blocks,
    5xx and connection failures transient, 200 a success (politeness.record_*)."""

    def __init__(self, public, now: Callable[[], datetime], inner: httpx.AsyncBaseTransport | None = None):
        self.public, self.now = public, now
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        page = extract.page_for_host(request.url.host)
        try:
            response = await self.inner.handle_async_request(request)
        except httpx.TransportError:
            if page is not None:
                politeness.record_block(self.public, page.origin, self.now(), "transport_failure")
            raise
        if page is not None:
            status = response.status_code
            reason = {403: "forbidden", 429: "rate_limit"}.get(status) or ("server_error" if status >= 500 else None)
            if reason:
                wait = response.headers.get("Retry-After", "")
                politeness.record_block(self.public, page.origin, self.now(), reason, int(wait) if wait.isdigit() else 0)
            elif status == 200:
                politeness.record_success(self.public, page.origin)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class PageInHand(httpx.AsyncBaseTransport):
    """The page a page run already fetched, handed to kw_mcp.notices from memory; every other request
    goes on to `inner`."""

    def __init__(self, url: str, html: str, inner: httpx.AsyncBaseTransport):
        self.url, self.html, self.inner = httpx.URL(url), html, inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url == self.url:
            return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"},
                                  content=self.html.encode("utf-8"), request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def unwrap(text: object) -> str:
    """Text kw_mcp.notices wrapped as someone else's writing, without the markers."""
    from kw_mcp.text import CONTENT_CLOSE, CONTENT_OPEN

    return str(text or "").removeprefix(CONTENT_OPEN).removesuffix(CONTENT_CLOSE).strip("\n")


def _fit(images: list[tuple[str, bytes]], pictures) -> list[tuple[str, bytes]]:
    """`images` and then `pictures`, in order, while fewer than MAX_IMAGES and IMAGE_BYTES in all."""
    total = sum(len(data) for _, data in images)
    for p in pictures:
        if len(images) >= MAX_IMAGES:
            break
        if total + len(p.data) <= IMAGE_BYTES:
            images.append((p.mime, p.data))
            total += len(p.data)
    return images


async def read_parts(notice: dict, read, gate, transport: Callable[[], httpx.AsyncBaseTransport],
                     limits: Callable[[], object] | None = None
                     ) -> tuple[str, list[tuple[str, bytes]], list[tuple[str, str]]]:
    """(body, images as (mime, bytes), attachments as (name, text)) of one notice through `read`
    (kw_mcp.notices.read_notice); the images and files only when its title can't name people. A file
    the gate refuses (notices.Refused) ends the call. `limits` gives kw_mcp's limits for each call."""
    from kw_mcp.notices import NoticeError, Refused

    def more() -> dict:
        return {} if limits is None else {"limits": limits()}

    _, url = extract.page_for(notice)
    plain = not name_risk(str(notice.get("category_id") or ""), str(notice.get("title") or ""))
    answer = await read(url, images=plain, gate=gate, transport=transport(), **more())
    images = _fit([], answer.pictures)
    files = []
    for row in (answer.data.get("attachments") or []) if plain else []:
        name = str(row.get("name") or "")
        if len(files) >= MAX_ATTACHMENTS or not file_wanted(name):
            continue
        text, part, parts = "", 1, 1
        while part <= parts and len(text) < ATTACHMENT_CHARS:
            try:
                got = await read(url, attachment=int(row["index"]), part=part, gate=gate, transport=transport(),
                                 **more())
            except Refused:
                raise
            except NoticeError:  # an unreadable or missing file is skipped
                break
            if got.pictures:  # an image attached as a file
                images = _fit(images, got.pictures)
                break
            text += unwrap(got.data.get("text")) + "\n"
            part, parts = part + 1, int(got.data.get("total_parts") or 1)
        if text.strip():
            files.append((name, text[:ATTACHMENT_CHARS]))
    return unwrap(answer.data.get("body")), images, files


def page_run(table, notice: dict, page, url: str, html: str, clock: Callable[[], datetime],
             time_left: Callable[[], float] | None = None, read=None,
             inner_transport: Callable[[], httpx.AsyncBaseTransport] | None = None, sleep=None
             ) -> tuple[list[tuple[str, bytes]], list[tuple[str, str]], str]:
    """(images, attachments, how) of a notice whose page (`html`, from `url` on `page`'s server) a page
    run just fetched. how: "read", "none" (nothing to read), "no_spare", "no_time" or "failed". The
    caller has checked the title (name_risk) and gives the page's server its minute back afterwards."""
    from kw_mcp import notices

    how = "no_time"
    left = ASSET_DEADLINE_S if time_left is None else min(ASSET_DEADLINE_S, time_left() - extract.MODEL_S)
    gate = PoliteGate(table, clock, sleep or asyncio.sleep, refused=notices.Refused, gap_s=politeness.ASSET_GAP_S,
                      fetched=notices.check_url(url), deadline=time.monotonic() + left)
    read = read or notices.read_notice

    def transport() -> httpx.AsyncBaseTransport:
        return PageInHand(gate.fetched, html,
                          WatchedTransport(table, clock, inner_transport() if inner_transport else None))

    def limits():
        return notices.Limits(file_bytes=FILE_BYTES, deadline_s=max(0.0, gate.deadline - time.monotonic()))

    async def go() -> tuple[list, list, str]:
        listed = (await read(url, gate=gate, transport=transport(), limits=limits())).data  # from memory
        images = [r for r in listed.get("images") or [] if extract.page_for_host(urlsplit(str(r.get("url"))).hostname or "")]
        files = [r for r in listed.get("attachments") or [] if file_wanted(str(r.get("name") or ""))]
        planned = min(len(images), MAX_IMAGES) + min(len(files), MAX_ATTACHMENTS)
        if planned == 0:
            return [], [], "none"
        if extract.spare(table, page, clock()) < planned:
            return [], [], "no_spare"
        gate.budget = planned + ASSET_SLACK
        _, got_images, got_files = await read_parts(notice, read, gate, transport, limits)
        return got_images, got_files, "read"

    images, files = [], []
    try:
        if left >= ASSET_MIN_S:
            images, files, how = asyncio.run(go())
    except Exception as e:  # noqa: BLE001 - the images and files are extra: the body still goes
        how = "failed"
        log("extract_assets_failed", notice=notice.get("SK"), error=e.__class__.__name__)
    finally:
        gate.rest()
    if how not in ("read", "none"):
        log("extract_assets_skipped", notice=notice.get("SK"), reason=how)
        metrics(ExtractAssetsSkipped=1)
    return images, files, how

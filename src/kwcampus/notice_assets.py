"""A notice's own images and attachments, read for extraction, and what of them was read. The collector's
page runs (page_run) and the operator's backfill (scripts/collector_admin.py extract-backfill) share this code.

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
  only, and a file named like a roster is skipped: images can't be masked;
- no file at all from a server whose page says files=False (extract.Page: gnuboard's download.php serves a
  file only to the browser session that opened the post), so no request is spent on one.

A page run has already fetched the page with the collector's own fetch: kw_mcp gets it from memory
(PageInHand), so listing what the page shows costs no request, and only the images and files go out. They
follow the page politeness.ASSET_GAP_S apart (a browser opening the notice loads them all at once), each
through politeness.acquire, so each counts against the server's daily cap. They are read only when the
page's server has a spare request for every planned one. A notice makes at most its planned requests plus
ASSET_SLACK (an image kw_mcp finds to be an icon, a redirect), at most 1 + 8 with the page, and never one
the gate's spare check refuses. Then each server asked gets its full minute again (politeness.rest). The
reading stops at ASSET_DEADLINE_S, or earlier when the Lambda needs the time for the model call
(extract.MODEL_S).

Every run reports what it found and read (Assets): images and files found, read, and skipped by reason.
Reasons in TRANSIENT (no spare request today, no time, a failed or refused request, a resting server) are
worth another try (extract requeues the notice); the others are final by design: over the caps, a roster
file, another site's image, a file only the site's own session gets, too large, not an image, an icon, or
privacy (a title that may name people). The fingerprint (Assets.fp) is a hash of the image URLs and the
files' names and paths (their query strings carry per-view tokens on some boards), so a notice whose
poster or file changed while its text didn't is read again (extract).

총학생회's posters sit on Supabase storage: read_thumbnail fetches one from exactly THUMB_HOST under
THUMB_PATH, through its own gate (politeness, THUMB_DAILY_CAP a day), no redirects followed, the same image
size cap as kw_mcp's.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from kwcampus import extract, politeness
from kwcampus.config import USER_AGENT
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
# Skips worth another try later; every other reason is final.
TRANSIENT = frozenset({"budget", "time", "error", "resting", "refused"})
# 총학생회's poster thumbnails (Supabase public storage of its site; robots.txt there is a 404)
THUMB_HOST = "lqptfesfsgpdjlzwxhbt.supabase.co"
THUMB_PATH = re.compile(r"/storage/v1/object/public/uploads/images/[0-9a-f-]{36}\.(?:jpe?g|png|webp|gif)", re.IGNORECASE)
THUMB_DAILY_CAP = 10
THUMB_BYTES = 1536 * 1024  # kw_mcp.notices.Limits.image_bytes


def margin(page) -> int:
    """Spare requests of a server the nightly sweep never takes: a tenth of its daily cap, at least one."""
    return max(1, int(page.daily_cap) // 10)


def file_wanted(name: str) -> bool:
    """A file whose text is read: a document kw_mcp.attachments reads, not named like a roster."""
    return bool(_FILE_OK.search(name)) and not _ROSTER_FILE.search(name)


def _file_skip(name: str) -> str | None:
    if not _FILE_OK.search(name):
        return "type"
    return "roster" if _ROSTER_FILE.search(name) else None


@dataclass
class Assets:
    """What a notice shows besides its text and what of it was read: images as (mime, bytes) and files as
    (name, text), only for the model call (never stored); the counts and fingerprint go on the notice.
    `prior_*` were read on an earlier try of this notice version (not asked for again); `read_keys` are the
    asset keys (asset_key) read this time."""
    images: list = field(default_factory=list)
    files: list = field(default_factory=list)
    found_images: int = 0
    found_files: int = 0
    read_files: int = 0  # files read, an image sent as a file included
    prior_images: int = 0
    prior_files: int = 0
    read_keys: set = field(default_factory=set)
    skipped_images: Counter = field(default_factory=Counter)
    skipped_files: Counter = field(default_factory=Counter)
    fp: str = ""
    # read, none (nothing to read), unchanged, privacy, no_spare, no_time, failed (reading failed half way),
    # list_failed (the page's own listing failed), host (a host kw_mcp.notices doesn't read yet)
    how: str = "none"

    def missing(self) -> int:
        """Images and files skipped for a reason worth another try."""
        return sum(n for k, n in (*self.skipped_images.items(), *self.skipped_files.items()) if k in TRANSIENT)

    def read_any(self) -> bool:
        return bool(self.images or self.read_files or self.prior_images or self.prior_files)

    def first_reason(self) -> str:
        for reason, n in (*self.skipped_images.items(), *self.skipped_files.items()):
            if n and reason in TRANSIENT:
                return reason
        return ""

    def counts(self) -> dict:
        return {"images": {"found": self.found_images, "read": len(self.images) + self.prior_images,
                           "skipped": {k: v for k, v in self.skipped_images.items() if v}},
                "files": {"found": self.found_files, "read": self.read_files + self.prior_files,
                          "skipped": {k: v for k, v in self.skipped_files.items() if v}}}


def _image_key(url: str) -> str:
    return f"i {url}"


def _file_key(name: str, url: str) -> str:
    return f"f {name} {urlsplit(str(url)).path}"  # no query: some boards put a per-view token there


def asset_key(key: str) -> str:
    """12 hex digits naming one image or file (what a queue row keeps of the assets already read)."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def fingerprint(image_urls, files) -> str:
    """16 hex digits over the sorted image URLs and the files' names and paths (no query: per-view tokens)."""
    keys = sorted({_image_key(u) for u in image_urls} | {_file_key(n, u) for n, u in files})
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16] if keys else ""


def listing(listed: dict, page, already: frozenset[str] = frozenset()) -> tuple[Assets, list[dict], list[dict]]:
    """(the counts so far, images to read, files to read) from what kw_mcp found on the page. `already`: asset
    keys (asset_key) read on an earlier try of this version; they count as read and are not planned again."""
    images, files = list(listed.get("images") or []), list(listed.get("attachments") or [])
    site_only = int(listed.get("files_on_site") or 0)
    got = Assets(found_images=len(images), found_files=len(files) + site_only,
                 fp=fingerprint([str(r.get("url")) for r in images],
                                [(str(r.get("name") or ""), str(r.get("url") or "")) for r in files]))
    if site_only:
        got.skipped_files["site_only"] += site_only
    plan_images, plan_files = [], []
    for row in images:
        if extract.page_for_host(urlsplit(str(row.get("url"))).hostname or "") is None:
            got.skipped_images["off_host"] += 1
        elif len(plan_images) + got.prior_images >= MAX_IMAGES:
            got.skipped_images["cap"] += 1
        elif asset_key(_image_key(str(row.get("url")))) in already:
            got.prior_images += 1
        else:
            plan_images.append(row)
    for row in files:
        name = str(row.get("name") or "")
        skip = "site_only" if not page.files else _file_skip(name)
        if skip is None and len(plan_files) + got.prior_files >= MAX_ATTACHMENTS:
            skip = "cap"
        if skip:
            got.skipped_files[skip] += 1
        elif asset_key(_file_key(name, str(row.get("url") or ""))) in already:
            got.prior_files += 1
        else:
            plan_files.append(row)
    return got, plan_images, plan_files


def _skip_all(got: Assets, images: list, files: list, reason: str) -> Assets:
    got.skipped_images[reason] += len(images)
    got.skipped_files[reason] += len(files)
    return got


class PoliteGate:
    """kw_mcp.notices' gate: the collectors' per-server gate in kw-public. Waits out the gap between two
    requests to a server; refuses a host the collectors don't read, a path robots.txt disallows, and a
    server whose daily cap has nothing to spare after today's list runs. A refusal carries its reason as
    the second argument (kw_mcp.notices.Refused.reason): off_host, budget, resting or time.

    A page run also passes `fetched` (the page it already has: kw_mcp asks for it, PageInHand hands it
    over, nothing goes out), `budget` (requests this notice may still make) and `deadline`
    (time.monotonic() by which any waiting must be over)."""

    def __init__(self, public, now: Callable[[], datetime], sleep=asyncio.sleep, refused=Exception,
                 extra_today: int = 0, gap_s: float | None = None, fetched: str | None = None,
                 budget: int | None = None, deadline: float | None = None, keep: bool = False):
        self.public, self.now, self.sleep, self.refused = public, now, sleep, refused
        self.keep = keep  # the sweep: each server keeps its margin() of spare requests
        self.extra_today = max(0, extra_today)
        # an operator's catch-up run, or a notice's own images and files, may come closer than the minute
        self.interval = timedelta(seconds=max(1.0, gap_s)) if gap_s else None
        self.fetched, self.budget, self.deadline = fetched, budget, deadline
        self.deny: set[str] = set()  # the page's images not planned this time (read before, over the cap)
        self.origins: set[str] = set()  # servers a request went to through this gate

    def extra(self, now: datetime) -> int:
        """Requests above our own daily cap, only while no list run is left today (weekends, or after the
        last weekday run): the collectors check the plain cap, so an extra taken earlier would cost them."""
        return self.extra_today if extract.runs_left(now) == 0 else 0

    async def before(self, url: str) -> None:
        if self.fetched is not None and url == self.fetched:
            return
        host = urlsplit(url).hostname or ""
        if url in self.deny:  # costs nothing: kw_mcp doesn't count it against its own tries either
            raise self.refused(f"{host}: 이번에는 읽지 않는 이미지입니다.", "done")
        page = extract.page_for_host(host)
        if page is None or not politeness.robots_allows(url):
            raise self.refused(f"{host}: 수집기가 읽지 않는 주소입니다.", "off_host")
        if self.budget is not None and self.budget < 1:
            raise self.refused(f"{host}: 이 공지에 쓸 요청을 다 썼습니다.", "budget")
        for _ in range(10):
            now = self.now()
            extra = self.extra(now)
            if extract.spare(self.public, page, now) + extra - (margin(page) if self.keep else 0) < 1:
                raise self.refused(f"{host}: 오늘 이 서버에 보낼 수 있는 요청을 다 썼습니다.", "budget")
            # a shorter gap never undercuts the host's robots.txt Crawl-delay (job.kw.ac.kr: 10 s)
            interval = self.interval and max(self.interval, timedelta(seconds=politeness.crawl_delay(url)))
            permit = politeness.acquire(self.public, page.origin, now, daily_cap=page.daily_cap + extra,
                                        interval=interval)
            if permit.granted:
                self.origins.add(page.origin)
                if self.budget is not None:
                    self.budget -= 1
                return
            if permit.reason not in ("too_soon", "raced"):
                raise self.refused(f"{host}: {permit.reason}", "budget" if permit.reason == "daily_cap" else "resting")
            wait = int(politeness.state(self.public, page.origin).get("next_at", 0)) - now.timestamp()
            if self.deadline is not None and time.monotonic() + wait > self.deadline - 2:
                raise self.refused(f"{host}: 차례를 기다릴 시간이 없습니다.", "time")
            await self.sleep(max(1.0, wait + 1))
        raise self.refused(f"{host}: 차례를 얻지 못했습니다.", "time")

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


def _fit(images: list[tuple[str, bytes]], pictures, kept: list | None = None) -> list[tuple[str, bytes]]:
    """`images` and then `pictures`, in order, while fewer than MAX_IMAGES and IMAGE_BYTES in all. `kept` gets
    the pictures that made it."""
    total = sum(len(data) for _, data in images)
    for p in pictures:
        if len(images) >= MAX_IMAGES:
            break
        if total + len(p.data) <= IMAGE_BYTES:
            images.append((p.mime, p.data))
            total += len(p.data)
            if kept is not None:
                kept.append(p)
    return images


async def read_parts(notice: dict, read, gate, transport: Callable[[], httpx.AsyncBaseTransport],
                     limits: Callable[[], object] | None = None, listed: dict | None = None,
                     already: frozenset[str] = frozenset()) -> tuple[str, Assets]:
    """(body, Assets) of one notice through `read` (kw_mcp.notices.read_notice): its images and files only
    when its title can't name people. `listed` is the page's listing when the caller has it (a page run);
    otherwise it is read first (kw_mcp keeps the page, so the images call asks for it no second time). Only
    the planned images go out: the gate refuses the page's other images (`deny`), those read on an earlier
    try (`already`) included. A refused or failed file request ends the files: the rest are skipped for the
    same reason."""
    from kw_mcp.notices import NoticeError, Refused

    def more() -> dict:
        return {} if limits is None else {"limits": limits()}

    page, url = extract.page_for(notice)
    plain = not name_risk(str(notice.get("category_id") or ""), str(notice.get("title") or ""))
    first = None
    if listed is None:
        first = await read(url, gate=gate, transport=transport(), **more())
        listed = first.data
    got, plan_images, plan_files = listing(listed, page, already)
    if not plain:
        got.how = "privacy"
        return unwrap(listed.get("body")), _skip_all(got, plan_images, plan_files, "privacy")
    planned = [str(r.get("url")) for r in plan_images]
    if hasattr(gate, "deny"):
        gate.deny = {str(r.get("url")) for r in listed.get("images") or []} - set(planned)
    answer = await read(url, images=True, gate=gate, transport=transport(), **more()) if planned else first
    rows = {str(r.get("url")): r for r in ((answer.data if answer else listed).get("images") or [])}
    shown = [p for p in (answer.pictures if answer else []) if p.url in set(planned)]
    kept: list = []
    got.images = _fit([], shown, kept)
    got.read_keys |= {asset_key(_image_key(p.url)) for p in kept}
    got.skipped_images["total_size"] += len(shown) - len(got.images)
    for want in set(planned) - {p.url for p in shown}:  # not fetched: kw_mcp says why (no code: past its cap)
        got.skipped_images[str((rows.get(want) or {}).get("skip") or "cap")] += 1
    for i, row in enumerate(plan_files):
        name = str(row.get("name") or "")
        text, part, parts, why, as_image = "", 1, 1, None, False
        while part <= parts and len(text) < ATTACHMENT_CHARS:
            try:
                answered = await read(url, attachment=int(row["index"]), part=part, gate=gate, transport=transport(),
                                      **more())
            except Refused as e:  # no spare request, no time: this file and the rest wait for another try
                why = getattr(e, "reason", "refused")
                break
            except NoticeError:  # an unreadable or missing file is skipped for good
                why = "unreadable"
                break
            if answered.pictures:  # an image attached as a file
                before = len(got.images)
                got.images = _fit(got.images, answered.pictures)
                as_image, why = len(got.images) > before, None if len(got.images) > before else "total_size"
                break
            text += unwrap(answered.data.get("text")) + "\n"
            part, parts = part + 1, int(answered.data.get("total_parts") or 1)
        if why in TRANSIENT:
            got.skipped_files[why] += len(plan_files) - i
            break
        if why:
            got.skipped_files[why] += 1
        elif as_image or text.strip():
            if text.strip():
                got.files.append((name, text[:ATTACHMENT_CHARS]))
            got.read_files += 1
            got.read_keys.add(asset_key(_file_key(name, str(row.get("url") or ""))))
        else:  # read, but nothing in it
            got.skipped_files["empty"] += 1
    got.how = "read"
    return unwrap(listed.get("body")), got


def page_run(table, notice: dict, page, url: str, html: str, clock: Callable[[], datetime],
             time_left: Callable[[], float] | None = None, read=None,
             inner_transport: Callable[[], httpx.AsyncBaseTransport] | None = None, sleep=None,
             skip: str | None = None, unchanged: Callable[[str], bool] | None = None,
             already: frozenset[str] = frozenset(), keep: bool = False) -> Assets:
    """What a notice whose page (`html`, from `url` on `page`'s server) a page run just fetched shows and
    what of it was read. The listing costs no request. skip: read nothing, and count everything planned as
    skipped for this reason ("privacy" for a title that may name people, "budget" when the page took the
    last spare request). unchanged(fp): True when the notice was read before with the same text and these
    assets, so nothing is asked for (how "unchanged"). how is also "host" for a host kw_mcp.notices doesn't
    read yet (a board collected ahead of the extension's release: the body goes alone). The caller gives the
    page's server its minute back afterwards. already: asset keys read on an earlier try of this notice
    version: counted as read, not asked for again. keep (the nightly sweep): every server asked keeps its
    margin() of spare requests untouched, and the slack is asked for up front."""
    from kw_mcp import notices

    try:
        fetched = notices.check_url(url)
    except notices.NoticeError:  # a board collected ahead of the released kw-mcp's host list: the body goes alone
        log("extract_assets_skipped", notice=notice.get("SK"), reason="host")
        metrics(ExtractAssetsSkipped=1)
        return Assets(how="host")
    left = ASSET_DEADLINE_S if time_left is None else min(ASSET_DEADLINE_S, time_left() - extract.MODEL_S)
    gate = PoliteGate(table, clock, sleep or asyncio.sleep, refused=notices.Refused, gap_s=politeness.ASSET_GAP_S,
                      fetched=fetched, deadline=time.monotonic() + max(left, 0), keep=keep)
    read = read or notices.read_notice

    def transport() -> httpx.AsyncBaseTransport:
        return PageInHand(gate.fetched, html,
                          WatchedTransport(table, clock, inner_transport() if inner_transport else None))

    def limits():
        return notices.Limits(file_bytes=FILE_BYTES, deadline_s=max(0.0, gate.deadline - time.monotonic()))

    held: dict = {}  # the listing, kept if reading fails half way

    async def go() -> Assets:
        listed = (await read(url, gate=gate, transport=transport(), limits=limits())).data  # from memory
        got, plan_images, plan_files = listing(listed, page, already)
        held.update(got=got, images=plan_images, files=plan_files)
        if unchanged is not None and unchanged(got.fp):
            got.how = "unchanged"
            return got
        if name_risk(str(notice.get("category_id") or ""), str(notice.get("title") or "")):
            got.how = "privacy"
            return _skip_all(got, plan_images, plan_files, "privacy")
        if not plan_images and not plan_files:
            return got
        need = len(plan_images) + len(plan_files) + (margin(page) + ASSET_SLACK if keep else 0)
        why = skip or ("time" if left < ASSET_MIN_S else "budget" if extract.spare(table, page, clock()) < need else None)
        if why:
            got.how = {"privacy": "privacy", "time": "no_time"}.get(why, "no_spare")
            return _skip_all(got, plan_images, plan_files, why)
        gate.budget = len(plan_images) + len(plan_files) + ASSET_SLACK
        _, read_got = await read_parts(notice, read, gate, transport, limits, listed=listed, already=already)
        return read_got

    try:
        got = asyncio.run(go())
    except Exception as e:  # noqa: BLE001 - the images and files are extra: the body still goes
        log("extract_assets_failed", notice=notice.get("SK"), error=e.__class__.__name__)
        if held:  # listed, then failed while reading: everything planned is worth another try
            got = _skip_all(Assets(found_images=held["got"].found_images, found_files=held["got"].found_files,
                                   prior_images=held["got"].prior_images, prior_files=held["got"].prior_files,
                                   skipped_images=Counter({k: v for k, v in held["got"].skipped_images.items()}),
                                   skipped_files=Counter({k: v for k, v in held["got"].skipped_files.items()}),
                                   fp=held["got"].fp), held["images"], held["files"], "error")
            got.how = "failed"
        else:  # not even listed: what the page shows is unknown (final: the text still goes)
            got = Assets(how="list_failed")
    finally:
        gate.rest()
    if got.how not in ("read", "none", "unchanged"):
        log("extract_assets_skipped", notice=notice.get("SK"), reason=got.how)
        metrics(ExtractAssetsSkipped=1)
    return got


# --- 총학생회's poster thumbnails ------------------------------------------------------------------------

def thumbnail_ok(url: str) -> bool:
    """Exactly THUMB_HOST, https, no port, user, password, query or fragment, a path under THUMB_PATH."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == THUMB_HOST and port is None and parts.username is None
            and parts.password is None and "@" not in parts.netloc and not parts.query and not parts.fragment
            and THUMB_PATH.fullmatch(parts.path) is not None)


THUMB_DEADLINE_S = 20.0  # the whole download, headers and body: a trickling server can't hold the run


def read_thumbnail(table, url: str, clock: Callable[[], datetime],
                   transport: httpx.BaseTransport | None = None) -> tuple[list[tuple[str, bytes]], str | None]:
    """([(mime, bytes)], None) for one 총학생회 poster, or ([], why skipped). One request through the host's own
    gate; a redirect is not followed; no compression asked for, the bytes read as sent; an image/* answer of at
    most THUMB_BYTES (Content-Length checked first), within THUMB_DEADLINE_S; not an image or icon-sized:
    skipped."""
    from kw_mcp.notices import _tiny, image_info

    if not thumbnail_ok(url) or not politeness.robots_allows(url):
        return [], "off_host"
    permit = politeness.acquire(table, THUMB_HOST, clock(), daily_cap=THUMB_DAILY_CAP)
    if not permit.granted:
        return [], "resting" if permit.reason in ("cooldown", "disabled_after_blocks") else "budget"
    deadline = time.monotonic() + THUMB_DEADLINE_S
    try:
        with (httpx.Client(transport=transport, follow_redirects=False,
                           timeout=httpx.Timeout(10.0, connect=5.0),
                           headers={"User-Agent": USER_AGENT, "Accept": "image/*",
                                    "Accept-Encoding": "identity"}) as client,
              client.stream("GET", url) as r):
            if r.status_code in (403, 429) or r.status_code >= 500:
                politeness.record_block(table, THUMB_HOST, clock(),
                                        {403: "forbidden", 429: "rate_limit"}.get(r.status_code, "server_error"))
                return [], "resting" if r.status_code in (403, 429) else "error"
            if r.status_code != 200:  # a redirect is never followed; a 404 is gone for good
                return [], "redirect" if 300 <= r.status_code < 400 else "gone"
            if not (r.headers.get("content-type") or "").lower().startswith("image/"):
                return [], "not_image"
            length = r.headers.get("content-length", "")
            if length.isdigit() and int(length) > THUMB_BYTES:
                return [], "too_large"
            body = b""
            for chunk in r.iter_raw():
                body += chunk
                if len(body) > THUMB_BYTES:
                    return [], "too_large"
                if time.monotonic() > deadline:
                    return [], "time"
    except httpx.HTTPError:
        politeness.record_block(table, THUMB_HOST, clock(), "transport_failure")
        return [], "error"
    politeness.record_success(table, THUMB_HOST)
    info = image_info(body)
    if info is None:
        return [], "not_image"
    if _tiny(info[1], info[2]):
        return [], "icon"
    return [(info[0], body)], None

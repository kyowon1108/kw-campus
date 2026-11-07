"""The list page of a gnuboard 5 board, on any site: each row's post number (wr_id), title and date.

Skins differ (gnuboard's basic skin is a table; 총동문회's is a div per row with .e_num, .e_subject and
.e_date), but every row links its post as board.php?bo_table=<table>&wr_id=<n> and shows its date in a
cell of its own (.e_date, .td_datetime or .td_date). A row here is the nearest ancestor of a post link
that holds such a cell and links no other post.

The list shows "HH:MM" for today's posts, then "MM-DD" with no year (or "YY-MM-DD" / "YYYY-MM-DD" in
some skins). wr_id only grows (posts and comments share it), so the rows are dated in wr_id order, newest
first: a date later in the year than the newer row's is from the year before. A pinned row (no number in
its number cell, or the basic skin's tr.bo_notice) may sit anywhere on the page; wr_id order places it.
A gap of more than a year between two neighbouring posts can't be seen and dates the older one a year too
late; a board that posts every month or so never has one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from kwcampus.kwnotice.redaction import redact_text

_DATE_CELL = ".e_date, .td_datetime, .td_date"
_NUMBER_CELL = ".e_num, .td_num, .td_num2"
_FILE_MARK = ".fa-download, .fa-paperclip"
_ROW_DEPTH = 6  # levels above a post link where its row may start
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_DATE = re.compile(r"(?<!\d)(?:(\d{4}|\d{2})[-./])?(\d{1,2})[-./](\d{1,2})(?!\d)")


@dataclass(frozen=True)
class Row:
    wr_id: int
    title: str  # masked (redaction.redact_text), at most 300 characters
    posted: date
    pinned: bool
    files: bool  # the row shows a download or clip icon


def full_date(text: str) -> date | None:
    """A date written with its year ("26-08-28 14:06", "2026-08-28"), as a post page shows it; None otherwise."""
    match = _DATE.search(text or "")
    if match is None or not match.group(1):
        return None
    year = int(match.group(1))
    try:
        return date(year + 2000 if year < 100 else year, int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def post_url(host: str, table: str, wr_id: int | str) -> str:
    return f"https://{host}/bbs/board.php?bo_table={table}&wr_id={wr_id}"


def _post_of(href: str, host: str, table: str) -> int | None:
    """The wr_id a link opens on this board, or None (another board, another site, a list link)."""
    try:
        target = urlsplit(urljoin(f"https://{host}/bbs/board.php", href.strip()))
        query, name = parse_qs(target.query), target.hostname
    except ValueError:  # a broken link ("http://[x", say) is no post
        return None
    ids = query.get("wr_id") or []
    if (name != host or not target.path.endswith("/board.php") or query.get("bo_table") != [table]
            or len(ids) != 1 or not ids[0].isdigit() or len(ids[0]) > 12):
        return None
    return int(ids[0])


def _row_of(link, wr_id: int, host: str, table: str):
    node = link
    for _ in range(_ROW_DEPTH):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            return None
        if node.select_one(_DATE_CELL) is None:
            continue
        others = {_post_of(a["href"], host, table) for a in node.find_all("a", href=True)} - {None}
        return node if others == {wr_id} else None
    return None


def _date(text: str, today: date, year: int, newer: date | None) -> date | None:
    """A row's date: today for "HH:MM", the date itself when it has a year, else the latest date on or
    before the newer row's (today for the first row) with that month and day."""
    match = _DATE.search(text)
    if match is None:
        return today if _TIME.search(text) else None
    month, day = int(match.group(2)), int(match.group(3))
    if match.group(1):
        full = int(match.group(1))
        try:
            return date(full + 2000 if full < 100 else full, month, day)
        except ValueError:
            return None
    limit = newer or today
    for y in range(year, year - 5, -1):  # 02-29 skips to the last leap year
        try:
            when = date(y, month, day)
        except ValueError:
            continue
        if when <= limit:
            return when
    return None


def parse_list(html: str, host: str, table: str, today: date) -> list[Row]:
    """The rows of one list page, newest (highest wr_id) first. `today` is the KST date."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[int, tuple[str, str, bool, bool]] = {}
    for link in soup.find_all("a", href=True):
        wr_id = _post_of(link["href"], host, table)
        if wr_id is None or wr_id in found:
            continue  # a second link of the same row (its comment count)
        row = _row_of(link, wr_id, host, table)
        title = redact_text(" ".join(link.get_text(" ").split()))[:300]
        if row is None or not title:
            continue
        number = row.select_one(_NUMBER_CELL)
        pinned = "bo_notice" in (row.get("class") or []) or (
            number is not None and not number.get_text(strip=True).replace(",", "").isdigit())
        found[wr_id] = (title, row.select_one(_DATE_CELL).get_text(" ", strip=True), pinned,
                        row.select_one(_FILE_MARK) is not None)
    rows, newer, year = [], None, today.year
    for wr_id in sorted(found, reverse=True):
        title, text, pinned, files = found[wr_id]
        posted = _date(text, today, year, newer)
        if posted is None:
            continue
        newer, year = posted, posted.year
        rows.append(Row(wr_id, title, posted, pinned, files))
    return rows

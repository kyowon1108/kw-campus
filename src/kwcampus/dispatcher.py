"""Lambda: turn kw-public stream records into Discord jobs on SQS.

- INSERT of a notice → "post" (skipped for seed rows, old pinned posts and the FAQ).
- MODIFY that changed the title, the 수정일 (updated_date) or the category → "edit" with what
  changed, addressed to the channel the notice was posted to (its old category's channel). The
  main board and the job board have a 수정일; department boards and the aicon widget only carry
  their 등록일 there, so it changes only when a post is reused.
  - quiet (fix the message, no "수정됨" note): only the category moved, or our own redaction changed
    the title: norm_v differs, the site's 수정일 stayed the same, and today's rules explain the new
    title. They do when they turn the old title into the new one, or when the new one is the old one
    with some name masks undone (older rules masked a word that was no name, "(최종본)"): the rest
    of the title must be the same, and each undone mask one 2-4 syllable Hangul word.
    An item stored before norm_v existed gets it on its first school edit, which therefore still
    counts as a real edit.
  - "repost": a department board gave an old post (same UID) a new title and a 등록일 more than
    30 days later. The sender edits it if we posted it, else posts it as new.
- any other MODIFY that moved the notice's ttl (pinned or department changed) → "keep": the sender
  moves the delivery record's ttl along, without calling Discord.
A notice taken down by hand (removed=true, scripts/collector_admin.py takedown) gets no job at all.
The facts extract.py writes onto a notice (extracted, body_hash, ...) change nothing the message shows
and move no ttl, so that MODIFY makes no job either. D-day reminders come from reminders.py, which
sends its "remind" jobs to the same queue with send_jobs.
While the /kw/collector/notify switch is off nothing reaches Discord: only "keep" jobs go out (for
every MODIFY that moved the ttl), so the delivery records still follow their notices.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import boto3
from boto3.dynamodb.types import TypeDeserializer

from kwcampus import params
from kwcampus.config import DISCORD_QUEUE_URL, KST, NOTIFY_ENABLED_PARAM, PINNED_MAX_AGE
from kwcampus.kwnotice.redaction import NAME, redact_text
from kwcampus.obs import log, metrics
from kwcampus.routing import channel_for

_deserializer = TypeDeserializer()
# What a posted message shows and so may need fixing. Department or pinned changes also rewrite
# the item (and move its ttl), but the message doesn't show them.
EDIT_FIELDS = ("title", "updated_date", "category_id")
SCHOOL_FIELDS = ("title", "updated_date")  # changes the school made that readers hear about
REPOST_JUMP = timedelta(days=30)
SEND_TRIES = 3
_MASK = re.compile(r"\[REDACTED_[A-Z_]+\]")
_NAME_WORD = re.compile(r"[가-힣]{2,4}")
# A title that is mostly masks says too little to tell our change from the school's.
_UNMASK_MIN_KEPT = 4


def _image(record: dict, key: str) -> dict:
    raw = record.get("dynamodb", {}).get(key) or {}
    return {k: _deserializer.deserialize(v) for k, v in raw.items()}


def _is_old_pinned(item: dict, today: date) -> bool:
    if not item.get("pinned"):
        return False
    posted = date.fromisoformat(str(item.get("posted_date")))
    return (today - posted) > PINNED_MAX_AGE


def _latest(item: dict) -> date | None:
    days = []
    for field in ("posted_date", "updated_date"):
        try:
            days.append(date.fromisoformat(str(item.get(field))))
        except ValueError:
            continue
    return max(days, default=None)


def _is_repost(old: dict, new: dict) -> bool:
    if not str(new.get("PK", "")).startswith("SRC#dept-") or old.get("title") == new.get("title"):
        return False
    before, after = _latest(old), _latest(new)
    return bool(before and after and after - before > REPOST_JUMP)


def _edit_job(key: dict, old: dict, new: dict, channel: str) -> dict | None:
    changed = [field for field in EDIT_FIELDS if old.get(field) != new.get(field)]
    if not changed:
        return None
    if _is_repost(old, new):
        return {"action": "repost", "key": key, "channel": channel, "changed": ["title", "updated_date"]}
    # the message lives in the channel of the category it was posted under
    posted_in = channel_for(str(old.get("category_id", ""))) or channel
    job = {"action": "edit", "key": key, "channel": posted_in, "changed": changed}
    school = any(field in changed for field in SCHOOL_FIELDS)
    if not school or _our_redaction(old, new):
        job["quiet"] = True
    return job


def _undone_name(new_title: str, position: int, literal: str) -> int | None:
    """Where a word an older rule masked as a name ends in new_title: one 2-4 syllable Hangul word at
    `position` with `literal` (the rest of the title up to the next mask) right after it."""
    for length in (4, 3, 2):
        end = position + length
        if (end <= len(new_title) and _NAME_WORD.fullmatch(new_title, position, end)
                and new_title.startswith(literal, end)):
            return end
    return None


def _explained_by_masks(old_title: str, new_title: str) -> bool:
    """new_title is old_title (as today's rules mask it) with each mask either kept or undone: older
    rules masked as a name a word today's rules leave ("(최종본)"), and the rest of the title is the
    same. A linear walk: the text between masks must match exactly, a name mask may stand for one
    2-4 syllable Hangul word, and any other mask must stay as it is."""
    redone = redact_text(old_title)
    if redone == new_title:
        return True
    parts, masks = _MASK.split(redone), _MASK.findall(redone)
    if not masks or sum(len(p.strip()) for p in parts) < _UNMASK_MIN_KEPT:
        return False
    if not new_title.startswith(parts[0]):
        return False
    position = len(parts[0])
    for mask, literal in zip(masks, parts[1:], strict=True):
        if new_title.startswith(mask, position):
            position += len(mask)
        elif mask == NAME and (end := _undone_name(new_title, position, literal)) is not None:
            position = end
        else:
            return False
        if not new_title.startswith(literal, position):
            return False
        position += len(literal)
    return position == len(new_title)


def _our_redaction(old: dict, new: dict) -> bool:
    """The title changed only because our redaction rules did, not because the school edited it."""
    return (old.get("norm_v") != new.get("norm_v")
            and old.get("updated_date") == new.get("updated_date")
            and _explained_by_masks(str(old.get("title") or ""), str(new.get("title") or "")))


def _keep_job(key: dict, old: dict, new: dict, channel: str) -> dict | None:
    """The notice's ttl moved (store.upsert moves it on every change) with nothing the message shows
    (pinned, department), or while notifications are off: the delivery record's ttl must move too, or
    it expires before the notice and a takedown can no longer find the message. No Discord call."""
    if old.get("ttl") == new.get("ttl"):
        return None
    return {"action": "keep", "key": key, "channel": channel_for(str(old.get("category_id", ""))) or channel}


def jobs_for(records: list[dict], today: date, notify: bool = True) -> list[dict]:
    """Discord jobs for these stream records; with notify off, only the "keep" jobs."""
    jobs = []
    for record in records:
        new = _image(record, "NewImage")
        if not str(new.get("PK", "")).startswith("SRC#") or not str(new.get("SK", "")).startswith("N#"):
            continue
        if new.get("removed"):  # taken down by hand: never announced or edited again
            continue
        key = {"PK": new["PK"], "SK": new["SK"]}
        channel = channel_for(str(new.get("category_id", "")))
        if channel is None:  # searchable only (FAQ)
            continue
        if record["eventName"] == "INSERT":
            if not notify or new.get("seed") or _is_old_pinned(new, today):
                continue
            jobs.append({"action": "post", "key": key, "channel": channel})
        elif record["eventName"] == "MODIFY":
            old = _image(record, "OldImage")
            if job := (_edit_job(key, old, new, channel) if notify else None) or _keep_job(key, old, new, channel):
                jobs.append(job)
    return jobs


def send_jobs(sqs, jobs: list[dict]) -> None:
    """Up to 10 jobs; entries SQS rejects are sent again a couple of times before giving up."""
    entries = [{"Id": str(i), "MessageBody": json.dumps(job)} for i, job in enumerate(jobs)]
    for _ in range(SEND_TRIES):
        failed = {f["Id"] for f in sqs.send_message_batch(QueueUrl=DISCORD_QUEUE_URL, Entries=entries)
                  .get("Failed") or []}
        entries = [e for e in entries if e["Id"] in failed]
        if not entries:
            return
    # Retry the whole stream batch; the sender's delivery records make re-sends idempotent.
    raise RuntimeError(f"SQS rejected {len(entries)} Discord jobs")


def handler(event, context):
    records = event.get("Records", [])
    notify = params.is_true(NOTIFY_ENABLED_PARAM)
    jobs = jobs_for(records, datetime.now(KST).date(), notify)  # posted_date is a KST date
    if jobs:
        sqs = boto3.client("sqs")
        for start in range(0, len(jobs), 10):
            send_jobs(sqs, jobs[start : start + 10])
    if not notify:  # only "keep" jobs went out: nothing reaches Discord
        log("dispatch_skip", reason="notify_off", records=len(records), keep=len(jobs))
        return {"batchItemFailures": []}
    log("dispatch_ok", records=len(records), jobs=len(jobs))
    metrics(DiscordJobs=len(jobs))
    return {"batchItemFailures": []}

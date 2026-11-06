"""Lambda: turn kw-public stream records into Discord jobs on SQS.

- INSERT of a notice → "post" (skipped for seed rows, old pinned posts and the FAQ).
- MODIFY that changed the title, the 수정일 (updated_date) or the category → "edit" with what
  changed, addressed to the channel the notice was posted to (its old category's channel). The
  main board and the job board have a 수정일; department boards and the aicon widget only carry
  their 등록일 there, so it changes only when a post is reused.
  - quiet (fix the message, no "수정됨" note): only the category moved, or our own redaction changed
    the title: norm_v differs, today's rules turn the old title into the new one, and the site's
    수정일 stayed the same. An item stored before norm_v existed gets it on its first school edit,
    which therefore still counts as a real edit.
  - "repost": a department board gave an old post (same UID) a new title and a 등록일 more than
    30 days later. The sender edits it if we posted it, else posts it as new.
Nothing is sent while the /kw/collector/notify switch is off.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import boto3
from boto3.dynamodb.types import TypeDeserializer

from kwcampus import params
from kwcampus.config import DISCORD_QUEUE_URL, KST, NOTIFY_ENABLED_PARAM, PINNED_MAX_AGE
from kwcampus.kwnotice.redaction import redact_text
from kwcampus.obs import log, metrics
from kwcampus.routing import channel_for

_deserializer = TypeDeserializer()
# What a posted message shows and so may need fixing. Department or pinned changes also rewrite
# the item, but the message doesn't show them.
EDIT_FIELDS = ("title", "updated_date", "category_id")
SCHOOL_FIELDS = ("title", "updated_date")  # changes the school made that readers hear about
REPOST_JUMP = timedelta(days=30)
SEND_TRIES = 3


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


def _our_redaction(old: dict, new: dict) -> bool:
    """The title changed only because our redaction rules did, not because the school edited it."""
    return (old.get("norm_v") != new.get("norm_v")
            and old.get("updated_date") == new.get("updated_date")
            and redact_text(str(old.get("title") or "")) == new.get("title"))


def jobs_for(records: list[dict], today: date) -> list[dict]:
    jobs = []
    for record in records:
        new = _image(record, "NewImage")
        if not str(new.get("PK", "")).startswith("SRC#") or not str(new.get("SK", "")).startswith("N#"):
            continue
        key = {"PK": new["PK"], "SK": new["SK"]}
        channel = channel_for(str(new.get("category_id", "")))
        if channel is None:  # searchable only (FAQ)
            continue
        if record["eventName"] == "INSERT":
            if new.get("seed") or _is_old_pinned(new, today):
                continue
            jobs.append({"action": "post", "key": key, "channel": channel})
        elif record["eventName"] == "MODIFY" and (job := _edit_job(key, _image(record, "OldImage"), new, channel)):
            jobs.append(job)
    return jobs


def _send(sqs, jobs: list[dict]) -> None:
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
    if not params.is_true(NOTIFY_ENABLED_PARAM):
        log("dispatch_skip", reason="notify_off", records=len(records))
        return {"batchItemFailures": []}
    jobs = jobs_for(records, datetime.now(KST).date())  # posted_date is a KST date
    sqs = boto3.client("sqs")
    for start in range(0, len(jobs), 10):
        _send(sqs, jobs[start : start + 10])
    log("dispatch_ok", records=len(records), jobs=len(jobs))
    metrics(DiscordJobs=len(jobs))
    return {"batchItemFailures": []}

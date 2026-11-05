"""Lambda: turn kw-public stream records into Discord jobs on SQS.

- INSERT of a notice → "post" (skipped for seed rows and old pinned posts)
- MODIFY that changed the title → "edit"
Nothing is sent while the /kw/collector/notify switch is off.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import boto3
from boto3.dynamodb.types import TypeDeserializer

from kwcampus import params
from kwcampus.config import DISCORD_QUEUE_URL, NOTIFY_ENABLED_PARAM, PINNED_MAX_AGE
from kwcampus.obs import log, metrics
from kwcampus.routing import channel_for

_deserializer = TypeDeserializer()


def _image(record: dict, key: str) -> dict:
    raw = record.get("dynamodb", {}).get(key) or {}
    return {k: _deserializer.deserialize(v) for k, v in raw.items()}


def _is_old_pinned(item: dict, today: date) -> bool:
    if not item.get("pinned"):
        return False
    posted = date.fromisoformat(str(item.get("posted_date")))
    return (today - posted) > PINNED_MAX_AGE


def jobs_for(records: list[dict], today: date) -> list[dict]:
    jobs = []
    for record in records:
        new = _image(record, "NewImage")
        if not str(new.get("PK", "")).startswith("SRC#") or not str(new.get("SK", "")).startswith("N#"):
            continue
        key = {"PK": new["PK"], "SK": new["SK"]}
        channel = channel_for(str(new.get("category_id", "")))
        if record["eventName"] == "INSERT":
            if new.get("seed") or _is_old_pinned(new, today):
                continue
            jobs.append({"action": "post", "key": key, "channel": channel})
        elif record["eventName"] == "MODIFY":
            old = _image(record, "OldImage")
            if old.get("title") != new.get("title"):
                jobs.append({"action": "edit", "key": key, "channel": channel})
    return jobs


def handler(event, context):  # noqa: ARG001
    records = event.get("Records", [])
    if not params.is_true(NOTIFY_ENABLED_PARAM):
        log("dispatch_skip", reason="notify_off", records=len(records))
        return {"batchItemFailures": []}
    jobs = jobs_for(records, datetime.now(UTC).date())
    sqs = boto3.client("sqs")
    for start in range(0, len(jobs), 10):
        batch = jobs[start : start + 10]
        response = sqs.send_message_batch(
            QueueUrl=DISCORD_QUEUE_URL,
            Entries=[{"Id": str(i), "MessageBody": json.dumps(job)} for i, job in enumerate(batch)],
        )
        if response.get("Failed"):
            # Retry the whole stream batch; the sender's delivery records make re-sends idempotent.
            raise RuntimeError(f"SQS rejected {len(response['Failed'])} Discord jobs")
    log("dispatch_ok", records=len(records), jobs=len(jobs))
    metrics(DiscordJobs=len(jobs))
    return {"batchItemFailures": []}

"""Structured logs and CloudWatch metrics (Embedded Metric Format) without personal data.

Only counters and short status codes are logged. Notice titles, Discord message
content, tokens, and HTTP bodies never go to the logs.
"""

from __future__ import annotations

import json
import time

NAMESPACE = "KwCampus"


def log(event: str, **fields: object) -> None:
    # flush: behind Lambda Web Adapter stdout is a pipe, so unflushed lines sit in the buffer and
    # are lost when the sandbox is frozen or recycled (security events went missing this way).
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str), flush=True)


def metrics(**values: int) -> None:
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {"Namespace": NAMESPACE, "Dimensions": [[]], "Metrics": [{"Name": k} for k in values]}
                    ],
                },
                **values,
            }
        ),
        flush=True,
    )

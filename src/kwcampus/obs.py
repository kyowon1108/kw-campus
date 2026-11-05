"""Structured logs and CloudWatch metrics (Embedded Metric Format) without personal data.

Only counters and short status codes are logged. Notice titles, Discord message
content, tokens, and HTTP bodies never go to the logs.
"""

from __future__ import annotations

import json
import time

NAMESPACE = "KwCampus"


def log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


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
        )
    )

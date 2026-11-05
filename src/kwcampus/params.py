"""Small cached SSM Parameter Store reads."""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError

_CACHE: dict[str, tuple[float, str | None]] = {}
_TTL_S = 60


def _client():
    return boto3.client("ssm")


def get(name: str, *, decrypt: bool = False, ttl_s: float = _TTL_S) -> str | None:
    """Parameter value, or None when the parameter does not exist."""
    hit = _CACHE.get(name)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    try:
        value = _client().get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            raise
        value = None
    _CACHE[name] = (time.monotonic(), value)
    return value


def is_true(name: str) -> bool:
    """Switches are on only when explicitly set to "true"; a missing parameter means off."""
    return (get(name, ttl_s=0) or "").strip().lower() == "true"


def clear_cache() -> None:
    _CACHE.clear()

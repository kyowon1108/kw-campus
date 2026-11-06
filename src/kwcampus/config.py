"""Names shared by the Lambda functions and the CDK stack."""

from __future__ import annotations

import os
from datetime import timedelta, timezone

KST = timezone(timedelta(hours=9))

PUBLIC_TABLE = os.environ.get("KW_PUBLIC_TABLE", "kw-public")
DELIVERY_TABLE = os.environ.get("KW_DELIVERY_TABLE", "kw-delivery")
DISCORD_QUEUE_URL = os.environ.get("KW_DISCORD_QUEUE_URL", "")

# Human-owned switches and secrets live in SSM and are never created by CDK,
# so a redeploy can't silently turn collection back on or overwrite a token.
COLLECTOR_ENABLED_PARAM = "/kw/collector/enabled"  # "true" to collect; missing = off
NOTIFY_ENABLED_PARAM = "/kw/collector/notify"  # "true" to send Discord alerts; missing = off
DISCORD_TOKEN_PARAM = "/kw/discord/bot-token"  # SecureString
DISCORD_CHANNELS_PARAM = "/kw/discord/channels"  # String: JSON {channel_key: channel_id}
DISCORD_PUBLIC_KEY_PARAM = "/kw/discord/public-key"  # the app's Ed25519 public key (hex), put in by hand
# Written by scripts/discord_setup.py: {"guild": id, "roles": {topic: role_id}, "menu": {"channel", "message"}}
DISCORD_ROLES_PARAM = "/kw/discord/roles"

USER_AGENT = (
    "kw-campus/0.1 (+https://github.com/kyowon1108/kw-notice-mcp; "
    "student non-commercial notice alerts)"
)

NOTICE_SOURCE = "kw-notice"  # the www.kw.ac.kr main notice board
NOTICE_TTL = timedelta(days=120)
DELIVERY_TTL = timedelta(days=120)
# A pinned post seen for the first time is only announced if it is this recent.
PINNED_MAX_AGE = timedelta(days=3)

# Posts between 22:00 and 08:00 KST go out without the topic role mention (no pings at night).
QUIET_FROM_HOUR, QUIET_UNTIL_HOUR = 22, 8
# An edit this soon after the post only updates the message; later ones also get a "수정됨" reply,
# at most one reply per notice per KST day.
EDIT_REPLY_AFTER = timedelta(minutes=30)

#!/usr/bin/env python3
"""CDK app for kw-campus. Deploy: npx aws-cdk@2.1147.0 deploy (see README)."""

from pathlib import Path

import aws_cdk as cdk

from kw_campus_stack import KwCampusStack

# Alarm e-mail stays out of git: --context alarmEmail=... or ~/.config/kw-campus/alarm-email.
# It must be given on every deploy, or the SNS subscription is removed.
ALARM_EMAIL_FILE = Path.home() / ".config" / "kw-campus" / "alarm-email"

app = cdk.App()
alarm_email = app.node.try_get_context("alarmEmail") or (
    ALARM_EMAIL_FILE.read_text().strip() if ALARM_EMAIL_FILE.exists() else None)
KwCampusStack(
    app,
    "KwCampus",
    asset_path=app.node.try_get_context("assetPath") or "../build/lambda",
    alarm_email=alarm_email,
    env=cdk.Environment(region="ap-northeast-2"),
    description="kw-campus: notice alerts, public MCP, personal KLAS bridge",
)
cdk.Tags.of(app).add("project", "kw-campus")
app.synth()

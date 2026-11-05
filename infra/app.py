#!/usr/bin/env python3
"""CDK app for kw-campus. Deploy: npx aws-cdk@2.1147.0 deploy (see README)."""

import aws_cdk as cdk

from kw_campus_stack import KwCampusStack

app = cdk.App()
KwCampusStack(
    app,
    "KwCampus",
    asset_path=app.node.try_get_context("assetPath") or "../build/lambda",
    alarm_email=app.node.try_get_context("alarmEmail"),
    env=cdk.Environment(region="ap-northeast-2"),
    description="kw-campus: notice alerts, public MCP, personal KLAS bridge",
)
cdk.Tags.of(app).add("project", "kw-campus")
app.synth()

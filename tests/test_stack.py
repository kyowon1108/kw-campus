import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).parents[1] / "infra"))
from kw_campus_stack import KwCampusStack


def template(tmp_path) -> Template:
    (tmp_path / "placeholder.py").write_text("")
    app = cdk.App()
    stack = KwCampusStack(app, "Test", asset_path=str(tmp_path), env=cdk.Environment(region="ap-northeast-2"))
    return Template.from_stack(stack)


def test_functions_and_tables(tmp_path):
    t = template(tmp_path)
    t.resource_count_is("AWS::Lambda::Function", 8)
    t.resource_count_is("AWS::DynamoDB::Table", 3)
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "TableName": "kw-public",
        "StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"},
        "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
    })


def test_schedule_is_weekday_daytime_kst_without_retries(tmp_path):
    template(tmp_path).has_resource_properties("AWS::Scheduler::Schedule", {
        "ScheduleExpression": "cron(0/20 9-18 ? * MON-FRI *)",
        "ScheduleExpressionTimezone": "Asia/Seoul",
        "Target": {"RetryPolicy": {"MaximumRetryAttempts": 0}},
    })


def test_collector_cannot_read_discord_secrets(tmp_path):
    t = template(tmp_path)
    policies = t.find_resources("AWS::IAM::Policy")
    collector = [p for name, p in policies.items() if name.startswith("CollectorServiceRole")]
    assert collector
    text = str(collector[0]["Properties"]["PolicyDocument"])
    assert "/kw/discord" not in text and "/kw/collector/*" in text


def test_stream_filter_only_passes_notice_items(tmp_path):
    template(tmp_path).has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "FilterCriteria": {"Filters": [{"Pattern": Match.string_like_regexp('"N#"')}]},
    })


def test_public_mcp_runs_behind_web_adapter_with_throttling(tmp_path):
    t = template(tmp_path)
    t.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "kw-publicmcp", "Handler": "run.sh",
        "Environment": {"Variables": Match.object_like({"AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap"})},
    })
    t.has_resource_properties("AWS::ApiGatewayV2::Stage", {
        "StageName": "$default",
        "DefaultRouteSettings": {"ThrottlingRateLimit": 2, "ThrottlingBurstLimit": 5},
    })
    policies = t.find_resources("AWS::IAM::Policy")
    mcp_policy = str(next(p for n, p in policies.items() if n.startswith("PublicMcpServiceRole")))
    assert "PutItem" not in mcp_policy and "/kw/mcp/*" in mcp_policy and "/kw/discord" not in mcp_policy


def test_bridge_wiring_and_least_privilege(tmp_path):
    t = template(tmp_path)
    t.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "kw-bridge", "Handler": "run.sh", "Timeout": 30,
        "Environment": {"Variables": Match.object_like({"KW_APP": "kwcampus.bridge.app:app",
                                                         "KW_BRIDGE_TABLE": Match.any_value()})},
    })
    t.has_resource_properties("AWS::DynamoDB::Table", {"TableName": "kw-bridge",
                                                        "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True}})
    routes = {r["Properties"]["RouteKey"] for r in t.find_resources("AWS::ApiGatewayV2::Route").values()}
    assert {"$connect", "$disconnect", "$default", "ANY /{proxy+}"} <= routes
    policies = t.find_resources("AWS::IAM::Policy")
    bridge = str(next(p for n, p in policies.items() if n.startswith("BridgeServiceRole")))
    ws = str(next(p for n, p in policies.items() if n.startswith("BridgeWsServiceRole")))
    assert "execute-api:ManageConnections" in bridge and "/kw/bridge/*" in bridge and "/kw/discord" not in bridge
    assert "ssm:" not in ws and "ManageConnections" not in ws


def test_discord_interactions_have_their_own_api_and_least_privilege(tmp_path):
    t = template(tmp_path)
    t.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "kw-discordinteractions", "Handler": "kwcampus.interactions.handler", "Timeout": 5})
    apis = {a["Properties"]["Name"] for a in t.find_resources("AWS::ApiGatewayV2::Api").values()}
    assert "kw-discord" in apis
    routes = {r["Properties"]["RouteKey"] for r in t.find_resources("AWS::ApiGatewayV2::Route").values()}
    assert "POST /discord/interactions" in routes
    policies = t.find_resources("AWS::IAM::Policy")
    policy = str(next(p for n, p in policies.items() if n.startswith("DiscordInteractionsServiceRole")))
    assert all(f"/kw/discord/{name}" in policy for name in ("bot-token", "public-key", "roles"))
    assert "/kw/discord/*" not in policy and "/kw/discord/channels" not in policy
    assert "dynamodb" not in policy and "sqs" not in policy
    assert "ssm:GetParameters" in policy  # one call for all three
    t.has_resource_properties("AWS::CloudWatch::Alarm", {"AlarmName": "kw-discord-buttons-failed",
                                                         "MetricName": "InteractionFailed"})

import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).parents[1] / "infra"))
from kw_campus_stack import KwCampusStack  # noqa: E402


def template(tmp_path) -> Template:
    (tmp_path / "placeholder.py").write_text("")
    app = cdk.App()
    stack = KwCampusStack(app, "Test", asset_path=str(tmp_path), env=cdk.Environment(region="ap-northeast-2"))
    return Template.from_stack(stack)


def test_functions_and_tables(tmp_path):
    t = template(tmp_path)
    t.resource_count_is("AWS::Lambda::Function", 7)
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
    mcp_policy = str([p for n, p in policies.items() if n.startswith("PublicMcpServiceRole")][0])
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
    bridge = str([p for n, p in policies.items() if n.startswith("BridgeServiceRole")][0])
    ws = str([p for n, p in policies.items() if n.startswith("BridgeWsServiceRole")][0])
    assert "execute-api:ManageConnections" in bridge and "/kw/bridge/*" in bridge and "/kw/discord" not in bridge
    assert "ssm:" not in ws and "ManageConnections" not in ws

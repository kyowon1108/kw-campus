import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).parents[1] / "infra"))
from kw_campus_stack import KwCampusStack


def template(tmp_path, alarm_email: str | None = None) -> Template:
    (tmp_path / "placeholder.py").write_text("")
    app = cdk.App()
    stack = KwCampusStack(app, "Test", asset_path=str(tmp_path), alarm_email=alarm_email,
                          env=cdk.Environment(region="ap-northeast-2"))
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


def test_collector_runs_one_at_a_time_and_disabled_origins_alarm(tmp_path):
    t = template(tmp_path)
    t.has_resource_properties("AWS::Lambda::Function", {"FunctionName": "kw-collector",
                                                        "ReservedConcurrentExecutions": 1})
    t.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumEventAgeInSeconds": 300,
                                                                 "MaximumRetryAttempts": 0})
    names = {a["Properties"]["AlarmName"]: a["Properties"].get("MetricName")
             for a in t.find_resources("AWS::CloudWatch::Alarm").values()}
    assert names["kw-collector-origin-disabled"] == "OriginDisabled"
    assert names["kw-collector-needs-human"] == "NeedsHuman"
    assert names["kw-collector-row-issues"] == "RowIssues" and names["kw-collector-uid-regressed"] == "UidRegressed"
    assert names["kw-collector-source-failed"] == "SourceFailed"


def test_bridge_routes_have_their_own_limits(tmp_path):
    t = template(tmp_path)
    routes = {r["Properties"]["RouteKey"] for r in t.find_resources("AWS::ApiGatewayV2::Route").values()}
    assert {"POST /oauth/register", "POST /device/register", "POST /device/result", "ANY /{proxy+}"} <= routes
    stage = next(s["Properties"] for s in t.find_resources("AWS::ApiGatewayV2::Stage").values()
                 if "RouteSettings" in s["Properties"])
    settings = stage["RouteSettings"]
    assert settings["POST /oauth/register"] == {"ThrottlingRateLimit": 1, "ThrottlingBurstLimit": 5}
    assert settings["POST /device/result"]["ThrottlingRateLimit"] == 20
    assert stage["DefaultRouteSettings"] == {"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20}
    t.has_resource_properties("AWS::Lambda::Function", {"FunctionName": "kw-bridge",
                                                        "ReservedConcurrentExecutions": 20})


def test_failures_and_security_events_raise_alarms(tmp_path):
    t = template(tmp_path)
    names = {a["Properties"]["AlarmName"] for a in t.find_resources("AWS::CloudWatch::Alarm").values()}
    assert {"kw-bridge-5xx", "kw-public-api-5xx", "kw-discord-api-5xx", "kw-stream-dlq",
            "kw-bridge-throttles", "kw-collector-throttles", "kw-bridge-refresh-reuse",
            "kw-bridge-device-signature-refused", "kw-bridge-approve-cross-site"} <= names
    filters = [f["Properties"]["FilterPattern"] for f in t.find_resources("AWS::Logs::MetricFilter").values()]
    assert any("refresh_reuse_revoked" in f for f in filters) and any("cross_site" in f for f in filters)
    t.has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "DestinationConfig": {"OnFailure": {"Destination": Match.any_value()}}})
    t.resource_count_is("AWS::Budgets::Budget", 0)  # no address, no budget mail


def test_budget_mails_the_alarm_address(tmp_path):
    t = template(tmp_path, alarm_email="ops@example.com")
    t.has_resource_properties("AWS::Budgets::Budget", {
        "Budget": Match.object_like({"BudgetLimit": {"Amount": 5, "Unit": "USD"}, "TimeUnit": "MONTHLY"}),
        "NotificationsWithSubscribers": Match.array_with([Match.object_like({
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": "ops@example.com"}]})]),
    })


def test_logical_ids_stay_put(tmp_path):
    """The deployed stack's IDs (checked against CloudFormation, 2026-11-06). A changed ID replaces the
    resource: a new API id breaks every connector and installer URL, a replaced table loses its data."""
    deployed = {"PublicD0AE4A11", "Delivery5F6C039D", "BridgeTable91945A73", "BridgeApi2D65AE30",
                "BridgeWsApi49B6887C", "PublicApiC8293BC5", "DiscordQueueF83490F5", "DiscordDlq7CEACF49",
                "Alarms04B5A0BF", "Bridge48182F58", "BridgeWs0035CE13", "Collector9EBA7CF5"}
    assert deployed <= set(template(tmp_path).to_json()["Resources"])

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


def policy_of(t: Template, prefix: str) -> dict:
    return next(p for n, p in t.find_resources("AWS::IAM::Policy").items() if n.startswith(prefix))


def route_targets(t: Template) -> dict[str, str]:
    """Bridge route key -> logical ID of the Lambda function its integration invokes."""
    integrations = t.find_resources("AWS::ApiGatewayV2::Integration")
    out = {}
    for route in t.find_resources("AWS::ApiGatewayV2::Route").values():
        props = route["Properties"]
        if props["ApiId"] != {"Ref": "BridgeApi2D65AE30"}:
            continue
        integration = props["Target"]["Fn::Join"][1][1]["Ref"]
        out[props["RouteKey"]] = integrations[integration]["Properties"]["IntegrationUri"]["Fn::GetAtt"][0]
    return out


def test_functions_and_tables(tmp_path):
    t = template(tmp_path)
    t.resource_count_is("AWS::Lambda::Function", 9)
    t.resource_count_is("AWS::DynamoDB::Table", 3)
    t.has_resource_properties("AWS::DynamoDB::Table", {
        "TableName": "kw-public",
        "StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"},
        "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
    })


def test_tables_refuse_deletes_and_the_bridge_table_keeps_no_backups(tmp_path):
    tables = {r["Properties"]["TableName"]: (r["Properties"], r.get("DeletionPolicy"))
              for r in template(tmp_path).find_resources("AWS::DynamoDB::Table").values()}
    assert set(tables) == {"kw-public", "kw-delivery", "kw-bridge"}
    for name, (props, deletion_policy) in tables.items():
        assert props.get("DeletionProtectionEnabled") is True, name
        assert deletion_policy == "Retain", name
    # /privacy promises no backups of the bridge table: no point-in-time recovery there
    pitr = tables["kw-bridge"][0].get("PointInTimeRecoverySpecification", {})
    assert not pitr.get("PointInTimeRecoveryEnabled")


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
    assert {"POST /oauth/register", "POST /device/register", "POST /device/result", "ANY /{proxy+}",
            "POST /mcp", "HEAD /oauth/authorize"} <= routes
    stage = next(s["Properties"] for s in t.find_resources("AWS::ApiGatewayV2::Stage").values()
                 if "RouteSettings" in s["Properties"])
    settings = stage["RouteSettings"]
    assert settings["POST /oauth/register"] == {"ThrottlingRateLimit": 1, "ThrottlingBurstLimit": 5}
    assert settings["POST /device/result"]["ThrottlingRateLimit"] == 20
    # /mcp no longer shares the stage default with junk paths and the installer pages
    assert settings["POST /mcp"] == {"ThrottlingRateLimit": 5, "ThrottlingBurstLimit": 10}
    # Starlette answers HEAD with the GET handler (which opens an AUTHREQ row): same limit as GET
    assert settings["HEAD /oauth/authorize"] == settings["GET /oauth/authorize"] == {
        "ThrottlingRateLimit": 2, "ThrottlingBurstLimit": 5}
    assert stage["DefaultRouteSettings"] == {"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20}
    t.has_resource_properties("AWS::Lambda::Function", {"FunctionName": "kw-bridge",
                                                        "ReservedConcurrentExecutions": 20})


def test_bridge_stage_waits_for_every_route_it_limits(tmp_path):
    """API Gateway refuses RouteSettings for a route key that doesn't exist yet, so the stage must
    not be created or updated before the routes it names."""
    t = template(tmp_path)
    stage = next(s for s in t.find_resources("AWS::ApiGatewayV2::Stage").values()
                 if "RouteSettings" in s["Properties"])
    route_ids = {r["Properties"]["RouteKey"]: name for name, r in t.find_resources("AWS::ApiGatewayV2::Route").items()
                 if r["Properties"]["ApiId"] == {"Ref": "BridgeApi2D65AE30"}}
    named = set(stage["Properties"]["RouteSettings"])
    assert named <= set(route_ids)
    assert {route_ids[key] for key in named} <= set(stage.get("DependsOn", []))


def test_result_posts_have_their_own_function(tmp_path):
    """/mcp calls wait up to 22 s for an agent while holding a bridge slot; the result posts they
    wait for must not need one of those slots."""
    t = template(tmp_path)
    targets = route_targets(t)
    assert targets["POST /device/result"] == "BridgeResult54FB6986"
    assert {fn for key, fn in targets.items() if key != "POST /device/result"} == {"Bridge48182F58"}
    t.has_resource_properties("AWS::Lambda::Function", {
        "FunctionName": "kw-bridgeresult", "Handler": "run.sh", "ReservedConcurrentExecutions": 5,
        "Environment": {"Variables": Match.object_like({
            "KW_APP": "kwcampus.bridge.app:app", "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
            "KW_BRIDGE_TABLE": Match.any_value(), "KW_BRIDGE_BASE_URL": Match.any_value()})},
        "LoggingConfig": {"LogGroup": {"Ref": Match.string_like_regexp("^BridgeResultLogs")}},
    })
    t.has_resource_properties("AWS::Logs::LogGroup", {"LogGroupName": "/aws/lambda/kw-bridgeresult"})
    functions = t.find_resources("AWS::Lambda::Function")
    result_env = functions["BridgeResult54FB6986"]["Properties"]["Environment"]["Variables"]
    bridge_env = functions["Bridge48182F58"]["Properties"]["Environment"]["Variables"]
    assert {k: v for k, v in bridge_env.items() if not k.startswith("KW_BRIDGE_WS")} == result_env
    reserved = sum(f["Properties"].get("ReservedConcurrentExecutions", 0) for f in functions.values())
    assert reserved == 1 + 20 + 5  # collector, bridge, result posts


def test_result_function_can_only_touch_result_slots(tmp_path):
    t = template(tmp_path)
    statements = policy_of(t, "BridgeResultServiceRole")["Properties"]["PolicyDocument"]["Statement"]
    by_action = {tuple(s["Action"]) if isinstance(s["Action"], list) else (s["Action"],): s for s in statements}
    assert set(by_action) == {("dynamodb:GetItem", "dynamodb:UpdateItem"), ("ssm:GetParameter",)}
    ddb = by_action[("dynamodb:GetItem", "dynamodb:UpdateItem")]
    assert ddb["Resource"] == {"Fn::GetAtt": ["BridgeTable91945A73", "Arn"]}  # the table, no index
    assert ddb["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["RESULT#*"]}}
    # only the JWT secret, for the app-wide bearer check; no invites, no other bridge parameters
    assert by_action[("ssm:GetParameter",)]["Resource"]["Fn::Join"][1][-1] == ":parameter/kw/bridge/jwt-secret"
    text = str(statements)
    assert "ManageConnections" not in text and "/kw/mcp" not in text and "PutItem" not in text
    assert "DeleteItem" not in text and "Query" not in text and "Scan" not in text
    # no grant on kw-public either
    assert "PublicD0AE4A11" not in text


def test_result_function_and_bridge_4xx_raise_alarms(tmp_path):
    alarms = {a["Properties"]["AlarmName"]: a["Properties"]
              for a in template(tmp_path).find_resources("AWS::CloudWatch::Alarm").values()}
    assert alarms["kw-bridgeresult-errors"]["MetricName"] == "Errors"
    assert alarms["kw-bridgeresult-throttles"]["MetricName"] == "Throttles"
    four = alarms["kw-bridge-4xx"]
    assert (four["Namespace"], four["MetricName"], four["Period"], four["Threshold"]) == (
        "AWS/ApiGateway", "4xx", 300, 100)
    assert four["Dimensions"] == [{"Name": "ApiId", "Value": {"Ref": "BridgeApi2D65AE30"}}]
    assert four["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


def test_agent_outcome_alarms_match_the_bridge_counters(tmp_path):
    """The bridge app emits AgentOutcomeTimeout and AgentOutcomeUnexpected (KwCampus, no dimensions)."""
    alarms = {a["Properties"]["MetricName"]: a["Properties"]
              for a in template(tmp_path).find_resources("AWS::CloudWatch::Alarm").values()}
    for metric, name in (("AgentOutcomeTimeout", "kw-bridge-agent-timeouts"),
                         ("AgentOutcomeUnexpected", "kw-bridge-agent-unexpected")):
        a = alarms[metric]
        assert a["AlarmName"] == name and a["Namespace"] == "KwCampus" and a["Statistic"] == "Sum"
        assert (a["Period"], a["EvaluationPeriods"], a["Threshold"]) == (900, 1, 5)
        assert "Dimensions" not in a
        assert a["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


def test_refused_websocket_signatures_count_as_device_signature_refusals(tmp_path):
    t = template(tmp_path)
    filters = [f["Properties"] for f in t.find_resources("AWS::Logs::MetricFilter").values()]
    ws = [f for f in filters if f["LogGroupName"] == {"Ref": "BridgeWsLogsF4E00FFD"}]
    assert len(ws) == 1
    pattern = ws[0]["FilterPattern"]
    assert '$.event = "ws_connect_denied"' in pattern
    assert '$.reason = "bad_signature"' in pattern and '$.reason = "replayed"' in pattern
    assert "stale_timestamp" not in pattern and "unknown_device" not in pattern
    assert ws[0]["MetricTransformations"] == [{"MetricName": "DeviceSignatureRefused",
                                               "MetricNamespace": "KwCampus", "MetricValue": "1"}]
    http = [f for f in filters if f["LogGroupName"] == {"Ref": "BridgeLogs410D2298"}
            and "device_signature_refused" in f["FilterPattern"]]
    assert http and http[0]["MetricTransformations"] == ws[0]["MetricTransformations"]


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


def test_budget_counts_spend_before_credits_and_refunds(tmp_path):
    """With credits counted (the default) the budget sees $0 while credits last and never mails."""
    budget = next(iter(template(tmp_path, alarm_email="ops@example.com")
                       .find_resources("AWS::Budgets::Budget").values()))["Properties"]["Budget"]
    assert budget["CostTypes"] == {"IncludeCredit": False, "IncludeRefund": False}


def test_logical_ids_stay_put(tmp_path):
    """The deployed stack's IDs (checked against CloudFormation, 2026-11-06). A changed ID replaces the
    resource: a new API id breaks every connector and installer URL, a replaced table loses its data."""
    deployed = {"PublicD0AE4A11", "Delivery5F6C039D", "BridgeTable91945A73", "BridgeApi2D65AE30",
                "BridgeWsApi49B6887C", "PublicApiC8293BC5", "DiscordQueueF83490F5", "DiscordDlq7CEACF49",
                "Alarms04B5A0BF", "Bridge48182F58", "BridgeWs0035CE13", "Collector9EBA7CF5"}
    assert deployed <= set(template(tmp_path).to_json()["Resources"])

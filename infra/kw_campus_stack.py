"""kw-campus infrastructure: public notices pipeline + public MCP (stages 1-2) and the personal bridge (stage 4)."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_int
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

# Weekdays 09:00-18:59 KST, every 20 minutes (30 runs a day).
COLLECT_SCHEDULE = "cron(0/20 9-18 ? * MON-FRI *)"
# One more look on weekday evenings: closures and class cancellations are often posted the evening
# before (에브리타임, 2026-11). No weekend run: none of the first 309 notices was posted on a weekend.
EXTRA_COLLECT_SCHEDULES = {"CollectEveningSchedule": ("kw-collector-evening", "cron(0 21 ? * MON-FRI *)")}
# Weekdays 07:30 KST: cafeteria menu / academic calendar refresh (0-2 www requests a run).
CAMPUS_SCHEDULE = "cron(30 7 ? * MON-FRI *)"
# Per-route limits on the bridge (requests/s, burst). The stage default stays 10/20 for /mcp and the
# installer pages; signup-like routes get much less, and agents' result posts get their own room so
# anonymous traffic can't starve them.
BRIDGE_ROUTE_LIMITS = {
    ("POST", "/oauth/register"): (1, 5),
    ("GET", "/oauth/authorize"): (2, 5),
    ("POST", "/oauth/approve"): (2, 5),
    ("POST", "/oauth/token"): (5, 10),
    ("POST", "/device/register"): (1, 3),
    ("POST", "/device/pair"): (2, 5),
    ("POST", "/device/grants"): (2, 5),
    ("POST", "/device/revoke"): (2, 5),
    ("POST", "/device/unlink"): (2, 5),
    ("POST", "/device/result"): (20, 40),
}
MONTHLY_BUDGET_USD = 5
# Lambda Web Adapter (arm64) in ap-northeast-2; serves uvicorn behind Lambda.
LWA_LAYER = "arn:aws:lambda:ap-northeast-2:753240598075:layer:LambdaAdapterLayerArm64:30"


class KwCampusStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, *, asset_path: str, alarm_email: str | None = None, **kwargs):
        super().__init__(scope, cid, **kwargs)
        ssm_arn = f"arn:aws:ssm:{self.region}:{self.account}:parameter"

        public = ddb.Table(
            self, "Public",
            table_name="kw-public",
            partition_key=ddb.Attribute(name="PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="SK", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        delivery = ddb.Table(
            self, "Delivery",
            table_name="kw-delivery",
            partition_key=ddb.Attribute(name="PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="SK", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        # Bridge bookkeeping only: device public keys, OAuth clients/codes/refresh tokens, live
        # WebSocket connections, and 2-minute ciphertext. No KLAS credentials or plaintext data.
        bridge_table = ddb.Table(
            self, "BridgeTable",
            table_name="kw-bridge",
            partition_key=ddb.Attribute(name="PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="SK", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        dlq = sqs.Queue(self, "DiscordDlq", queue_name="kw-discord-dlq", retention_period=cdk.Duration.days(14))
        queue = sqs.Queue(
            self, "DiscordQueue",
            queue_name="kw-discord",
            visibility_timeout=cdk.Duration.seconds(180),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=5),
        )

        code = lambda_.Code.from_asset(asset_path)
        env = {"KW_PUBLIC_TABLE": public.table_name, "KW_DELIVERY_TABLE": delivery.table_name}

        def fn(name: str, handler: str, timeout: int, extra_env: dict | None = None,
               memory: int = 256, layers: list | None = None, retries: int | None = None,
               concurrency: int | None = None, max_event_age: cdk.Duration | None = None) -> lambda_.Function:
            log_group = logs.LogGroup(
                self, f"{name}Logs",
                log_group_name=f"/aws/lambda/kw-{name.lower()}",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            return lambda_.Function(
                self, name,
                function_name=f"kw-{name.lower()}",
                runtime=lambda_.Runtime.PYTHON_3_13,
                architecture=lambda_.Architecture.ARM_64,
                handler=handler,
                code=code,
                memory_size=memory,
                timeout=cdk.Duration.seconds(timeout),
                environment={**env, **(extra_env or {})},
                log_group=log_group,
                layers=layers,
                retry_attempts=retries,
                reserved_concurrent_executions=concurrency,
                max_event_age=max_event_age,
            )

        # Up to 8 requests a run (www, E1-E4, two Wix hosts, www again after a 61 s wait), each
        # bounded by fetch.WORST_CASE_S; a collector with less time left than that skips its request.
        # retries=0: a failed or slow run must not hit the school's servers again a minute later.
        # One at a time: a manual run can never overlap a scheduled one (the 60 s gap holds), and a
        # run held back by that is dropped after 5 minutes instead of retried for hours.
        collector = fn("Collector", "kwcampus.collector.handler", 150, retries=0, concurrency=1,
                       max_event_age=cdk.Duration.minutes(5))
        dispatcher = fn("Dispatcher", "kwcampus.dispatcher.handler", 30, {"KW_DISCORD_QUEUE_URL": queue.queue_url})
        sender = fn("DiscordSender", "kwcampus.discord.handler", 60)
        campus = fn("Campus", "kwcampus.campus.handler", 150, retries=0)
        lwa = lambda_.LayerVersion.from_layer_version_arn(self, "LwaLayer", LWA_LAYER)
        web_env = {"AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap", "AWS_LWA_PORT": "8080",
                   "AWS_LWA_READINESS_CHECK_PATH": "/healthz"}
        public_mcp = fn("PublicMcp", "run.sh", 15, memory=512, layers=[lwa], extra_env=web_env)

        # collector: kw-public only, its own switches, no Discord secrets
        public.grant_read_write_data(collector)
        collector.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"], resources=[f"{ssm_arn}/kw/collector/*"]))

        # dispatcher: reads the stream, writes jobs, reads the notify switch
        # Records the dispatcher still can't handle after the retries are kept here instead of dropped.
        stream_dlq = sqs.Queue(self, "StreamDlq", queue_name="kw-stream-dlq", retention_period=cdk.Duration.days(14))
        dispatcher.add_event_source(sources.DynamoEventSource(
            public,
            starting_position=lambda_.StartingPosition.LATEST,
            batch_size=25,
            retry_attempts=3,
            bisect_batch_on_error=True,
            on_failure=sources.SqsDlq(stream_dlq),
            filters=[lambda_.FilterCriteria.filter({
                "eventName": lambda_.FilterRule.or_("INSERT", "MODIFY"),
                "dynamodb": {"Keys": {"SK": {"S": lambda_.FilterRule.begins_with("N#")}}},
            })],
        ))
        queue.grant_send_messages(dispatcher)
        dispatcher.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"], resources=[f"{ssm_arn}/kw/collector/notify"]))

        # sender: reads notices, owns delivery records, reads Discord parameters
        sender.add_event_source(sources.SqsEventSource(queue, batch_size=5, report_batch_item_failures=True))
        public.grant_read_data(sender)
        delivery.grant_read_write_data(sender)
        sender.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"], resources=[f"{ssm_arn}/kw/discord/*"]))

        # Discord buttons (#알림-설정): its own function and API, no table access, reads only the
        # three parameters it needs. Discord must get an answer within 3 s.
        # 1024 MB for the CPU share: a cold start must still answer well inside those 3 s.
        interactions = fn("DiscordInteractions", "kwcampus.interactions.handler", 5, memory=1024)
        interactions.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[f"{ssm_arn}/kw/discord/{name}" for name in ("bot-token", "public-key", "roles")]))
        discord_api = apigw.HttpApi(self, "DiscordApi", api_name="kw-discord", create_default_stage=False)
        discord_api.add_routes(path="/discord/interactions", methods=[apigw.HttpMethod.POST],
                               integration=apigw_int.HttpLambdaIntegration("DiscordInteractionsIntegration",
                                                                           interactions))
        # Shared by every caller, unsigned junk included (rejected for a fraction of a cent each), so
        # it is set well above real click rates; Discord doesn't retry a click that got 429.
        discord_api.add_stage("DiscordDefaultStage", stage_name="$default", auto_deploy=True,
                              throttle=apigw.ThrottleSettings(rate_limit=20, burst_limit=40))

        # campus data: same table and switch as the collector
        public.grant_read_write_data(campus)
        campus.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"], resources=[f"{ssm_arn}/kw/collector/*"]))

        # public MCP: read-only on kw-public, its own token list
        public.grant_read_data(public_mcp)
        public_mcp.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"], resources=[f"{ssm_arn}/kw/mcp/*"]))
        api = apigw.HttpApi(self, "PublicApi", api_name="kw-public-mcp", create_default_stage=False,
                            default_integration=apigw_int.HttpLambdaIntegration("PublicMcpIntegration", public_mcp))
        api.add_stage("DefaultStage", stage_name="$default", auto_deploy=True,
                      throttle=apigw.ThrottleSettings(rate_limit=2, burst_limit=5))

        # bridge: WebSocket for PC agents, HTTP for OAuth + personal MCP + installer
        bridge_env = {"KW_BRIDGE_TABLE": bridge_table.table_name}
        bridge_ws = fn("BridgeWs", "kwcampus.bridge.ws.handler", 10, extra_env=bridge_env)
        bridge_table.grant_read_write_data(bridge_ws)
        ws_integration = apigw_int.WebSocketLambdaIntegration
        ws_api = apigw.WebSocketApi(
            self, "BridgeWsApi", api_name="kw-bridge-ws",
            connect_route_options=apigw.WebSocketRouteOptions(integration=ws_integration("WsConnect", bridge_ws)),
            disconnect_route_options=apigw.WebSocketRouteOptions(integration=ws_integration("WsDisconnect", bridge_ws)),
            default_route_options=apigw.WebSocketRouteOptions(integration=ws_integration("WsDefault", bridge_ws)),
        )
        ws_stage = apigw.WebSocketStage(self, "BridgeWsStage", web_socket_api=ws_api, stage_name="prod",
                                        auto_deploy=True, throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10))
        bridge_api = apigw.HttpApi(self, "BridgeApi", api_name="kw-bridge", create_default_stage=False)
        # 20 at most: a flood on the bridge can't take the concurrency the collector and Discord need.
        bridge = fn("Bridge", "run.sh", 30, memory=512, layers=[lwa], concurrency=20, extra_env={
            **web_env, **bridge_env, "KW_APP": "kwcampus.bridge.app:app",
            "KW_BRIDGE_BASE_URL": bridge_api.api_endpoint,
            "KW_BRIDGE_WS_URL": ws_stage.url, "KW_BRIDGE_WS_MGMT": ws_stage.callback_url,
        })
        bridge_table.grant_read_write_data(bridge)
        public.grant_read_data(bridge)
        ws_stage.grant_management_api_access(bridge)
        bridge.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"], resources=[f"{ssm_arn}/kw/bridge/*", f"{ssm_arn}/kw/mcp/*"]))
        bridge_integration = apigw_int.HttpLambdaIntegration("BridgeIntegration", bridge)
        bridge_api.add_routes(path="/{proxy+}", methods=[apigw.HttpMethod.ANY], integration=bridge_integration)
        for method, path in BRIDGE_ROUTE_LIMITS:
            bridge_api.add_routes(path=path, methods=[apigw.HttpMethod(method)], integration=bridge_integration)
        # Account-wide, not per IP (HTTP APIs take no WAF): high enough that one noisy client doesn't
        # lock everyone out, low enough to cap the bill (the budget alarm covers the rest).
        bridge_stage = bridge_api.add_stage("BridgeDefaultStage", stage_name="$default", auto_deploy=True,
                                            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20))
        bridge_stage.node.default_child.add_property_override("RouteSettings", {
            f"{method} {path}": {"ThrottlingRateLimit": rate, "ThrottlingBurstLimit": burst}
            for (method, path), (rate, burst) in BRIDGE_ROUTE_LIMITS.items()})

        # schedules
        schedule_role = iam.Role(self, "ScheduleRole", assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))
        collector.grant_invoke(schedule_role)
        campus.grant_invoke(schedule_role)
        scheduler.CfnSchedule(
            self, "CampusSchedule",
            name="kw-campus-tick",
            schedule_expression=CAMPUS_SCHEDULE,
            schedule_expression_timezone="Asia/Seoul",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=campus.function_arn,
                role_arn=schedule_role.role_arn,
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_retry_attempts=0),
            ),
        )
        for construct_id, (name, expression) in {"CollectSchedule": ("kw-collector-tick", COLLECT_SCHEDULE),
                                                 **EXTRA_COLLECT_SCHEDULES}.items():
            scheduler.CfnSchedule(
                self, construct_id,
                name=name,
                schedule_expression=expression,
                schedule_expression_timezone="Asia/Seoul",
                flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
                target=scheduler.CfnSchedule.TargetProperty(
                    arn=collector.function_arn,
                    role_arn=schedule_role.role_arn,
                    retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_retry_attempts=0),
                ),
            )

        # alarms
        topic = sns.Topic(self, "Alarms", topic_name="kw-alarms")
        if alarm_email:
            topic.add_subscription(subs.EmailSubscription(alarm_email))
        action = cw_actions.SnsAction(topic)

        def alarm(name: str, metric: cw.IMetric, threshold: float = 1) -> None:
            a = cw.Alarm(
                self, name,
                alarm_name=f"kw-{name}",
                metric=metric,
                threshold=threshold,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(action)

        five_min = cdk.Duration.minutes(5)
        for f in (collector, dispatcher, sender, interactions, campus, public_mcp, bridge, bridge_ws):
            alarm(f"{f.node.id.lower()}-errors", f.metric_errors(period=five_min))
        for f in (collector, bridge, interactions):
            alarm(f"{f.node.id.lower()}-throttles", f.metric_throttles(period=five_min))
        # 5xx answers that never raise in the function (adapter or integration failures, timeouts)
        for name, http_api in (("bridge", bridge_api), ("public-api", api), ("discord-api", discord_api)):
            alarm(f"{name}-5xx", http_api.metric_server_error(period=five_min), threshold=3)
        alarm("stream-dlq", stream_dlq.metric_approximate_number_of_messages_visible(period=five_min))
        # Security events the bridge logs (obs.log): a reused refresh token (a stolen one, or a
        # client bug), a burst of bad device signatures, approvals posted from another site.
        is_event = lambda event: logs.FilterPattern.string_value("$.event", "=", event)
        security_events = (
            ("refresh-reuse", "RefreshReuseRevoked", is_event("refresh_reuse_revoked"), 1),
            ("device-signature-refused", "DeviceSignatureRefused", is_event("device_signature_refused"), 20),
            ("approve-cross-site", "ApproveCrossSite",
             logs.FilterPattern.all(is_event("approve_refused"),
                                    logs.FilterPattern.string_value("$.reason", "=", "cross_site")), 3),
        )
        for name, metric_name, pattern, threshold in security_events:
            logs.MetricFilter(self, f"{metric_name}Filter", log_group=bridge.log_group,
                              metric_namespace="KwCampus", metric_name=metric_name, metric_value="1",
                              filter_pattern=pattern)
            alarm(f"bridge-{name}", cw.Metric(namespace="KwCampus", metric_name=metric_name,
                                             statistic="Sum", period=five_min), threshold=threshold)
        if alarm_email:
            budgets.CfnBudget(self, "MonthlyBudget", budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="kw-campus-monthly", budget_type="COST", time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=MONTHLY_BUDGET_USD, unit="USD")),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            notification_type=kind, comparison_operator="GREATER_THAN", threshold=percent,
                            threshold_type="PERCENTAGE"),
                        subscribers=[budgets.CfnBudget.SubscriberProperty(subscription_type="EMAIL",
                                                                          address=alarm_email)])
                    for kind, percent in (("ACTUAL", 80), ("FORECASTED", 100))])
        # hard blocks only (403, 429, a challenge page); transient failures go to NeedsHuman after 6 in a row
        alarm("collector-blocked", cw.Metric(namespace="KwCampus", metric_name="Blocked",
                                             statistic="Sum", period=cdk.Duration.minutes(20)))
        # every run that skips a switched-off origin counts, so this stays in ALARM until a human
        # re-enables it (scripts/collector_admin.py reenable)
        alarm("collector-origin-disabled", cw.Metric(namespace="KwCampus", metric_name="OriginDisabled",
                                                     statistic="Sum", period=cdk.Duration.hours(1)))
        # a switched-off source, a robots.txt Disallow, or six transient failures in a row
        alarm("collector-needs-human", cw.Metric(namespace="KwCampus", metric_name="NeedsHuman",
                                                 statistic="Sum", period=cdk.Duration.hours(1)))
        # one board's own failures (404, a redirect, a changed page) back off that board only
        alarm("collector-source-failed", cw.Metric(namespace="KwCampus", metric_name="SourceFailed",
                                                   statistic="Sum", period=cdk.Duration.hours(6)), threshold=3)
        # a board's highest post number went down: its newest post was deleted, or it renumbered
        alarm("collector-uid-regressed", cw.Metric(namespace="KwCampus", metric_name="UidRegressed",
                                                   statistic="Sum", period=cdk.Duration.hours(6)))
        # main-board rows with a 분류 the parser doesn't know (a new category): not stored until handled
        alarm("collector-row-issues", cw.Metric(namespace="KwCampus", metric_name="RowIssues",
                                                statistic="Sum", period=cdk.Duration.hours(3)))
        # a board that answers but parses to nothing: its layout changed, or a block page we don't recognise
        alarm("collector-parse-empty", cw.Metric(namespace="KwCampus", metric_name="ParseEmpty",
                                                 statistic="Sum", period=cdk.Duration.hours(3)), threshold=3)
        alarm("discord-dlq", dlq.metric_approximate_number_of_messages_visible(period=five_min))
        # buttons that couldn't change a role (bot role dragged below the subscription roles, token gone)
        alarm("discord-buttons-failed", cw.Metric(namespace="KwCampus", metric_name="InteractionFailed",
                                                  statistic="Sum", period=cdk.Duration.hours(1)), threshold=3)

        cdk.CfnOutput(self, "PublicTableName", value=public.table_name)
        cdk.CfnOutput(self, "DiscordQueueUrl", value=queue.queue_url)
        cdk.CfnOutput(self, "PublicApiUrl", value=api.api_endpoint)
        cdk.CfnOutput(self, "BridgeUrl", value=bridge_api.api_endpoint)
        cdk.CfnOutput(self, "BridgeWsUrl", value=ws_stage.url)
        # Developer Portal → General Information → Interactions Endpoint URL
        cdk.CfnOutput(self, "DiscordInteractionsUrl", value=f"{discord_api.api_endpoint}/discord/interactions")

"""Stage 1 infrastructure: kw-public / kw-delivery tables, collector, dispatcher, Discord sender."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_int
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
# Weekdays 07:30 KST: cafeteria menu / academic calendar refresh (0-2 requests).
CAMPUS_SCHEDULE = "cron(30 7 ? * MON-FRI *)"
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
               memory: int = 256, layers: list | None = None) -> lambda_.Function:
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
            )

        collector = fn("Collector", "kwcampus.collector.handler", 60)
        dispatcher = fn("Dispatcher", "kwcampus.dispatcher.handler", 30, {"KW_DISCORD_QUEUE_URL": queue.queue_url})
        sender = fn("DiscordSender", "kwcampus.discord.handler", 60)
        campus = fn("Campus", "kwcampus.campus.handler", 150)
        public_mcp = fn(
            "PublicMcp", "run.sh", 15, memory=512,
            layers=[lambda_.LayerVersion.from_layer_version_arn(self, "LwaLayer", LWA_LAYER)],
            extra_env={"AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap", "AWS_LWA_PORT": "8080",
                       "AWS_LWA_READINESS_CHECK_PATH": "/healthz"},
        )

        # collector: kw-public only, its own switches, no Discord secrets
        public.grant_read_write_data(collector)
        collector.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"], resources=[f"{ssm_arn}/kw/collector/*"]))

        # dispatcher: reads the stream, writes jobs, reads the notify switch
        dispatcher.add_event_source(sources.DynamoEventSource(
            public,
            starting_position=lambda_.StartingPosition.LATEST,
            batch_size=25,
            retry_attempts=3,
            bisect_batch_on_error=True,
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
        scheduler.CfnSchedule(
            self, "CollectSchedule",
            name="kw-collector-tick",
            schedule_expression=COLLECT_SCHEDULE,
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
        for f in (collector, dispatcher, sender, campus, public_mcp):
            alarm(f"{f.node.id.lower()}-errors", f.metric_errors(period=five_min))
        alarm("collector-blocked", cw.Metric(namespace="KwCampus", metric_name="Blocked",
                                             statistic="Sum", period=cdk.Duration.minutes(20)))
        alarm("discord-dlq", dlq.metric_approximate_number_of_messages_visible(period=five_min))

        cdk.CfnOutput(self, "PublicTableName", value=public.table_name)
        cdk.CfnOutput(self, "DiscordQueueUrl", value=queue.queue_url)
        cdk.CfnOutput(self, "PublicApiUrl", value=api.api_endpoint)

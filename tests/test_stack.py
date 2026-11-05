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


def test_three_functions_and_two_tables(tmp_path):
    t = template(tmp_path)
    t.resource_count_is("AWS::Lambda::Function", 3)
    t.resource_count_is("AWS::DynamoDB::Table", 2)
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

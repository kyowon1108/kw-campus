import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


def _table(dynamodb, name: str):
    return dynamodb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws():
    from kwcampus import invites, params

    params.clear_cache()
    invites.clear_cache()  # an invite's answer is kept a minute: never across tests
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        yield {
            "public": _table(dynamodb, "kw-public"),
            "delivery": _table(dynamodb, "kw-delivery"),
            "ssm": boto3.client("ssm"),
            "sqs": boto3.client("sqs"),
        }
    params.clear_cache()
    invites.clear_cache()

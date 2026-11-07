import os
import socket

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


class _NoNetwork:
    """The collectors' opener during tests: a school site is never asked for real. A test that means to read a
    page patches kwcampus.fetch._opener (or passes its own fetch) itself."""

    def __init__(self, attempts: list[str]):
        self.attempts = attempts

    def open(self, request, timeout=None):
        self.attempts.append(request.full_url)
        raise AssertionError(f"a test tried to reach the network: {request.full_url}")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches the network: the collectors' opener, every name lookup but localhost's and every socket
    connect (but local AF_UNIX ones) refuse, and a test that tried fails at teardown, even if its code swallowed
    the error."""
    from kwcampus import fetch

    attempts: list[str] = []
    real = socket.socket.connect, socket.socket.connect_ex

    def blocked(which):
        def connect(self, address):
            if self.family == getattr(socket, "AF_UNIX", None):
                return real[which](self, address)
            attempts.append(repr(address))
            raise ConnectionRefusedError(f"a test tried to reach the network: {address!r}")
        return connect

    real_lookup = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        if str(host) in ("localhost", "127.0.0.1", "::1"):
            return real_lookup(host, *args, **kwargs)
        attempts.append(f"lookup {host}")
        raise socket.gaierror(f"a test tried to reach the network: {host}")

    monkeypatch.setattr(socket.socket, "connect", blocked(0))
    monkeypatch.setattr(socket.socket, "connect_ex", blocked(1))
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(fetch, "_opener", _NoNetwork(attempts))
    yield attempts
    assert not attempts, f"a test tried to reach the network: {attempts}"


@pytest.fixture
def aws():
    from kwcampus import extract, invites, params

    params.clear_cache()
    invites.clear_cache()  # an invite's answer is kept a minute: never across tests
    extract._marked.clear()  # each test's kw-public is new: its read-records mark too
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

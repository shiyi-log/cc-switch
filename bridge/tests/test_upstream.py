import json
from decimal import Decimal

import pytest

from bridge.models import Account, ModelPrice
from bridge.upstream import SSEUsageParser, build_upstream_headers, calculate_cost, upstream_url


def test_upstream_url_keeps_base_path():
    account = Account(base_url="https://relay.test/anthropic/")
    assert upstream_url(account, "/v1/messages") == "https://relay.test/anthropic/v1/messages"


def test_oauth_headers_replace_client_credentials():
    account = Account(auth_type=Account.AUTH_OAUTH)
    account.set_credential("oauth-secret")
    headers = build_upstream_headers(
        account,
        {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "authorization": "Bearer client-token",
            "x-api-key": "client-key",
        },
    )
    assert headers["authorization"] == "Bearer oauth-secret"
    assert "x-api-key" not in headers
    assert "oauth-2025-04-20" in headers["anthropic-beta"]


def test_api_key_headers_use_account_key():
    account = Account(auth_type=Account.AUTH_API_KEY)
    account.set_credential("sk-ant-test")
    headers = build_upstream_headers(account, {"x-api-key": "bridge-token"})
    assert headers["x-api-key"] == "sk-ant-test"
    assert "authorization" not in headers


def test_sse_parser_extracts_usage_and_model():
    parser = SSEUsageParser()
    events = [
        "event: message_start\n"
        'data: {"type":"message_start","message":{"model":"claude-sonnet-4-5",'
        '"usage":{"input_tokens":12,"cache_read_input_tokens":3}}}\n\n',
        "event: message_delta\n"
        'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n',
    ]
    for event in events:
        parser.feed(event.encode())
    parser.finish()
    assert parser.model == "claude-sonnet-4-5"
    assert parser.usage == {
        "input_tokens": 12,
        "cache_read_input_tokens": 3,
        "output_tokens": 7,
    }


@pytest.mark.django_db
def test_calculate_cost_uses_longest_prefix():
    ModelPrice.objects.create(
        model_prefix="acme-model",
        input_per_mtok=Decimal("3"),
        output_per_mtok=Decimal("15"),
        cache_read_per_mtok=Decimal("0.3"),
        cache_write_per_mtok=Decimal("3.75"),
    )
    ModelPrice.objects.create(
        model_prefix="acme-model-v2",
        input_per_mtok=Decimal("4"),
        output_per_mtok=Decimal("20"),
        cache_read_per_mtok=Decimal("0.4"),
        cache_write_per_mtok=Decimal("5"),
    )
    cost = calculate_cost(
        "acme-model-v2-20260101",
        {"input_tokens": 1_000_000, "output_tokens": 0},
    )
    assert cost == Decimal("4.000000")


@pytest.mark.django_db
def test_calculate_cost_without_price_returns_none():
    assert calculate_cost("unknown-model", {"input_tokens": 10}) is None


def test_sse_parser_ignores_non_data_lines():
    parser = SSEUsageParser()
    parser.feed(b"event: ping\ndata: not-json\n\n")
    parser.finish()
    assert parser.usage == {}

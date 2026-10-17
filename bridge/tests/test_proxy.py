import json
from decimal import Decimal

import httpx
import pytest
import respx
from django.test import AsyncClient

from bridge.models import Account, RequestLog, UsageWindow

BRIDGE = {
    "TOKEN": "test-token",
    "MAX_ATTEMPTS": 3,
    "TIMEOUT_SECONDS": 30,
    "RESERVE_RATIO": "0.05",
    "SAFETY_MARGIN": "0.005",
    "PROBE_TTL_SECONDS": 300,
    "PROBE_REQUIRED": True,
    "PROBE_TIMEOUT_SECONDS": 5,
}


@pytest.fixture
def bridge_settings(settings):
    settings.BRIDGE = dict(BRIDGE)
    return settings


async def make_official(name="official", **kwargs):
    account = await Account.objects.acreate(
        name=name,
        tier=Account.TIER_OFFICIAL,
        auth_type=Account.AUTH_OAUTH,
        quota_mode=Account.QUOTA_HEADERS,
        **kwargs,
    )
    account.set_credential("oauth-secret")
    await account.asave()
    return account


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_proxy_forwards_and_updates_quota(bridge_settings):
    account = await make_official()
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            headers={
                "content-type": "application/json",
                "anthropic-ratelimit-unified-5h-utilization": "0.42",
                "anthropic-ratelimit-unified-7d-utilization": "0.31",
            },
        )
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 16, "messages": []}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token", "x-api-key": "client-key"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "msg_1"
    upstream_request = route.calls[0].request
    assert upstream_request.headers["authorization"] == "Bearer oauth-secret"
    assert "x-api-key" not in upstream_request.headers

    window = await UsageWindow.objects.aget(account_id=account.pk, key=UsageWindow.FIVE_HOURS)
    assert window.utilization == Decimal("0.420000")
    log = await RequestLog.objects.aget(account_id=account.pk)
    assert log.input_tokens == 10
    assert log.output_tokens == 5


@pytest.mark.django_db(transaction=True)
async def test_proxy_rejects_missing_token(bridge_settings):
    client = AsyncClient()
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5"}),
        content_type="application/json",
    )
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_proxy_returns_429_when_all_accounts_hit_reserve(bridge_settings):
    account = await make_official()
    await UsageWindow.objects.acreate(
        account=account, key=UsageWindow.FIVE_HOURS, utilization=Decimal("0.99")
    )
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={})
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 8, "messages": []}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert route.call_count == 0


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_proxy_falls_back_to_third_party(bridge_settings):
    official = await make_official()
    await UsageWindow.objects.acreate(
        account=official, key=UsageWindow.FIVE_HOURS, utilization=Decimal("0.99")
    )
    relay = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test/anthropic",
        budget_usd=Decimal("100"),
    )
    relay.set_credential("sk-relay")
    await relay.asave()
    await UsageWindow.objects.acreate(
        account=relay, key=UsageWindow.PROBE, utilization=Decimal("0.40")
    )

    route = respx.post("https://relay.test/anthropic/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={"id": "msg_relay", "type": "message", "model": "claude-sonnet-4-5"},
            headers={"content-type": "application/json"},
        )
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 8, "messages": []}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == "msg_relay"
    assert route.calls[0].request.headers["x-api-key"] == "sk-relay"


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_proxy_streams_and_records_usage(bridge_settings):
    await make_official()
    sse = (
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"model":"claude-sonnet-4-5","usage":{"input_tokens":9}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta",'
        b'"usage":{"output_tokens":4}}\n\n'
    )
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200, content=sse, headers={"content-type": "text/event-stream"}
        )
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/messages",
        data=json.dumps(
            {"model": "claude-sonnet-4-5", "max_tokens": 8, "messages": [], "stream": True}
        ),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )
    assert response.status_code == 200
    chunks = [chunk async for chunk in response.streaming_content]
    assert b"".join(chunks) == sse

    log = await RequestLog.objects.aget()
    assert log.input_tokens == 9
    assert log.output_tokens == 4


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_proxy_fails_over_after_upstream_429(bridge_settings):
    first = await make_official("first", priority=1)
    await make_official("second", priority=2)
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        side_effect=[
            httpx.Response(
                429,
                json={
                    "type": "error",
                    "error": {"type": "rate_limit_error", "message": "quota"},
                },
                headers={
                    "content-type": "application/json",
                    "anthropic-ratelimit-unified-5h-utilization": "0.95",
                },
            ),
            httpx.Response(
                200,
                json={"id": "msg_second", "type": "message", "model": "claude-sonnet-4-5"},
                headers={"content-type": "application/json"},
            ),
        ]
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 8, "messages": []}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "msg_second"
    assert route.call_count == 2
    cooled = await Account.objects.aget(pk=first.pk)
    assert cooled.cooldown_until is not None
    window = await UsageWindow.objects.aget(account_id=first.pk, key=UsageWindow.FIVE_HOURS)
    assert window.utilization == Decimal("0.950000")


@pytest.mark.django_db(transaction=True)
async def test_status_reports_reserve_limit(bridge_settings):
    await make_official()
    client = AsyncClient()

    unauthorized = await client.get("/api/status")
    assert unauthorized.status_code == 401

    response = await client.get("/api/status", headers={"x-bridge-token": "test-token"})
    assert response.status_code == 200
    account = response.json()["accounts"][0]
    assert account["limit"] == pytest.approx(0.945)
    assert account["available"] is True


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_proxy_routes_openai_responses(bridge_settings):
    account = await Account.objects.acreate(
        name="pp",
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://pp.test",
        budget_usd=Decimal("100"),
    )
    account.set_credential("sk-pp")
    await account.asave()
    await UsageWindow.objects.acreate(
        account=account, key=UsageWindow.PROBE, utilization=Decimal("0.10")
    )

    route = respx.post("https://pp.test/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "model": "gpt-6.1-sol",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
            headers={"content-type": "application/json"},
        )
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/responses",
        data=json.dumps({"model": "gpt-6.1-sol", "input": "hi"}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "resp_1"
    upstream_request = route.calls[0].request
    assert upstream_request.headers["authorization"] == "Bearer sk-pp"
    assert "anthropic-version" not in upstream_request.headers


@pytest.mark.django_db(transaction=True)
async def test_anthropic_request_without_anthropic_account_returns_503(bridge_settings):
    account = await Account.objects.acreate(
        name="pp",
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_NONE,
        base_url="https://pp.test",
    )
    account.set_credential("sk-pp")
    await account.asave()

    client = AsyncClient()
    response = await client.post(
        "/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 8, "messages": []}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )
    assert response.status_code == 503
    assert "anthropic" in response.json()["error"]["message"]


@pytest.mark.django_db(transaction=True)
async def test_logs_endpoint_separates_channels(bridge_settings):
    account = await make_official()
    account.channel = "official"
    await account.asave()
    await RequestLog.objects.acreate(
        account=account,
        account_name=account.name,
        channel="official",
        tier=Account.TIER_OFFICIAL,
        base_url="https://api.anthropic.com",
        path="/v1/messages",
        model="claude-sonnet-4-5",
        status_code=200,
    )

    client = AsyncClient()
    response = await client.get(
        "/api/logs?channel=official", headers={"x-bridge-token": "test-token"}
    )
    assert response.status_code == 200
    logs = response.json()["logs"]
    assert logs and logs[0]["channel"] == "official"

    empty = await client.get(
        "/api/logs?channel=other", headers={"x-bridge-token": "test-token"}
    )
    assert empty.json()["logs"] == []

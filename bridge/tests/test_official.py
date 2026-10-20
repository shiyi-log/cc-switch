import json
from decimal import Decimal

import httpx
import pytest
import respx
from django.test import AsyncClient, override_settings
from django.utils import timezone

from bridge.models import Account, UsageWindow

BASE_BRIDGE = {
    "TOKEN": "test-token",
    "MAX_ATTEMPTS": 3,
    "TIMEOUT_SECONDS": 30,
    "RESERVE_RATIO": "0.05",
    "SAFETY_MARGIN": "0.005",
    "PROBE_TTL_SECONDS": 300,
    "PROBE_REQUIRED": True,
    "PROBE_TIMEOUT_SECONDS": 5,
    "EXPLORE_RATE": 0,
    "OFFICIAL_QUOTA_TTL_SECONDS": 60,
}


def write_auth(path, token="official-token", account_id="acct-1"):
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": token,
                    "account_id": account_id,
                    "refresh_token": "r",
                },
            }
        )
    )
    return path


@pytest.fixture
def official_settings(settings, tmp_path):
    path = write_auth(tmp_path / "auth.json")
    settings.BRIDGE = {**BASE_BRIDGE, "CODEX_AUTH_PATH": str(path)}
    return settings


async def make_official(utilization=None):
    account = await Account.objects.acreate(
        name="official",
        tier=Account.TIER_OFFICIAL,
        protocol=Account.PROTOCOL_CODEX,
        auth_type=Account.AUTH_OAUTH,
        quota_mode=Account.QUOTA_OFFICIAL,
        base_url="https://chatgpt.com/backend-api/codex",
        priority=1,
        last_probe_at=timezone.now(),
    )
    if utilization is not None:
        await UsageWindow.objects.acreate(
            account=account,
            key=UsageWindow.OFFICIAL_5H,
            utilization=Decimal(utilization),
        )
    return account


async def make_relay():
    account = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test",
        budget_usd=Decimal("100"),
    )
    account.set_credential("sk-relay")
    await account.asave()
    await UsageWindow.objects.acreate(
        account=account, key=UsageWindow.PROBE, utilization=Decimal("0.10")
    )
    return account


@pytest.mark.django_db(transaction=True)
async def test_official_without_quota_is_not_used(official_settings):
    await make_official()
    official = await Account.objects.prefetch_related("windows").aget(name="official")
    assert official.is_available() is False


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_official_is_preferred_below_reserve(official_settings):
    await make_official("0.50")
    await make_relay()
    route = respx.post("https://chatgpt.com/backend-api/codex/responses").mock(
        return_value=httpx.Response(
            200,
            json={"id": "resp_official", "object": "response", "model": "gpt-6.1-sol"},
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
    assert response.json()["id"] == "resp_official"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer official-token"
    assert request.headers["chatgpt-account-id"] == "acct-1"


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_official_at_99_percent_falls_back_to_relay(official_settings):
    await make_official("0.99")
    await make_relay()
    route = respx.post("https://relay.test/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"id": "resp_relay", "object": "response", "model": "gpt-6.1-sol"},
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
    assert response.json()["id"] == "resp_relay"
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-relay"


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_official_sse_with_json_content_type_records_usage(official_settings):
    import asyncio

    from bridge.models import RequestLog

    await make_official("0.10")
    sse = (
        b'event: response.created\ndata: {"type":"response.created",'
        b'"response":{"model":"gpt-6.1-sol"}}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed",'
        b'"response":{"model":"gpt-6.1-sol","usage":'
        b'{"input_tokens":7,"output_tokens":11}}}\n\n'
    )
    respx.post("https://chatgpt.com/backend-api/codex/responses").mock(
        return_value=httpx.Response(
            200, content=sse, headers={"content-type": "application/json"}
        )
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/responses",
        data=json.dumps(
            {"model": "gpt-6.1-sol", "input": [{"role": "user", "content": []}], "stream": True}
        ),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )
    assert response.status_code == 200
    _ = [chunk async for chunk in response.streaming_content]
    await asyncio.sleep(0.2)

    log = await RequestLog.objects.filter(path__endswith="/responses").order_by("-created_at").afirst()
    assert log is not None
    assert log.input_tokens == 7
    assert log.output_tokens == 11


@pytest.mark.django_db(transaction=True)
@override_settings(BRIDGE={**BASE_BRIDGE})
async def test_soft_reserve_is_last_resort_not_hard_block(official_settings):
    from asgiref.sync import sync_to_async

    from bridge.models import UsageWindow
    from bridge.quotas import release_account, reserve_next_account

    official = await Account.objects.acreate(
        name="official",
        tier=Account.TIER_OFFICIAL,
        protocol=Account.PROTOCOL_CODEX,
        auth_type=Account.AUTH_OAUTH,
        quota_mode=Account.QUOTA_OFFICIAL,
        base_url="https://chatgpt.com/backend-api/codex",
        reserve_ratio=Decimal("0.02"),
        safety_margin=Decimal("0"),
        soft_reserve_ratio=Decimal("0.05"),
        priority=1,
        last_probe_at=timezone.now(),
    )
    await UsageWindow.objects.acreate(
        account=official,
        key=UsageWindow.OFFICIAL_5H,
        utilization=Decimal("0.96"),
    )
    relay = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_NONE,
        base_url="https://relay.test",
        priority=10,
    )

    picked = await sync_to_async(reserve_next_account)(
        (), protocols=[Account.PROTOCOL_CODEX, Account.PROTOCOL_OPENAI], model="gpt-6.1-sol"
    )
    assert picked.name == "relay"
    await sync_to_async(release_account)(picked.pk)

    await Account.objects.filter(pk=relay.pk).aupdate(enabled=False)
    picked = await sync_to_async(reserve_next_account)(
        (), protocols=[Account.PROTOCOL_CODEX, Account.PROTOCOL_OPENAI], model="gpt-6.1-sol"
    )
    assert picked.name == "official"
    await sync_to_async(release_account)(picked.pk)


@pytest.mark.django_db(transaction=True)
@override_settings(BRIDGE={**BASE_BRIDGE})
async def test_hard_reserve_blocks_official_entirely(official_settings):
    from asgiref.sync import sync_to_async

    from bridge.models import UsageWindow
    from bridge.quotas import reserve_next_account

    official = await Account.objects.acreate(
        name="official",
        tier=Account.TIER_OFFICIAL,
        protocol=Account.PROTOCOL_CODEX,
        auth_type=Account.AUTH_OAUTH,
        quota_mode=Account.QUOTA_OFFICIAL,
        base_url="https://chatgpt.com/backend-api/codex",
        reserve_ratio=Decimal("0.02"),
        safety_margin=Decimal("0"),
        soft_reserve_ratio=Decimal("0.05"),
        priority=1,
        last_probe_at=timezone.now(),
    )
    await UsageWindow.objects.acreate(
        account=official,
        key=UsageWindow.OFFICIAL_5H,
        utilization=Decimal("0.99"),
    )
    picked = await sync_to_async(reserve_next_account)(
        (), protocols=[Account.PROTOCOL_CODEX], model="gpt-6.1-sol"
    )
    assert picked is None


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_capacity_stream_event_fails_over(official_settings):
    from bridge.models import RequestLog

    official = await make_official("0.10")
    relay = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_NONE,
        base_url="https://relay.test",
        priority=10,
    )
    relay.set_credential("sk-relay")
    await relay.asave()

    respx.post("https://chatgpt.com/backend-api/codex/responses").mock(
        return_value=httpx.Response(
            200,
            content=(
                b'event: response.failed\ndata: {"type":"response.failed",'
                b'"response":{"error":{"message":"Selected model is at capacity."}}}\n\n'
            ),
            headers={"content-type": "application/json"},
        )
    )
    relay_sse = (
        b'event: response.created\ndata: {"type":"response.created",'
        b'"response":{"model":"gpt-6.1-sol"}}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed",'
        b'"response":{"model":"gpt-6.1-sol","usage":{"input_tokens":3,"output_tokens":2}}}\n\n'
    )
    route = respx.post("https://relay.test/v1/responses").mock(
        return_value=httpx.Response(
            200, content=relay_sse, headers={"content-type": "text/event-stream"}
        )
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/responses",
        data=json.dumps({"model": "gpt-6.1-sol", "input": "hi", "stream": True}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )
    assert response.status_code == 200
    _ = [chunk async for chunk in response.streaming_content]
    assert route.call_count == 1

    refreshed = await Account.objects.aget(pk=official.pk)
    assert refreshed.cooldown_until is not None


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_official_stream_without_completion_fails_over(official_settings):
    official = await make_official("0.10")
    relay = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_NONE,
        base_url="https://relay.test",
        priority=10,
    )
    relay.set_credential("sk-relay")
    await relay.asave()

    respx.post("https://chatgpt.com/backend-api/codex/responses").mock(
        return_value=httpx.Response(
            200,
            content=(
                b'event: response.created\ndata: {"type":"response.created",'
                b'"response":{"model":"gpt-6.1-sol"}}\n\n'
            ),
            headers={"content-type": "application/json"},
        )
    )
    route = respx.post("https://relay.test/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"id": "resp_relay", "object": "response", "model": "gpt-6.1-sol"},
            headers={"content-type": "application/json"},
        )
    )

    client = AsyncClient()
    response = await client.post(
        "/v1/responses",
        data=json.dumps({"model": "gpt-6.1-sol", "input": "hi", "stream": True}),
        content_type="application/json",
        headers={"x-bridge-token": "test-token"},
    )
    assert response.status_code == 200
    assert route.call_count == 1
    refreshed = await Account.objects.aget(pk=official.pk)
    assert refreshed.cooldown_until is not None

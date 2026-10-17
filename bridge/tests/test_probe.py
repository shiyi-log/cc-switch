from decimal import Decimal

import httpx
import pytest
import respx

from bridge.models import Account, UsageWindow
from bridge.probe import extract_active, extract_number, probe_account, refresh_stale_probes

BRIDGE = {
    "TOKEN": "test-token",
    "PROBE_TTL_SECONDS": 300,
    "PROBE_TIMEOUT_SECONDS": 5,
    "PROBE_REQUIRED": True,
    "RESERVE_RATIO": "0.05",
    "SAFETY_MARGIN": "0.005",
    "MAX_ATTEMPTS": 3,
    "TIMEOUT_SECONDS": 30,
}


def test_extract_deepseek_balance_shape():
    data = {
        "is_available": True,
        "balance_infos": [{"currency": "USD", "total_balance": "12.50"}],
    }
    assert extract_number(data, ["balance", "balance_infos[*].total_balance"]) == 12.5
    assert extract_active(data, "is_available") is True


def test_extract_plain_balance_shape():
    data = {"balance": 3.25, "is_active": False}
    assert extract_number(data, ["balance"]) == 3.25
    assert extract_active(data, "is_active") is False


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_probe_updates_utilization(settings):
    settings.BRIDGE = dict(BRIDGE)
    account = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test",
        budget_usd=Decimal("100"),
    )
    account.set_credential("sk-relay")
    await account.asave()

    route = respx.get("https://relay.test/user/balance").mock(
        return_value=httpx.Response(
            200,
            json={
                "is_available": True,
                "balance_infos": [{"currency": "USD", "total_balance": "7.5"}],
            },
        )
    )

    assert await probe_account(account.pk, force=True) is True
    window = await UsageWindow.objects.aget(account_id=account.pk, key=UsageWindow.PROBE)
    assert window.utilization == Decimal("0.925000")
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-relay"

    refreshed = await Account.objects.aget(pk=account.pk)
    assert refreshed.last_probe_error == ""


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_probe_failure_keeps_account_but_records_error(settings):
    settings.BRIDGE = dict(BRIDGE)
    account = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test",
        budget_usd=Decimal("100"),
    )
    account.set_credential("sk-relay")
    await account.asave()
    respx.get("https://relay.test/user/balance").mock(
        return_value=httpx.Response(500, text="boom")
    )

    assert await probe_account(account.pk, force=True) is False
    refreshed = await Account.objects.aget(pk=account.pk)
    assert "500" in refreshed.last_probe_error
    assert refreshed.last_probe_at is not None


@pytest.mark.django_db(transaction=True)
@respx.mock
async def test_refresh_stale_probes_skips_fresh(settings):
    settings.BRIDGE = dict(BRIDGE)
    account = await Account.objects.acreate(
        name="relay",
        tier=Account.TIER_THIRD_PARTY,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test",
        budget_usd=Decimal("100"),
    )
    respx.get("https://relay.test/user/balance").mock(
        return_value=httpx.Response(200, json={"balance": 50})
    )
    assert await refresh_stale_probes() == 1
    assert await refresh_stale_probes() == 0

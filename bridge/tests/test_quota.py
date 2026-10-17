from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from bridge.models import Account, UsageWindow
from bridge.quotas import (
    available_accounts,
    parse_ratio,
    parse_timestamp,
    parse_unified_headers,
    record_budget_spend,
    record_probe_usage,
    reserve_next_account,
    release_account,
)

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


def make_account(name, **kwargs):
    defaults = {
        "tier": Account.TIER_OFFICIAL,
        "auth_type": Account.AUTH_OAUTH,
        "quota_mode": Account.QUOTA_HEADERS,
    }
    defaults.update(kwargs)
    return Account.objects.create(name=name, **defaults)


def test_parse_ratio_variants():
    assert parse_ratio("0.42") == pytest.approx(0.42)
    assert parse_ratio("42%") == pytest.approx(0.42)
    assert parse_ratio(42) == pytest.approx(0.42)
    assert parse_ratio("1") == pytest.approx(1.0)
    assert parse_ratio("bad") is None
    assert parse_ratio(None) is None
    assert parse_ratio("-1") == 0.0


def test_parse_timestamp_iso():
    parsed = parse_timestamp("2026-10-17T12:00:00Z")
    assert parsed == datetime(2026, 10, 17, 12, tzinfo=dt_timezone.utc)
    assert parse_timestamp("") is None


def test_parse_unified_headers():
    parsed = parse_unified_headers(
        {
            "anthropic-ratelimit-unified-5h-utilization": "0.94",
            "anthropic-ratelimit-unified-7d-utilization": "12%",
            "anthropic-ratelimit-unified-status": "allowed_warning",
        }
    )
    assert parsed["5h"]["utilization"] == pytest.approx(0.94)
    assert parsed["7d"]["utilization"] == pytest.approx(0.12)
    assert parsed["5h"]["status"] == "allowed_warning"


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_reserve_threshold_uses_five_percent():
    account = make_account("official")
    UsageWindow.objects.create(
        account=account, key=UsageWindow.FIVE_HOURS, utilization=Decimal("0.96")
    )
    account = Account.objects.prefetch_related("windows").get(pk=account.pk)
    assert account.quota_limit() == Decimal("0.945")
    assert account.at_reserve() is True
    assert account.is_available() is False


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_account_below_reserve_is_available():
    account = make_account("official")
    UsageWindow.objects.create(
        account=account, key=UsageWindow.FIVE_HOURS, utilization=Decimal("0.90")
    )
    account = Account.objects.prefetch_related("windows").get(pk=account.pk)
    assert account.is_available() is True


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_official_is_preferred_over_third_party():
    official = make_account("official")
    third = make_account(
        "relay",
        tier=Account.TIER_THIRD_PARTY,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test",
        budget_usd=Decimal("100"),
    )
    record_probe_usage(third, remaining=90, total=None, active=True)
    UsageWindow.objects.create(
        account=official, key=UsageWindow.FIVE_HOURS, utilization=Decimal("0.10")
    )
    ordered = available_accounts()
    assert [a.name for a in ordered] == ["official", "relay"]


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_third_party_used_when_official_is_at_reserve():
    official = make_account("official")
    third = make_account(
        "relay",
        tier=Account.TIER_THIRD_PARTY,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test",
        budget_usd=Decimal("100"),
    )
    record_probe_usage(third, remaining=90, total=None, active=True)
    UsageWindow.objects.create(
        account=official, key=UsageWindow.FIVE_HOURS, utilization=Decimal("0.99")
    )
    ordered = available_accounts()
    assert [a.name for a in ordered] == ["relay"]


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_probe_account_requires_balance_before_use():
    third = make_account(
        "relay",
        tier=Account.TIER_THIRD_PARTY,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_PROBE,
        base_url="https://relay.test",
        budget_usd=Decimal("100"),
    )
    third = Account.objects.prefetch_related("windows").get(pk=third.pk)
    assert third.is_available() is False

    record_probe_usage(third, remaining=90, total=None, active=True)
    third = Account.objects.prefetch_related("windows").get(pk=third.pk)
    assert third.is_available() is True


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_budget_mode_stops_at_reserve():
    account = make_account(
        "api-key",
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_BUDGET,
        budget_usd=Decimal("10"),
    )
    record_budget_spend(account, Decimal("9.6"))
    account = Account.objects.prefetch_related("windows").get(pk=account.pk)
    assert account.utilization() == Decimal("0.960000")
    assert account.is_available() is False


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_reserve_next_account_increments_inflight():
    make_account("official")
    account = reserve_next_account()
    assert account is not None
    assert Account.objects.get(pk=account.pk).inflight == 1
    release_account(account.pk)
    assert Account.objects.get(pk=account.pk).inflight == 0

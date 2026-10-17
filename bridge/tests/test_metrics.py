from decimal import Decimal

import pytest
from django.test import override_settings

from bridge.metrics import rank_accounts, update_account_stats
from bridge.models import Account
from bridge.quotas import release_account, reserve_next_account

BRIDGE = {
    "TOKEN": "test-token",
    "ROUTING_MODE": "dynamic",
    "WEIGHT_COST": "0.40",
    "WEIGHT_TTFT": "0.30",
    "WEIGHT_SPEED": "0.20",
    "WEIGHT_ERROR": "0.10",
    "WEIGHT_PRIORITY": "0.05",
    "STATS_MIN_SAMPLES": "3",
    "RESERVE_RATIO": "0.05",
    "SAFETY_MARGIN": "0.005",
}


def relay(name, **kwargs):
    defaults = dict(
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        auth_type=Account.AUTH_API_KEY,
        quota_mode=Account.QUOTA_NONE,
        enabled=True,
    )
    defaults.update(kwargs)
    return Account.objects.create(name=name, **defaults)


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_rank_prefers_cheaper_and_faster_channel():
    fast = relay(
        "fast",
        ewma_ttft_ms=400,
        ewma_tps=60,
        ewma_cost_per_1k=Decimal("0.005"),
        ewma_success=1.0,
        stats_samples=5,
    )
    slow = relay(
        "slow",
        ewma_ttft_ms=2500,
        ewma_tps=12,
        ewma_cost_per_1k=Decimal("0.030"),
        ewma_success=1.0,
        stats_samples=5,
    )
    ranked = rank_accounts([slow, fast], "gpt-6.1-sol")
    assert [a.name for a in ranked] == ["fast", "slow"]


@override_settings(BRIDGE={**BRIDGE, "ROUTING_MODE": "priority"})
@pytest.mark.django_db
def test_priority_mode_keeps_priority_order():
    fast = relay("fast", priority=200, ewma_ttft_ms=100, stats_samples=5)
    slow = relay("slow", priority=10, ewma_ttft_ms=3000, stats_samples=5)
    ranked = rank_accounts([fast, slow], "gpt-6.1-sol")
    assert [a.name for a in ranked] == ["slow", "fast"]


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_update_account_stats_tracks_ttft_and_tps():
    account = relay("relay")
    update_account_stats(
        account.pk,
        ttft_ms=1000,
        duration_ms=3000,
        total_tokens=1100,
        output_tokens=100,
        cost_usd=Decimal("0.01"),
    )
    account.refresh_from_db()
    assert account.stats_samples == 1
    assert account.ewma_ttft_ms == pytest.approx(1000)
    assert account.ewma_tps == pytest.approx(50)
    assert account.ewma_cost_per_1k == Decimal("0.009091")
    assert account.ewma_success == pytest.approx(1.0)


@override_settings(BRIDGE=BRIDGE)
@pytest.mark.django_db
def test_reserve_next_account_uses_dynamic_score():
    fast = relay("fast", priority=20, ewma_ttft_ms=300, ewma_tps=80, stats_samples=5)
    slow = relay("slow", priority=10, ewma_ttft_ms=3000, ewma_tps=10, stats_samples=5)

    account = reserve_next_account(protocols=[Account.PROTOCOL_OPENAI], model="gpt-6.1-sol")
    assert account is not None
    assert account.name == "fast"
    release_account(account.pk)

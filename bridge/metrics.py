"""Rolling per-account performance metrics and dynamic routing scores."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .models import Account, ModelPrice

logger = logging.getLogger("bridge")

PRIOR_TTFT_MS = 2000.0
PRIOR_TPS = 25.0
PRIOR_COST_PER_1K = 0.02
ALPHA = 0.25
EXPLORE_BONUS = 0.85


def _blend(old, new):
    if new is None:
        return old
    if old is None:
        return new
    return ALPHA * new + (1 - ALPHA) * old


def update_account_stats(
    account_id,
    *,
    ttft_ms=None,
    duration_ms=None,
    total_tokens=0,
    output_tokens=0,
    cost_usd=None,
    success=True,
) -> None:
    account = Account.objects.filter(pk=account_id).first()
    if account is None:
        return

    ttft = float(ttft_ms) if ttft_ms else None
    tps = None
    if output_tokens and duration_ms:
        generation_ms = max(1.0, float(duration_ms) - float(ttft_ms or 0))
        tps = float(output_tokens) / (generation_ms / 1000.0)

    if total_tokens and cost_usd is not None:
        per_1k = float(cost_usd) / float(total_tokens) * 1000.0
        account.ewma_cost_per_1k = Decimal(str(per_1k)).quantize(Decimal("0.000001"))

    account.ewma_ttft_ms = _blend(account.ewma_ttft_ms, ttft)
    account.ewma_tps = _blend(account.ewma_tps, tps)
    account.ewma_success = _blend(account.ewma_success, 1.0 if success else 0.0)
    account.stats_samples = (account.stats_samples or 0) + 1
    account.stats_updated_at = timezone.now()
    account.save(
        update_fields=[
            "ewma_ttft_ms",
            "ewma_tps",
            "ewma_cost_per_1k",
            "ewma_success",
            "stats_samples",
            "stats_updated_at",
        ]
    )


def _reference_cost_per_1k(model: str) -> float:
    if model:
        for price in ModelPrice.objects.all():
            if model.startswith(price.model_prefix):
                blended = (float(price.input_per_mtok) + float(price.output_per_mtok)) / 2.0
                return blended / 1000.0
    return PRIOR_COST_PER_1K


def _metrics(account: Account, reference_cost: float) -> dict:
    samples = account.stats_samples or 0
    knows = samples > 0
    return {
        "ttft": (account.ewma_ttft_ms or PRIOR_TTFT_MS) if knows else PRIOR_TTFT_MS,
        "tps": (account.ewma_tps or PRIOR_TPS) if knows else PRIOR_TPS,
        "cost": float(account.ewma_cost_per_1k)
        if knows and account.ewma_cost_per_1k is not None
        else reference_cost * float(account.price_multiplier or 1),
        "error": 1.0 - (account.ewma_success if knows and account.ewma_success is not None else 1.0),
        "priority": float(account.priority or 100),
        "samples": samples,
    }


def rank_accounts(accounts: list[Account], model: str = "") -> list[Account]:
    """Sort candidates by a weighted score: cheaper / faster / more reliable first."""
    mode = (settings.BRIDGE.get("ROUTING_MODE") or "dynamic").lower()
    if mode != "dynamic" or len(accounts) <= 1:
        return sorted(accounts, key=lambda a: (a.priority, a.name))

    reference_cost = _reference_cost_per_1k(model)
    metrics = {account.pk: _metrics(account, reference_cost) for account in accounts}

    max_ttft = max(m["ttft"] for m in metrics.values()) or 1.0
    max_cost = max(m["cost"] for m in metrics.values()) or 1.0
    max_tps = max(m["tps"] for m in metrics.values()) or 1.0
    max_priority = max(m["priority"] for m in metrics.values()) or 1.0

    weights = settings.BRIDGE
    w_cost = float(weights.get("WEIGHT_COST", 0.4))
    w_ttft = float(weights.get("WEIGHT_TTFT", 0.3))
    w_speed = float(weights.get("WEIGHT_SPEED", 0.2))
    w_error = float(weights.get("WEIGHT_ERROR", 0.1))
    w_priority = float(weights.get("WEIGHT_PRIORITY", 0.05))
    min_samples = int(weights.get("STATS_MIN_SAMPLES", 3))

    def score(account: Account) -> tuple[float, int, str]:
        m = metrics[account.pk]
        value = (
            w_cost * (m["cost"] / max_cost)
            + w_ttft * (m["ttft"] / max_ttft)
            + w_speed * (max_tps / m["tps"])
            + w_error * m["error"]
            + w_priority * (m["priority"] / max_priority)
        )
        if m["samples"] < min_samples:
            value *= EXPLORE_BONUS  # give new channels a chance
        return (value, account.priority, account.name)

    return sorted(accounts, key=score)

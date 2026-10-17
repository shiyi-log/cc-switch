"""Rolling per-account performance metrics and dynamic routing."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .models import Account, ModelPrice

logger = logging.getLogger("bridge")

PRIOR_TTFT_MS = 2000.0
PRIOR_TPS = 25.0
PRIOR_COST_PER_1K = 0.02
EXPLORE_BONUS = 0.85
SLOW_PENALTY = 5.0


def _setting(name, default):
    return settings.BRIDGE.get(name, default)


def _blend(old, new, alpha):
    if new is None:
        return old
    if old is None:
        return new
    return alpha * new + (1 - alpha) * old


def update_account_stats(
    account_id,
    *,
    ttft_ms=None,
    duration_ms=None,
    total_tokens=0,
    output_tokens=0,
    cost_usd=None,
    success=True,
) -> bool:
    """Update EWMAs and flag a request as slow so routing can react at once."""
    account = Account.objects.filter(pk=account_id).first()
    if account is None:
        return False

    alpha = float(_setting("ROUTING_ALPHA", 0.6))
    slow_ttft = float(_setting("SLOW_TTFT_MS", 5000))
    slow_tps = float(_setting("SLOW_MIN_TPS", 15))

    ttft = float(ttft_ms) if ttft_ms else None
    tps = None
    if output_tokens >= 20 and duration_ms:
        generation_ms = float(duration_ms) - float(ttft_ms or 0)
        if generation_ms >= 200:
            tps = min(200.0, float(output_tokens) / (generation_ms / 1000.0))

    slow = False
    if ttft is not None and ttft > slow_ttft:
        slow = True
    if tps is not None and output_tokens >= 50 and tps < slow_tps:
        slow = True

    if total_tokens and cost_usd is not None:
        per_1k = float(cost_usd) / float(total_tokens) * 1000.0
        account.ewma_cost_per_1k = Decimal(str(per_1k)).quantize(Decimal("0.000001"))

    account.ewma_ttft_ms = _blend(account.ewma_ttft_ms, ttft, alpha)
    account.ewma_tps = _blend(account.ewma_tps, tps, alpha)
    account.ewma_success = _blend(account.ewma_success, 1.0 if success else 0.0, alpha)
    account.stats_samples = (account.stats_samples or 0) + 1
    account.stats_updated_at = timezone.now()
    if slow:
        account.last_slow_at = timezone.now()
        logger.warning(
            "slow request on %s: ttft=%sms tps=%s", account.name, ttft, tps
        )
    account.save(
        update_fields=[
            "ewma_ttft_ms",
            "ewma_tps",
            "ewma_cost_per_1k",
            "ewma_success",
            "stats_samples",
            "stats_updated_at",
            "last_slow_at",
        ]
    )
    return slow


def _reference_cost_per_1k(model: str) -> float:
    if model:
        for price in ModelPrice.objects.all():
            if model.startswith(price.model_prefix):
                blended = (float(price.input_per_mtok) + float(price.output_per_mtok)) / 2.0
                return blended / 1000.0
    return PRIOR_COST_PER_1K


def _scored(accounts: list[Account], model: str) -> list[tuple[float, Account]]:
    reference_cost = _reference_cost_per_1k(model)
    metrics = {}
    now = timezone.now()
    penalty_seconds = int(_setting("SLOW_PENALTY_SECONDS", 60))
    min_samples = int(_setting("STATS_MIN_SAMPLES", 3))

    known_ttft = [
        a.ewma_ttft_ms
        for a in accounts
        if a.stats_samples and a.ewma_ttft_ms
    ]
    known_tps = [
        a.ewma_tps for a in accounts if a.stats_samples and a.ewma_tps
    ]
    known_costs = [
        float(a.ewma_cost_per_1k)
        for a in accounts
        if a.stats_samples and a.ewma_cost_per_1k is not None
    ]
    optimistic_ttft = min(known_ttft) if known_ttft else PRIOR_TTFT_MS
    optimistic_tps = max(known_tps) if known_tps else PRIOR_TPS
    optimistic_cost = min(known_costs) if known_costs else reference_cost

    for account in accounts:
        samples = account.stats_samples or 0
        knows = samples > 0
        ttft = (
            (account.ewma_ttft_ms or optimistic_ttft)
            if knows
            else optimistic_ttft
        )
        tps = (
            (account.ewma_tps or optimistic_tps) if knows else optimistic_tps
        )
        if account.observed_cost_per_1k is not None:
            cost = float(account.observed_cost_per_1k)
        elif knows and account.ewma_cost_per_1k is not None:
            cost = float(account.ewma_cost_per_1k)
        else:
            cost = optimistic_cost * float(account.price_multiplier or 1)
        error = 1.0 - (
            account.ewma_success if knows and account.ewma_success is not None else 1.0
        )
        recent_slow = bool(
            account.last_slow_at
            and (now - account.last_slow_at).total_seconds() < penalty_seconds
        )
        metrics[account.pk] = {
            "ttft": ttft,
            "tps": tps,
            "cost": cost,
            "error": error,
            "priority": float(account.priority or 100),
            "samples": samples,
            "recent_slow": recent_slow,
        }

    max_ttft = max(m["ttft"] for m in metrics.values()) or 1.0
    min_ttft = min(m["ttft"] for m in metrics.values())
    max_cost = max(m["cost"] for m in metrics.values()) or 1.0
    min_cost = min(m["cost"] for m in metrics.values())
    max_tps = max(m["tps"] for m in metrics.values()) or 1.0
    min_tps = min(m["tps"] for m in metrics.values())
    max_priority = max(m["priority"] for m in metrics.values()) or 1.0

    # Speed first: TTFT + throughput dominate; price is a tie-breaker.
    w_cost = float(_setting("WEIGHT_COST", 0.2))
    w_ttft = float(_setting("WEIGHT_TTFT", 0.5))
    w_speed = float(_setting("WEIGHT_SPEED", 0.25))
    w_error = float(_setting("WEIGHT_ERROR", 0.05))
    w_priority = float(_setting("WEIGHT_PRIORITY", 0.0))

    scored = []
    for account in accounts:
        m = metrics[account.pk]
        value = (
            w_cost * ((m["cost"] - min_cost) / (max_cost - min_cost + 1e-9))
            + w_ttft * ((m["ttft"] - min_ttft) / (max_ttft - min_ttft + 1e-9))
            + w_speed * ((max_tps - m["tps"]) / (max_tps - min_tps + 1e-9))
            + w_error * m["error"]
            + w_priority * (m["priority"] / max_priority)
        )
        if m["samples"] < min_samples:
            value *= EXPLORE_BONUS
        if m["recent_slow"]:
            value += SLOW_PENALTY
        scored.append((value, account))
    scored.sort(key=lambda item: (item[0], item[1].priority, item[1].name))
    return scored


def rank_accounts(accounts: list[Account], model: str = "") -> list[Account]:
    mode = (_setting("ROUTING_MODE", "dynamic") or "dynamic").lower()
    if mode != "dynamic" or len(accounts) <= 1:
        return sorted(accounts, key=lambda a: (a.priority, a.name))
    return [account for _, account in _scored(accounts, model)]


def pick_account(accounts: list[Account], model: str = "") -> Account | None:
    """Pick best, but rotate among close candidates and occasionally explore."""
    if not accounts:
        return None
    mode = (_setting("ROUTING_MODE", "dynamic") or "dynamic").lower()
    if mode != "dynamic" or len(accounts) == 1:
        return sorted(accounts, key=lambda a: (a.priority, a.name))[0]

    scored = _scored(accounts, model)
    healthy = [
        item
        for item in scored
        if not (
            item[1].last_slow_at
            and (timezone.now() - item[1].last_slow_at).total_seconds()
            < int(_setting("SLOW_PENALTY_SECONDS", 60))
        )
    ]
    if healthy:
        scored = healthy
    best = scored[0][0]
    margin = float(_setting("ROTATION_MARGIN", 0.15))
    eligible = [account for score, account in scored if score <= best * (1 + margin) + 0.02][:3]
    if not eligible:
        eligible = [scored[0][1]]

    if len(healthy) > 1 and random.random() < float(_setting("EXPLORE_RATE", 0.3)):
        # Explore any healthy channel, not only the current top three, so new
        # providers get measured quickly.
        return random.choice([account for _, account in healthy])

    def last_used(account: Account) -> float:
        return account.last_used_at.timestamp() if account.last_used_at else 0.0

    return min(eligible, key=lambda a: (last_used(a), a.priority))

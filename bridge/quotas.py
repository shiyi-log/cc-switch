"""Quota bookkeeping: upstream headers, local budgets and third-party probes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import Account, UsageWindow

logger = logging.getLogger("bridge")

HEADER_STATUS = "anthropic-ratelimit-unified-status"
HEADER_5H = "anthropic-ratelimit-unified-5h-utilization"
HEADER_5H_RESET = "anthropic-ratelimit-unified-5h-reset"
HEADER_7D = "anthropic-ratelimit-unified-7d-utilization"
HEADER_7D_RESET = "anthropic-ratelimit-unified-7d-reset"
HEADER_RESET = "anthropic-ratelimit-unified-reset"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def parse_ratio(value) -> float | None:
    """Accept ``0.42``, ``"42%"`` or ``42`` and normalise to ``0.0..1.0``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    if percent or number > 1:
        number /= 100.0
    return max(0.0, min(1.0, number))


def parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def parse_unified_headers(headers) -> dict[str, dict]:
    """Extract 5h/7d utilisation from Anthropic subscription response headers."""
    result: dict[str, dict] = {}
    status = headers.get(HEADER_STATUS) or ""
    five_hour = parse_ratio(headers.get(HEADER_5H))
    seven_day = parse_ratio(headers.get(HEADER_7D))
    if five_hour is not None:
        result[UsageWindow.FIVE_HOURS] = {
            "utilization": five_hour,
            "resets_at": parse_timestamp(headers.get(HEADER_5H_RESET) or headers.get(HEADER_RESET)),
            "status": status,
        }
    if seven_day is not None:
        result[UsageWindow.SEVEN_DAYS] = {
            "utilization": seven_day,
            "resets_at": parse_timestamp(headers.get(HEADER_7D_RESET) or headers.get(HEADER_RESET)),
            "status": status,
        }
    return result


def apply_header_usage(account: Account, headers) -> dict[str, dict]:
    """Persist utilisation reported by the upstream, without resetting on 401/5xx."""
    parsed = parse_unified_headers(headers)
    now = timezone.now()
    for key, payload in parsed.items():
        UsageWindow.objects.update_or_create(
            account=account,
            key=key,
            defaults={
                "utilization": Decimal(str(payload["utilization"])).quantize(Decimal("0.000001")),
                "status": payload["status"][:40],
                "resets_at": payload["resets_at"],
                "raw": {
                    "status": payload["status"],
                    "source": "headers",
                    "seen_at": now.isoformat(),
                },
            },
        )
    return parsed


# ---------------------------------------------------------------------------
# Local budget accounting
# ---------------------------------------------------------------------------
def _next_month_start(moment: datetime) -> datetime:
    year, month = moment.year, moment.month
    if month == 12:
        return moment.replace(
            year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return moment.replace(
        month=month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )


def record_budget_spend(account: Account, cost_usd: Decimal) -> UsageWindow:
    now = timezone.now()
    with transaction.atomic():
        window, _ = UsageWindow.objects.get_or_create(
            account=account,
            key=UsageWindow.BUDGET,
            defaults={"resets_at": _next_month_start(now)},
        )
        if connection.features.has_select_for_update:
            window = UsageWindow.objects.select_for_update().get(pk=window.pk)
        if window.resets_at and now >= window.resets_at:
            window.cost_usd = Decimal("0")
            window.resets_at = _next_month_start(now)
        window.cost_usd = (window.cost_usd or Decimal("0")) + cost_usd
        budget = account.budget_usd or Decimal("0")
        if budget > 0:
            window.utilization = min(
                Decimal("1.500000"), (window.cost_usd / budget).quantize(Decimal("0.000001"))
            )
        else:
            window.utilization = Decimal("0")
        window.raw = {"source": "local", "budget_usd": str(budget)}
        window.save()
    return window


def record_probe_usage(
    account: Account,
    remaining: float | None,
    total: float | None,
    active: bool | None,
    cost_per_1k: float | None = None,
) -> UsageWindow:
    """Persist a third-party balance probe result as a 0..1 utilisation."""
    now = timezone.now()
    reference_total = total if total and total > 0 else (
        float(account.budget_usd) if account.budget_usd else None
    )
    utilization = Decimal("0")
    if active is False:
        utilization = Decimal("1")
    elif remaining is not None and reference_total and reference_total > 0:
        ratio = 1.0 - (remaining / reference_total)
        utilization = Decimal(str(max(0.0, min(1.5, ratio)))).quantize(Decimal("0.000001"))

    window, _ = UsageWindow.objects.update_or_create(
        account=account,
        key=UsageWindow.PROBE,
        defaults={
            "utilization": utilization,
            "status": "ok" if active is not False else "inactive",
            "resets_at": None,
            "raw": {
                "source": "probe",
                "remaining": remaining,
                "total": reference_total,
                "active": active,
                "seen_at": now.isoformat(),
            },
        },
    )
    update_fields = {"last_probe_at": now, "last_probe_error": ""}
    if cost_per_1k is not None and cost_per_1k > 0:
        update_fields["observed_cost_per_1k"] = Decimal(str(cost_per_1k)).quantize(
            Decimal("0.000001")
        )
    Account.objects.filter(pk=account.pk).update(**update_fields)
    return window


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def _candidates(
    accounts: list[Account],
    now: datetime,
    protocols: list[str] | None = None,
    model: str = "",
    ignore_cooldown: bool = False,
) -> list[Account]:
    if protocols:
        accounts = [account for account in accounts if account.protocol in protocols]
    if model:
        accounts = [
            account
            for account in accounts
            if model not in (account.blocked_models or [])
        ]
    available = [
        account for account in accounts if account.is_available(now, ignore_cooldown)
    ]
    from .metrics import pick_account, rank_accounts

    official = [a for a in available if a.tier == Account.TIER_OFFICIAL]
    third_party = [a for a in available if a.tier == Account.TIER_THIRD_PARTY]
    official_order = rank_accounts(official, model)
    third_order = rank_accounts(third_party, model)
    if third_order:
        chosen = pick_account(third_order, model)
        if chosen is not None:
            third_order = [chosen] + [a for a in third_order if a.pk != chosen.pk]
    return official_order + third_order


def available_accounts(
    now: datetime | None = None,
    protocols: list[str] | None = None,
    model: str = "",
    ignore_cooldown: bool = False,
) -> list[Account]:
    now = now or timezone.now()
    accounts = list(
        Account.objects.filter(enabled=True).prefetch_related("windows").order_by("priority", "name")
    )
    return _candidates(accounts, now, protocols, model, ignore_cooldown)


def reserve_next_account(
    exclude_ids=(),
    now: datetime | None = None,
    protocols: list[str] | None = None,
    model: str = "",
    ignore_cooldown: bool = False,
) -> Account | None:
    """Atomically pick the best account and bump its in-flight counter."""
    now = now or timezone.now()
    excluded = set(exclude_ids)
    with transaction.atomic():
        queryset = Account.objects.filter(enabled=True)
        if connection.features.has_select_for_update:
            queryset = queryset.select_for_update()
        accounts = list(queryset.prefetch_related("windows").order_by("priority", "name"))
        candidates = [
            a
            for a in _candidates(accounts, now, protocols, model, ignore_cooldown)
            if a.pk not in excluded
        ]
        if not candidates:
            return None
        account = candidates[0]
        Account.objects.filter(pk=account.pk).update(
            inflight=F("inflight") + 1, last_used_at=now
        )
        account.inflight += 1
        return account


def release_account(account_id) -> None:
    Account.objects.filter(pk=account_id, inflight__gt=0).update(inflight=F("inflight") - 1)


def mark_failure(account_id, message: str, status_code: int | None = None) -> None:
    now = timezone.now()
    if settings.BRIDGE.get("POOL_MODE", True):
        base = int(settings.BRIDGE.get("POOL_COOLDOWN_SECONDS", 5))
        cooldown = timedelta(seconds=base if status_code != 429 else base * 2)
    elif status_code in (401, 403):
        cooldown = timedelta(minutes=10)
    elif status_code == 429:
        cooldown = timedelta(seconds=60)
    elif status_code and status_code >= 500:
        cooldown = timedelta(seconds=30)
    else:
        cooldown = timedelta(seconds=15)
    Account.objects.filter(pk=account_id).update(
        last_error=message[:2000], cooldown_until=now + cooldown
    )


def clear_failure(account_id) -> None:
    Account.objects.filter(pk=account_id).update(last_error="", cooldown_until=None)


def stale_probe_ids(now: datetime | None = None) -> list:
    now = now or timezone.now()
    ttl = int(settings.BRIDGE.get("PROBE_TTL_SECONDS", 300))
    cutoff = now - timedelta(seconds=ttl)
    return list(
        Account.objects.filter(enabled=True, quota_mode=Account.QUOTA_PROBE)
        .filter(Q(last_probe_at__isnull=True) | Q(last_probe_at__lt=cutoff))
        .values_list("id", flat=True)
    )

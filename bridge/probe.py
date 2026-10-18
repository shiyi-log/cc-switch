"""Quota probes for third-party providers without rate-limit headers.

A probe is a small HTTP request described by ``Account.quota_probe``::

    {
      "url": "{base_url}/user/balance",
      "method": "GET",
      "headers": {"Authorization": "Bearer {api_key}"},
      "remaining_paths": ["balance", "balance_infos[*].total_balance"],
      "total_path": null,
      "active_path": "is_available"
    }

Paths are dot-separated; ``[*]`` expands a list.  Numbers from every matching
leaf are summed, which covers DeepSeek-style ``balance_infos`` arrays.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings

from .models import Account
from .quotas import record_probe_usage, stale_probe_ids

logger = logging.getLogger("bridge")
_probe_lock = asyncio.Lock()


def _render(template: str, account: Account) -> str:
    return (
        str(template)
        .replace("{base_url}", account.base_url.rstrip("/"))
        .replace("{api_key}", account.credential_plain)
    )


def _resolve(node, path: str) -> list:
    """Resolve ``a.b[*].c`` against nested dicts/lists."""
    current = [node]
    for raw_part in path.split("."):
        expand = raw_part.endswith("[*]")
        part = raw_part[:-3] if expand else raw_part
        following = []
        for item in current:
            if isinstance(item, dict):
                if part not in item:
                    continue
                value = item[part]
            elif isinstance(item, list) and part.isdigit():
                index = int(part)
                if index >= len(item):
                    continue
                value = item[index]
            else:
                continue
            if expand:
                if isinstance(value, list):
                    following.extend(value)
            else:
                following.append(value)
        current = following
        if not current:
            break
    return current


def extract_number(data, paths: list[str]) -> float | None:
    for path in paths or []:
        total = 0.0
        found = False
        for value in _resolve(data, path):
            if isinstance(value, bool):
                continue
            try:
                total += float(str(value).replace(",", ""))
                found = True
            except (TypeError, ValueError):
                continue
        if found:
            return total
    return None


def extract_active(data, path: str | None) -> bool | None:
    if not path:
        return None
    for value in _resolve(data, path):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "inactive")
    return None


def _record_probe_error(account_id, message: str) -> None:
    from django.utils import timezone

    Account.objects.filter(pk=account_id).update(
        last_probe_at=timezone.now(), last_probe_error=message[:2000]
    )


async def probe_account(account_id, force: bool = False) -> bool:
    account = await sync_to_async(Account.objects.get)(pk=account_id)
    probe = account.quota_probe or {}
    url = _render(probe.get("url") or "{base_url}/user/balance", account)
    method = str(probe.get("method") or "GET").upper()
    headers = {
        str(key): _render(value, account)
        for key, value in (probe.get("headers") or {}).items()
    }

    try:
        timeout = float(settings.BRIDGE.get("PROBE_TIMEOUT_SECONDS", 8))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, headers=headers)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - surface any probe failure
        logger.warning("quota probe failed for %s: %s", account.name, exc)
        await sync_to_async(_record_probe_error)(account.pk, str(exc))
        return False

    paths = probe.get("remaining_paths") or ["balance"]
    remaining = extract_number(data, paths)
    total = extract_number(data, [probe["total_path"]]) if probe.get("total_path") else None
    active = extract_active(data, probe.get("active_path"))

    # Real provider pricing from /v1/usage model_stats (cost per 1k tokens).
    cost_per_1k = None
    stats = data.get("model_stats") if isinstance(data, dict) else None
    if isinstance(stats, list) and stats:
        target = next(
            (item for item in stats if item.get("model") == "gpt-6.1-sol"), None
        )
        if target and target.get("total_tokens"):
            cost_per_1k = float(target.get("cost") or 0) / float(target["total_tokens"]) * 1000.0
        else:
            tokens = sum(float(item.get("total_tokens") or 0) for item in stats)
            cost = sum(float(item.get("cost") or 0) for item in stats)
            if tokens:
                cost_per_1k = cost / tokens * 1000.0

    await sync_to_async(record_probe_usage)(
        account, remaining, total, active, cost_per_1k
    )
    logger.info(
        "quota probe %s: remaining=%s total=%s active=%s", account.name, remaining, total, active
    )
    return True


async def refresh_stale_probes(force: bool = False) -> int:
    async with _probe_lock:
        return await _refresh_stale_probes_locked(force)


async def _refresh_stale_probes_locked(force: bool = False) -> int:
    if force:
        ids = await sync_to_async(
            lambda: list(
                Account.objects.filter(enabled=True, quota_mode=Account.QUOTA_PROBE).values_list(
                    "id", flat=True
                )
            )
        )()
    else:
        ids = await sync_to_async(stale_probe_ids)()
    if not ids:
        return 0
    await asyncio.gather(*(probe_account(account_id, force=force) for account_id in ids))
    return len(ids)

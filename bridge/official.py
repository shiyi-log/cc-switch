"""Official ChatGPT (Codex) quota tracking.

Codex does not expose percentage headers any more, so the bridge reads the same
``wham/usage`` endpoint the desktop app uses.  The access token is read from
``~/.codex/auth.json`` on every request, so a re-login is picked up without
copying credentials into the bridge database.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

import httpx
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from .models import Account, UsageWindow

logger = logging.getLogger("bridge")


def auth_path() -> Path:
    raw = settings.BRIDGE.get("CODEX_AUTH_PATH") or "~/.codex/auth.json"
    return Path(raw).expanduser()


def load_tokens() -> tuple[str, str]:
    try:
        data = json.loads(auth_path().read_text())
    except (OSError, json.JSONDecodeError):
        return "", ""
    tokens = data.get("tokens") or {}
    return str(tokens.get("access_token") or ""), str(tokens.get("account_id") or "")


def _parse_reset(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=dt_timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _window_payloads(data: dict) -> list[tuple[str, Decimal, datetime | None, dict]]:
    rate_limit = data.get("rate_limit") or {}
    payloads: list[tuple[str, Decimal, datetime | None, dict]] = []
    for key, name in (
        (UsageWindow.OFFICIAL_5H, "primary_window"),
        (UsageWindow.OFFICIAL_7D, "secondary_window"),
    ):
        window = rate_limit.get(name)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        if used is None:
            continue
        utilization = Decimal(str(float(used) / 100.0)).quantize(Decimal("0.000001"))
        payloads.append(
            (key, utilization, _parse_reset(window.get("reset_at") or window.get("resets_at")), window)
        )
    return payloads


def _save(accounts, payloads, data: dict) -> None:
    now = timezone.now()
    plan = data.get("plan_type")
    for account in accounts:
        for key, utilization, reset, window in payloads:
            UsageWindow.objects.update_or_create(
                account=account,
                key=key,
                defaults={
                    "utilization": utilization,
                    "status": str(data.get("rate_limit_reached_type") or ""),
                    "resets_at": reset,
                    "raw": {"source": "official", "plan_type": plan, "window": window},
                },
            )
        Account.objects.filter(pk=account.pk).update(
            last_probe_at=now, last_probe_error=""
        )


def _mark_error(accounts, message: str) -> None:
    now = timezone.now()
    for account in accounts:
        Account.objects.filter(pk=account.pk).update(
            last_probe_at=now, last_probe_error=message[:2000]
        )


async def refresh_official_quotas(force: bool = False) -> int:
    accounts = await sync_to_async(
        lambda: list(
            Account.objects.filter(
                enabled=True,
                protocol=Account.PROTOCOL_CODEX,
                quota_mode=Account.QUOTA_OFFICIAL,
            )
        )
    )()
    if not accounts:
        return 0

    now = timezone.now()
    ttl = int(settings.BRIDGE.get("OFFICIAL_QUOTA_TTL_SECONDS", 60))
    stale = [
        account
        for account in accounts
        if force
        or not account.last_probe_at
        or (now - account.last_probe_at).total_seconds() >= ttl
    ]
    if not stale:
        return 0

    token, account_id = load_tokens()
    if not token:
        await sync_to_async(_mark_error)(stale, "missing Codex access token")
        return 0

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "cc-switch-bridge/1.0",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    try:
        timeout = float(settings.BRIDGE.get("PROBE_TIMEOUT_SECONDS", 8))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                settings.BRIDGE.get("OFFICIAL_QUOTA_URL"), headers=headers
            )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("official quota refresh failed: %s", exc)
        await sync_to_async(_mark_error)(stale, str(exc))
        return 0

    payloads = _window_payloads(data)
    await sync_to_async(_save)(stale, payloads, data)
    logger.info(
        "official quota: %s", [(key, str(util)) for key, util, _, _ in payloads]
    )
    return len(stale)

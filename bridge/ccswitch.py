"""Mirror bridge request logs into CC Switch's own request log table."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from django.conf import settings

from .models import Account

logger = logging.getLogger("bridge")

_KEY_CACHE: dict[str, tuple[float, str]] = {}


def provider_api_key(provider_id: str, max_age: float = 30.0) -> str:
    """Read the current API key for a provider from CC Switch (short cache)."""
    if not provider_id:
        return ""
    now = time.time()
    cached = _KEY_CACHE.get(provider_id)
    if cached and now - cached[0] < max_age:
        return cached[1]
    key = ""
    path = Path(
        settings.BRIDGE.get("CC_SWITCH_DB_PATH")
        or Path.home() / ".cc-switch/cc-switch.db"
    )
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT settings_config FROM providers WHERE id=?", (provider_id,)
            ).fetchone()
        finally:
            con.close()
        if row:
            auth = (json.loads(row[0]).get("auth") or {})
            for name in (
                "OPENAI_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "api_key",
            ):
                if auth.get(name):
                    key = str(auth[name])
                    break
    except Exception:  # noqa: BLE001 - best effort
        return cached[1] if cached else ""
    _KEY_CACHE[provider_id] = (now, key)
    return key


def _app_type(account: Account) -> str:
    if account.protocol in (Account.PROTOCOL_CODEX, Account.PROTOCOL_OPENAI):
        return "codex"
    return "claude"


def mirror_request(
    *,
    account: Account,
    request_id: str,
    path: str,
    model: str,
    status_code: int | None,
    error_message: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd=None,
    duration_ms: int | None = None,
    ttft_ms: int | None = None,
    is_streaming: bool = False,
) -> bool:
    """Best-effort insert; never let CC Switch DB problems break proxying."""
    if not settings.BRIDGE.get("CC_SWITCH_MIRROR"):
        return False
    if status_code is None or status_code >= 400:
        # Failed bridge attempts are not client-visible results; keep them out
        # of CC Switch usage statistics.
        return False
    provider_id = (account.cc_provider_id or "").strip()
    if not provider_id:
        return False

    db_path = Path(
        settings.BRIDGE.get("CC_SWITCH_DB_PATH")
        or (Path.home() / ".cc-switch" / "cc-switch.db")
    ).expanduser()
    if not db_path.exists():
        return False

    created_at = int(time.time())
    total_cost = str(cost_usd) if cost_usd is not None else "0"
    values = (
        f"bridge:{request_id}",
        provider_id,
        _app_type(account),
        model or "",
        model or "",
        "",
        int(input_tokens or 0),
        int(output_tokens or 0),
        int(cache_read_tokens or 0),
        int(cache_write_tokens or 0),
        0,
        total_cost,
        "0",
        "0",
        "0",
        total_cost,
        int(duration_ms or 0),
        int(ttft_ms) if ttft_ms is not None else None,
        int(duration_ms or 0),
        status_code,
        error_message or "",
        request_id,
        "",
        1 if is_streaming else 0,
        "1.0",
        created_at,
        "proxy",
    )
    try:
        con = sqlite3.connect(db_path, timeout=2)
        con.execute("PRAGMA busy_timeout=2000")
        con.execute(
            """INSERT OR REPLACE INTO proxy_request_logs
               (request_id, provider_id, app_type, model, request_model, pricing_model,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                input_token_semantics, input_cost_usd, output_cost_usd,
                cache_read_cost_usd, cache_creation_cost_usd, total_cost_usd,
                latency_ms, first_token_ms, duration_ms, status_code, error_message,
                session_id, provider_type, is_streaming, cost_multiplier, created_at,
                data_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        con.commit()
        con.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to mirror log to CC Switch: %s", exc)
        return False


def reattribute_request(
    *,
    account: Account,
    model: str,
    input_tokens: int,
    output_tokens: int,
    status_code: int | None,
    created_at: int,
    request_id: str,
    path: str,
    cost_usd=None,
    duration_ms: int | None = None,
    ttft_ms: int | None = None,
    error_message: str = "",
) -> bool:
    """Rewrite CC Switch's native bridge row to the real vendor provider id."""
    if not settings.BRIDGE.get("CC_SWITCH_MIRROR"):
        return False
    vendor_id = (account.cc_provider_id or "").strip()
    bridge_id = (settings.BRIDGE.get("CC_SWITCH_BRIDGE_PROVIDER_ID") or "").strip()
    if not vendor_id or not bridge_id:
        return False

    db_path = Path(
        settings.BRIDGE.get("CC_SWITCH_DB_PATH")
        or (Path.home() / ".cc-switch" / "cc-switch.db")
    ).expanduser()
    if not db_path.exists():
        return False

    for _ in range(8):
        try:
            con = sqlite3.connect(db_path, timeout=2)
            con.execute("PRAGMA busy_timeout=2000")
            row = con.execute(
                """SELECT request_id FROM proxy_request_logs
                   WHERE provider_id=? AND data_source='proxy' AND model=?
                     AND status_code IS ?
                     AND ABS(created_at - ?) <= 12
                     AND input_tokens=? AND output_tokens=?
                   ORDER BY ABS(created_at - ?) LIMIT 1""",
                (
                    bridge_id,
                    model or "",
                    status_code,
                    created_at,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    created_at,
                ),
            ).fetchone()
            if row:
                con.execute(
                    "UPDATE proxy_request_logs SET provider_id=? WHERE request_id=?",
                    (vendor_id, row[0]),
                )
                con.commit()
                con.close()
                return True
            con.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("reattribute failed: %s", exc)
        time.sleep(0.5)

    return mirror_request(
        account=account,
        request_id=request_id,
        path=path,
        model=model,
        status_code=status_code,
        error_message=error_message,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        ttft_ms=ttft_ms,
    )

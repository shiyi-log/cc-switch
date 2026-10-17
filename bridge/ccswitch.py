"""Mirror bridge request logs into CC Switch's own request log table."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from django.conf import settings

from .models import Account

logger = logging.getLogger("bridge")


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

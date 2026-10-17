import sqlite3

import pytest
from django.test import override_settings

from bridge.ccswitch import mirror_request
from bridge.models import Account

SCHEMA = """
CREATE TABLE proxy_request_logs (
    request_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, app_type TEXT NOT NULL,
    model TEXT, request_model TEXT, pricing_model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER, input_token_semantics INTEGER,
    input_cost_usd TEXT, output_cost_usd TEXT, cache_read_cost_usd TEXT,
    cache_creation_cost_usd TEXT, total_cost_usd TEXT,
    latency_ms INTEGER, first_token_ms INTEGER, duration_ms INTEGER,
    status_code INTEGER, error_message TEXT, session_id TEXT, provider_type TEXT,
    is_streaming INTEGER, cost_multiplier TEXT, created_at INTEGER, data_source TEXT
);
"""


@pytest.mark.django_db
def test_mirror_writes_vendor_row(tmp_path, settings):
    db = tmp_path / "cc-switch.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.close()

    settings.BRIDGE = {
        **settings.BRIDGE,
        "CC_SWITCH_MIRROR": True,
        "CC_SWITCH_DB_PATH": str(db),
    }
    account = Account.objects.create(
        name="wdlink pro",
        tier=Account.TIER_THIRD_PARTY,
        protocol=Account.PROTOCOL_OPENAI,
        quota_mode=Account.QUOTA_NONE,
        cc_provider_id="provider-123",
    )

    assert mirror_request(
        account=account,
        request_id="req-1",
        path="/v1/responses",
        model="gpt-6.1-sol",
        status_code=200,
        input_tokens=100,
        output_tokens=20,
        duration_ms=1500,
        ttft_ms=400,
    )

    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT request_id, provider_id, app_type, status_code, input_tokens, output_tokens "
        "FROM proxy_request_logs"
    ).fetchone()
    con.close()
    assert row == ("bridge:req-1", "provider-123", "codex", 200, 100, 20)

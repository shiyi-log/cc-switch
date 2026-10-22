#!/usr/bin/env python3
"""Keep the official SSE gate alive and enforce the 20% quota reserve."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

HOME = Path.home()
ROOT = Path(__file__).resolve().parent.parent
LIVE = HOME / ".cc-switch" / "live-state.json"
SETTINGS = HOME / ".cc-switch" / "settings.json"
AUTH = HOME / ".codex" / "auth.json"
LOG = ROOT / ".run" / "route-watchdog.log"
ALERT = ROOT / ".run" / "quota-alert.json"
OFFICIAL_PROVIDER_ID = "7126a089-f42e-4292-a0f9-86e8f73bd08f"
FALLBACK_ID = "721db9d2-2c10-40cc-bf58-535dc0a7fcc8"  # pp 特惠
GATE_PORT = 9998
QUOTA_GUARD_PERCENT = 80.0  # remaining <= 20%
QUOTA_CHECK_INTERVAL = 60.0


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def route() -> str:
    try:
        data = json.loads(LIVE.read_text())
        return (data.get("apps", {}).get("codex") or {}).get("proxy_route") or ""
    except Exception:
        return ""


def set_route(provider_id: str) -> None:
    data = json.loads(LIVE.read_text())
    data.setdefault("apps", {}).setdefault("codex", {})["proxy_route"] = provider_id
    data["apps"]["codex"]["mode"] = "proxy"
    data["apps"]["codex"]["attached"] = True
    tmp = LIVE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, LIVE)
    try:
        settings = json.loads(SETTINGS.read_text())
        settings["currentProviderCodex"] = provider_id
        SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
    except Exception:
        pass
    log(f"route -> {provider_id}")


def official_used_percent() -> float | None:
    try:
        tokens = json.loads(AUTH.read_text())["tokens"]
        response = httpx.get(
            "https://chatgpt.com/backend-api/wham/usage",
            headers={
                "authorization": f"Bearer {tokens['access_token']}",
                "chatgpt-account-id": tokens.get("account_id", ""),
            },
            timeout=15,
        )
        if response.status_code != 200:
            return None
        window = (response.json().get("rate_limit") or {}).get("primary_window") or {}
        return float(window.get("used_percent"))
    except Exception:
        return None


def remove_from_queue(provider_id: str) -> None:
    db = HOME / ".cc-switch" / "cc-switch.db"
    try:
        con = sqlite3.connect(db, timeout=10)
        con.execute(
            "UPDATE providers SET in_failover_queue=0 WHERE id=?", (provider_id,)
        )
        con.commit()
        con.close()
        log(f"removed {provider_id} from failover queue")
    except Exception as exc:  # noqa: BLE001
        log(f"failed to update queue: {exc}")


def gate_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", GATE_PORT), timeout=2):
            return True
    except OSError:
        return False


def start_gate() -> None:
    subprocess.Popen(
        [
            str(ROOT / ".venv/bin/uvicorn"),
            "scripts.official_gate:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(GATE_PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        stdout=(ROOT / ".run/official-gate.out").open("a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log("official gate restarted")


def main() -> None:
    log("watchdog started (gate + quota guard)")
    last_quota_check = 0.0
    while True:
        try:
            if not gate_alive():
                start_gate()
                time.sleep(2)
            now = time.time()
            if now - last_quota_check >= QUOTA_CHECK_INTERVAL:
                last_quota_check = now
                used = official_used_percent()
                if used is not None and used >= QUOTA_GUARD_PERCENT:
                    remove_from_queue(OFFICIAL_PROVIDER_ID)
                    if route() == OFFICIAL_PROVIDER_ID:
                        set_route(FALLBACK_ID)
                    ALERT.parent.mkdir(parents=True, exist_ok=True)
                    ALERT.write_text(
                        json.dumps(
                            {
                                "used_percent": used,
                                "remaining_percent": 100.0 - used,
                                "action": "removed local official provider; route -> pp 特惠",
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    log(f"QUOTA GUARD: used={used:.2f}% remaining={100-used:.2f}%")
                    sys.exit(0)
        except Exception as exc:  # noqa: BLE001
            log(f"watchdog error: {exc}")
        time.sleep(5)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Watch Codex desktop logs and hot-switch the CC Switch route on capacity errors."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

HOME = Path.home()
LIVE = HOME / ".cc-switch" / "live-state.json"
SETTINGS = HOME / ".cc-switch" / "settings.json"
AUTH = HOME / ".codex" / "auth.json"
LOG_DIR = HOME / "Library" / "Logs" / "com.openai.codex"
LOG = Path(__file__).resolve().parent.parent / ".run" / "route-watchdog.log"
DEFAULT_ID = "default"
FALLBACK_ID = "721db9d2-2c10-40cc-bf58-535dc0a7fcc8"  # pp 特惠
COOLDOWN = 300.0
PROBE_INTERVAL = 60.0


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


def newest_log() -> Path | None:
    try:
        files = [p for p in LOG_DIR.rglob("*.log") if p.is_file()]
    except Exception:
        return None
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def official_ok() -> bool:
    try:
        tokens = json.loads(AUTH.read_text())["tokens"]
        headers = {
            "authorization": f"Bearer {tokens['access_token']}",
            "chatgpt-account-id": tokens.get("account_id", ""),
            "content-type": "application/json",
        }
        body = {
            "model": "gpt-6.1-sol",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "max_output_tokens": 16,
            "store": False,
            "stream": True,
        }
        with httpx.stream(
            "POST",
            "https://chatgpt.com/backend-api/codex/responses",
            headers=headers,
            json=body,
            timeout=30,
        ) as response:
            if response.status_code >= 400:
                return False
            sample = b""
            for chunk in response.iter_bytes():
                sample += chunk
                if len(sample) > 8192:
                    break
        text = sample.decode("utf-8", "replace").lower()
        return "capacity" not in text and "response.created" in text
    except Exception:
        return False


def main() -> None:
    log("route watchdog started")
    current = None
    offset = 0
    switched_at = 0.0
    last_probe = 0.0
    while True:
        try:
            path = newest_log()
            if path and path != current:
                current = path
                offset = path.stat().st_size  # ignore history on first sight
            if path:
                size = path.stat().st_size
                if size < offset:
                    offset = 0
                if size > offset:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(offset)
                        for line in handle:
                            if "at capacity" in line.lower() and route() == DEFAULT_ID:
                                set_route(FALLBACK_ID)
                                switched_at = time.time()
                                last_probe = switched_at
                    offset = size

            if route() == FALLBACK_ID:
                now = time.time()
                if now - switched_at >= COOLDOWN and now - last_probe >= PROBE_INTERVAL:
                    last_probe = now
                    if official_ok():
                        set_route(DEFAULT_ID)
                        log("official recovered; route restored")
                    else:
                        log("official still capacity; staying on fallback")
        except Exception as exc:  # noqa: BLE001
            log(f"watchdog error: {exc}")
        time.sleep(5)


if __name__ == "__main__":
    main()

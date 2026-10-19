#!/usr/bin/env python3
"""Keep Codex on its official ChatGPT provider (account UI) behind the local proxy.

CC Switch rewrites ~/.codex/config.toml to a custom provider every time its proxy
attaches. This watchdog restores the official-provider form with
``openai_base_url`` pointing at the CC Switch proxy, so the ChatGPT account stays
visible in the Codex UI.
"""

from __future__ import annotations

import glob
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
CONFIG = HOME / ".codex" / "config.toml"
AUTH = HOME / ".codex" / "auth.json"
LOG = Path(__file__).resolve().parent.parent / ".run" / "codex-config-watchdog.log"
BASE_URL = "http://127.0.0.1:15721/v1"


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def restore_config() -> None:
    if not CONFIG.exists():
        return
    original = CONFIG.read_text(encoding="utf-8")
    text = original
    text = re.sub(r"^model_provider\s*=.*\n", "", text, flags=re.M)
    text = re.sub(r"\n\[model_providers\.custom\].*?(?=\n\[|\Z)", "", text, flags=re.S)
    if re.search(r"^openai_base_url\s*=", text, flags=re.M):
        text = re.sub(
            r"^openai_base_url\s*=.*$",
            f'openai_base_url = "{BASE_URL}"',
            text,
            count=1,
            flags=re.M,
        )
    else:
        first = re.search(r"^\[", text, re.M)
        at = first.start() if first else len(text)
        text = text[:at] + f'openai_base_url = "{BASE_URL}"\n' + text[at:]
    if text != original:
        CONFIG.write_text(text, encoding="utf-8")
        log("config restored to official provider + proxy base_url")

    if not AUTH.exists():
        backups = sorted(glob.glob(str(HOME / ".codex" / "auth.json.bak-*")))
        if backups:
            shutil.copy2(backups[-1], AUTH)
            log("auth.json restored from latest backup")


def main() -> None:
    log("watchdog started")
    while True:
        try:
            restore_config()
        except Exception as exc:  # noqa: BLE001
            log(f"error: {exc}")
        time.sleep(10)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Lightweight health monitor for the CC Switch bridge chain."""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / ".run" / "monitor.log"
BRIDGE = "http://127.0.0.1:9999"
PROXY = ("127.0.0.1", 15721)
ROUTE_ID = "af1c5777-d9ec-4f29-8604-151824c2d396"


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def bridge_status() -> dict | None:
    token = ""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("BRIDGE_TOKEN="):
                token = line.split("=", 1)[1].strip()
    try:
        request = urllib.request.Request(
            BRIDGE + "/api/status", headers={"x-bridge-token": token}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except Exception:
        return None


def start_bridge() -> None:
    subprocess.Popen(
        [
            str(ROOT / ".venv/bin/uvicorn"),
            "cc_switch.asgi:application",
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--workers",
            "1",
        ],
        cwd=ROOT,
        stdout=(ROOT / ".run/bridge.log").open("a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log("ALERT bridge down -> restarted")


def recent_counts(minutes: int = 10) -> tuple[int, int, int]:
    """Count client-visible responses recorded by CC Switch, not bridge attempts."""
    db = Path.home() / ".cc-switch/cc-switch.db"
    if not db.exists():
        return (0, 0, 0)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        since = int(time.time()) - minutes * 60
        rows = con.execute(
            "SELECT status_code, COUNT(*) FROM proxy_request_logs "
            "WHERE created_at >= ? AND model IS NOT NULL AND model <> '' "
            "AND request_id NOT LIKE 'bridge:%' GROUP BY status_code",
            (since,),
        ).fetchall()
    finally:
        con.close()
    total = sum(n for _, n in rows)
    ok = sum(n for status, n in rows if status == 200)
    bad = total - ok
    return total, ok, bad


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=120)
    args = parser.parse_args()
    deadline = time.time() + args.minutes * 60
    log(f"monitor started for {args.minutes} minutes")
    last_heartbeat = 0.0
    soft_alerted = False
    while time.time() < deadline:
        try:
            if not http_ok(BRIDGE + "/api/health"):
                start_bridge()
                time.sleep(5)
            if not port_open(*PROXY):
                log("ALERT CC Switch proxy 15721 is not listening")
            live = ROOT.parent / ".cc-switch/live-state.json"
            try:
                state = json.loads((Path.home() / ".cc-switch/live-state.json").read_text())
                route = (state.get("apps", {}).get("codex") or {}).get("proxy_route")
                if route != ROUTE_ID:
                    log(f"ALERT route changed: {route}")
            except Exception:
                pass
            status = bridge_status()
            if status:
                official = next(
                    (a for a in status.get("accounts", []) if a.get("tier") == "official"),
                    None,
                )
                if official:
                    if official.get("soft_reserve") and not soft_alerted:
                        log(
                            "ALERT official reached soft reserve "
                            f"util={official.get('utilization')}"
                        )
                        soft_alerted = True
                    if float(official.get("utilization") or 0) >= float(official.get("limit") or 1):
                        log("ALERT official reached hard reserve")
            total, ok, bad = recent_counts(10)
            if total >= 20 and bad / total > 0.5:
                log(f"ALERT high error attempts: total={total} ok={ok} bad={bad}")
            if time.time() - last_heartbeat > 600:
                log(f"heartbeat total10m={total} ok={ok} bad={bad}")
                last_heartbeat = time.time()
        except Exception as exc:  # noqa: BLE001
            log(f"monitor error: {exc}")
        time.sleep(30)
    total, ok, bad = recent_counts(10)
    log(f"monitor finished total10m={total} ok={ok} bad={bad}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Supervise a Codex task session.

- exit code 0 (or a completion marker)      -> task finished, do not restart
- non-zero exit / signal / failure marker   -> abnormal stop, resume and retry
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def log(path: Path, event: str, **fields) -> None:
    line = json.dumps({"ts": utcnow(), "event": event, **fields}, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def build_command(args, resume: bool, session_id: str | None):
    if args.command:
        return args.command
    if resume:
        command = [CODEX, "exec", "resume", "--skip-git-repo-check"]
        command.append(session_id or "--last")
        command.append(args.resume_prompt)
        return command
    return [CODEX, "exec", "--skip-git-repo-check", args.prompt]


def run_once(args, command, state, log_path):
    """Run one attempt; return (exit_code, completed, session_id, output)."""
    log(log_path, "start", attempt=state["attempts"], command=command if isinstance(command, str) else " ".join(command))
    process = subprocess.Popen(
        command,
        cwd=args.cwd,
        shell=isinstance(command, str),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    chunks: list[str] = []
    marker_seen = False
    session_id = args.session_id
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        chunks.append(line)
        match = SESSION_RE.search(line)
        if match:
            session_id = match.group(1)
        if args.complete_marker and args.complete_marker in line:
            marker_seen = True
    code = process.wait()
    output = "".join(chunks)

    failure_marker = any(marker in output for marker in args.failure_marker)
    completed = marker_seen or (code == 0 and not failure_marker)
    return code, completed, session_id, output


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex session watchdog")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt")
    source.add_argument("--prompt-file")
    source.add_argument("--command", help="shell command override (testing)")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--session-id")
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--restart-delay", type=float, default=5.0)
    parser.add_argument("--complete-marker", default="TASK_COMPLETE")
    parser.add_argument("--failure-marker", action="append", default=["exceeded retry limit"])
    parser.add_argument("--resume-prompt", default="Continue the task from where it stopped.")
    parser.add_argument("--log", default=".run/watchdog.log")
    parser.add_argument("--state", default=".run/watchdog-state.json")
    args = parser.parse_args()

    if args.prompt_file:
        args.prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not args.command and not args.prompt:
        parser.error("--prompt or --prompt-file is required")

    log_path = Path(args.log)
    state_path = Path(args.state)
    state = {
        "task": args.prompt,
        "command": args.command,
        "attempts": 0,
        "status": "running",
        "session_id": args.session_id,
        "started_at": utcnow(),
        "updated_at": utcnow(),
    }
    write_state(state_path, state)
    log(log_path, "watchdog_start", task=args.prompt)

    resume = False
    while True:
        state["attempts"] += 1
        state["updated_at"] = utcnow()
        write_state(state_path, state)
        command = build_command(args, resume, state.get("session_id"))
        code, completed, session_id, _ = run_once(args, command, state, log_path)
        state["session_id"] = session_id or state.get("session_id")

        if completed:
            state["status"] = "completed"
            state["exit_code"] = code
            state["updated_at"] = utcnow()
            write_state(state_path, state)
            log(log_path, "completed", exit_code=code, attempts=state["attempts"])
            return 0

        reason = "signal" if code < 0 else "exit_code"
        state["status"] = "restarting"
        state["last_exit_code"] = code
        state["reason"] = reason
        state["updated_at"] = utcnow()
        write_state(state_path, state)
        log(log_path, "abnormal_stop", exit_code=code, reason=reason, attempts=state["attempts"])

        if state["attempts"] > args.max_restarts:
            state["status"] = "failed"
            state["updated_at"] = utcnow()
            write_state(state_path, state)
            log(log_path, "gave_up", attempts=state["attempts"])
            return 2

        delay = min(60.0, args.restart_delay * (2 ** (state["attempts"] - 1)))
        log(log_path, "restart_scheduled", delay=delay)
        time.sleep(delay)
        resume = True


if __name__ == "__main__":
    raise SystemExit(main())

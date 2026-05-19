#!/usr/bin/env python3
"""Deterministic Work Ledger watchdog runner.

Clean checks stay outside the LLM. The main session is woken only when the
ledger CLI reports recovery, orphan reconciliation, referenced terminal task,
or runner error signals.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path("/Users/moon/.openclaw/workspace")
LEDGER = Path("/Users/moon/.openclaw/bin/openclaw-ledger")
OPENCLAW = Path("/opt/homebrew/bin/openclaw")
STATE_PATH = WORKSPACE / "state" / "work-ledger" / "watchdog-runner-state.json"
PROMPT_PATH = WORKSPACE / "crons" / "work-ledger-watchdog.md"
SESSION_KEY = "agent:main:telegram:direct:343580315"
WAKE_SUPPRESSION_SECONDS = 30 * 60


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(WORKSPACE), capture_output=True, text=True, timeout=timeout, check=False)


def stable_signature(result: dict[str, Any]) -> str:
    payload = {
        "status": result.get("status"),
        "wake_reason": result.get("wake_reason"),
        "recoveries": [
            item.get("recovery_fingerprint") or item.get("work_id")
            for item in result.get("recoveries") or []
            if isinstance(item, dict)
        ],
        "terminal_refs": [
            {
                "work_id": item.get("work_id"),
                "ref": item.get("ref"),
                "task_status": item.get("task_status"),
            }
            for item in ((result.get("terminal_refs") or {}).get("terminal_refs") or [])
            if isinstance(item, dict)
        ],
        "orphans": [
            item.get("orphan_fingerprint") or item.get("taskId") or item.get("runId")
            for item in ((result.get("orphans") or {}).get("orphans") or [])
            if isinstance(item, dict)
        ],
        "errors": result.get("errors") or ((result.get("orphans") or {}).get("errors") if isinstance(result.get("orphans"), dict) else None),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def wake_prompt(result: dict[str, Any]) -> str:
    base = PROMPT_PATH.read_text(encoding="utf-8")
    return (
        base
        + "\n\n---\n\n"
        + "Non-LLM runner already executed watchdog-check. Use this result as the initial triage evidence. "
        + "You may rerun checks if needed, but do not repeat risky side effects.\n\n"
        + "watchdog-check result JSON:\n"
        + json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def main() -> int:
    os.environ["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    state = load_json(STATE_PATH, {})
    started_at = now_iso()
    try:
        proc = run([str(LEDGER), "watchdog-check", "--include-cron"], timeout=45)
    except Exception as exc:
        result: dict[str, Any] = {
            "ok": False,
            "status": "error",
            "needs_wake": True,
            "wake_reason": "runner_error",
            "errors": [str(exc)],
            "policy": "runner exception; LLM should inspect before user-visible output",
        }
    else:
        if proc.returncode == 0:
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                result = {
                    "ok": False,
                    "status": "error",
                    "needs_wake": True,
                    "wake_reason": "runner_error",
                    "errors": [f"invalid watchdog-check JSON: {exc}", proc.stdout[-1000:]],
                }
        else:
            result = {
                "ok": False,
                "status": "error",
                "needs_wake": True,
                "wake_reason": "runner_error",
                "errors": [(proc.stderr or proc.stdout or f"watchdog-check exited {proc.returncode}").strip()],
            }

    state.update({
        "last_run_at": started_at,
        "last_status": result.get("status"),
        "last_wake_reason": result.get("wake_reason"),
    })

    if not result.get("needs_wake"):
        state["last_clean_at"] = now_iso()
        save_json(STATE_PATH, state)
        return 0

    signature = stable_signature(result)
    now_ts = time.time()
    last_wake_ts = float(state.get("last_wake_ts") or 0)
    last_wake_succeeded = state.get("last_wake_returncode") == 0
    if last_wake_succeeded and state.get("last_wake_signature") == signature and now_ts - last_wake_ts < WAKE_SUPPRESSION_SECONDS:
        state["last_suppressed_at"] = now_iso()
        state["last_suppressed_signature"] = signature
        save_json(STATE_PATH, state)
        return 0

    wake_cmd = [
        str(OPENCLAW),
        "system",
        "event",
        "--session-key",
        SESSION_KEY,
        "--mode",
        "now",
        "--text",
        wake_prompt(result),
        "--json",
        "--timeout",
        "30000",
    ]
    try:
        wake = run(wake_cmd, timeout=45)
    except Exception as exc:
        wake = subprocess.CompletedProcess(wake_cmd, 1, stdout="", stderr=str(exc))
    state["last_wake_attempt_at"] = now_iso()
    state["last_wake_returncode"] = wake.returncode
    state["last_wake_stdout"] = wake.stdout[-2000:]
    state["last_wake_stderr"] = wake.stderr[-2000:]
    if wake.returncode == 0:
        state["last_wake_signature"] = signature
        state["last_wake_ts"] = now_ts
    else:
        state["last_failed_wake_signature"] = signature
        state["last_failed_wake_ts"] = now_ts
    save_json(STATE_PATH, state)
    return 0 if wake.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

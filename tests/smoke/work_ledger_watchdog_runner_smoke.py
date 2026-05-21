#!/usr/bin/env python3
"""Focused smoke checks for Work Ledger watchdog runner edges."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPTS = WORKSPACE / "scripts"
LEDGER_PATH = SCRIPTS / "work_ledger.py"
if not LEDGER_PATH.exists():
    LEDGER_PATH = WORKSPACE / "src" / "work_ledger.py"
RUNNER_PATH = SCRIPTS / "work_ledger_watchdog_runner.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
src_dir = WORKSPACE / "src"
if src_dir.exists() and str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_true(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def completed(cmd: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def write_runner_config(runner, tmp: str) -> None:
    config_path = Path(tmp) / "config.json"
    runner.DEFAULT_CONFIG_PATH = config_path
    runner.PROMPT_PATH = Path(tmp) / "prompt.md"
    runner.STATE_PATH = Path(tmp) / "state.json"
    runner.PROMPT_PATH.write_text("watchdog prompt", encoding="utf-8")
    config_path.write_text(
        json.dumps(
            {
                "workspace": tmp,
                "ledger_path": str(runner.LEDGER),
                "openclaw_path": str(runner.OPENCLAW),
                "prompt_path": str(runner.PROMPT_PATH),
                "state_path": str(runner.STATE_PATH),
                "fallback_session_key": "agent:main:telegram:direct:test-user",
            }
        ),
        encoding="utf-8",
    )


def test_runner_has_no_private_defaults() -> None:
    source = RUNNER_PATH.read_text(encoding="utf-8")
    private_chat_id = "343" + "580" + "315"
    private_home = "/Users/" + "moon"
    external_prompt = "workspace/crons/" + "work-ledger-watchdog.md"
    forbidden = [private_home, private_chat_id, external_prompt]
    assert_true(not any(item in source for item in forbidden), "runner must not ship private paths, ids, or external prompt defaults")


def test_runner_help_without_config() -> None:
    runner = load_module("work_ledger_watchdog_runner_help_target", RUNNER_PATH)
    original_argv = sys.argv[:]
    try:
        sys.argv = [str(RUNNER_PATH), "--help"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = runner.main()
    finally:
        sys.argv = original_argv
    output = buf.getvalue()
    assert_true(code == 0, "runner --help should exit cleanly without config")
    assert_true("Usage:" in output and "OPENCLAW_LEDGER_CONFIG" in output, "runner --help should explain config")


def test_delivery_does_not_suppress_unresolved_wake() -> None:
    runner = load_module("work_ledger_watchdog_runner_smoke_target", RUNNER_PATH)
    non_clean = {
        "ok": True,
        "status": "needs_wake",
        "needs_wake": True,
        "wake_reason": "referenced_task_reconciliation",
        "terminal_refs": {"terminal_refs": [{"work_id": "w1", "ref": "task-1", "task_status": "failed"}]},
    }
    clean_json = json.dumps(non_clean)
    wake_returncodes = [1, 0]
    wake_calls: list[int] = []

    with tempfile.TemporaryDirectory() as tmp:
        write_runner_config(runner, tmp)

        def fake_run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
            if "watchdog-check" in cmd:
                assert_true("--root" in cmd and cmd[cmd.index("--root") + 1] == tmp, "runner should pass configured workspace as ledger root")
                assert_true(os.environ.get("OPENCLAW_BIN") == str(runner.OPENCLAW), "runner should expose configured openclaw_path to ledger child")
                return completed(cmd, 0, stdout=clean_json)
            if cmd[:3] == [str(runner.OPENCLAW), "system", "event"]:
                code = wake_returncodes.pop(0) if wake_returncodes else 0
                wake_calls.append(code)
                return completed(cmd, code, stdout="{}" if code == 0 else "", stderr="wake failed" if code else "")
            raise AssertionError(f"unexpected command: {cmd}")

        runner.run = fake_run
        assert_true(runner.main() == 1, "failed wake should return nonzero")
        assert_true(runner.main() == 0, "same signature should retry after failed wake")
        assert_true(wake_calls == [1, 0], f"expected failed wake then retry, got {wake_calls}")
        assert_true(runner.main() == 0, "same unresolved signature should wake again after delivery")
        assert_true(wake_calls == [1, 0, 0], "event delivery alone must not suppress unresolved recovery")


def test_missing_prompt_uses_fallback_wake() -> None:
    runner = load_module("work_ledger_watchdog_runner_missing_prompt_target", RUNNER_PATH)
    non_clean = {
        "ok": True,
        "status": "needs_wake",
        "needs_wake": True,
        "wake_reason": "recovery",
        "recoveries": [{"work_id": "w1", "recovery_fingerprint": "rf1"}],
    }
    wake_texts: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        write_runner_config(runner, tmp)
        runner.PROMPT_PATH.unlink()

        def fake_run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
            if "watchdog-check" in cmd:
                return completed(cmd, 0, stdout=json.dumps(non_clean))
            if cmd[:3] == [str(runner.OPENCLAW), "system", "event"]:
                wake_texts.append(cmd[cmd.index("--text") + 1])
                return completed(cmd, 0, stdout="{}")
            raise AssertionError(f"unexpected command: {cmd}")

        runner.run = fake_run
        assert_true(runner.main() == 0, "missing packaged prompt should fall back and still wake")

    assert_true(wake_texts, "fallback wake should include prompt text")
    assert_true("Work Ledger Watchdog v1 Wake Handler" in wake_texts[0], "fallback prompt should identify recovery handler")
    assert_true("runner_prompt_error" in wake_texts[0], "fallback wake should include prompt read error evidence")


def test_wake_exception_persists_failed_metadata() -> None:
    runner = load_module("work_ledger_watchdog_runner_exception_target", RUNNER_PATH)
    non_clean = {
        "ok": True,
        "status": "needs_wake",
        "needs_wake": True,
        "wake_reason": "recovery",
        "recoveries": [{"work_id": "w1", "recovery_fingerprint": "rf1"}],
    }
    wake_results = ["timeout", "success"]

    with tempfile.TemporaryDirectory() as tmp:
        write_runner_config(runner, tmp)

        def fake_run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
            if "watchdog-check" in cmd:
                assert_true("--root" in cmd and cmd[cmd.index("--root") + 1] == tmp, "runner should pass configured workspace as ledger root")
                return completed(cmd, 0, stdout=json.dumps(non_clean))
            if cmd[:3] == [str(runner.OPENCLAW), "system", "event"]:
                result = wake_results.pop(0)
                if result == "timeout":
                    raise subprocess.TimeoutExpired(cmd, timeout)
                return completed(cmd, 0, stdout="{}")
            raise AssertionError(f"unexpected command: {cmd}")

        runner.run = fake_run
        assert_true(runner.main() == 1, "wake timeout should return nonzero")
        state_after_timeout = json.loads(runner.STATE_PATH.read_text(encoding="utf-8"))
        assert_true("last_failed_wake_signature" in state_after_timeout, "wake timeout should persist failed signature")
        assert_true("last_wake_signature" not in state_after_timeout, "wake timeout must not arm successful suppression")
        assert_true(runner.main() == 0, "same signature should retry after wake timeout")


def test_referenced_terminal_task_status_vocabulary() -> None:
    ledger = load_module("work_ledger_smoke_target", LEDGER_PATH)
    refs = [
        {"work_id": "w-shutdown", "ref": "task-shutdown", "status": "running", "source": "openclaw_task_ids"},
        {"work_id": "w-notfound", "ref": "task-notfound", "status": "running", "source": "openclaw_task_ids"},
        {"work_id": "w-completed", "ref": "task-completed", "status": "running", "source": "openclaw_task_ids"},
        {"work_id": "w-alias-prefixed", "ref": "codex-thread:synthetic-alias", "raw_ref": "codex-thread:synthetic-alias", "status": "running", "source": "subagents.runId"},
        {"work_id": "w-alias-bare", "ref": "synthetic-alias", "raw_ref": "codex-thread:synthetic-alias", "status": "running", "source": "subagents.runId"},
    ]

    def fake_refs(root: Path):
        return refs

    def fake_lookup(lookup: str):
        if lookup == "task-shutdown":
            return {"status": "shutdown", "taskId": lookup}, None
        if lookup == "task-notfound":
            return {"status": "notFound", "taskId": lookup}, None
        if lookup == "task-completed":
            return {"status": {"completed": "done"}, "taskId": lookup}, None
        if lookup in {"codex-thread:synthetic-alias", "synthetic-alias"}:
            raise AssertionError("synthetic codex-thread aliases should not be task lookups")
        raise AssertionError(f"unexpected lookup: {lookup}")

    ledger.collect_active_task_ref_details = fake_refs
    ledger.load_openclaw_task_lookup = fake_lookup
    result = ledger.find_referenced_terminal_tasks(Path("/unused"))
    statuses = {item["task_status"] for item in result["terminal_refs"]}
    assert_true(result["has_terminal_refs"], "expected terminal refs")
    assert_true(statuses == {"shutdown", "notFound", "completed"}, f"unexpected statuses: {statuses}")


def main() -> None:
    test_runner_has_no_private_defaults()
    test_runner_help_without_config()
    test_delivery_does_not_suppress_unresolved_wake()
    test_missing_prompt_uses_fallback_wake()
    test_wake_exception_persists_failed_metadata()
    test_referenced_terminal_task_status_vocabulary()
    print(json.dumps({"ok": True, "checked": ["runner-public-defaults", "runner-help", "configured-root", "configured-openclaw-bin", "wake-delivery-does-not-suppress", "missing-prompt-fallback", "wake-exception-state", "referenced-terminal-statuses"]}, indent=2))


if __name__ == "__main__":
    main()

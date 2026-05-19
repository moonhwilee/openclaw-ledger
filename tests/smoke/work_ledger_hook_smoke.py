#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "src" / "work_ledger.py"


def run(root: Path, *args: str) -> dict:
    proc = subprocess.run(
        ["python3", str(LEDGER), "--root", str(root), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


def age_events(root: Path, work_id: str, seconds: int) -> None:
    path = root / "state" / "work-ledger" / "events.jsonl"
    events = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line)
            if event.get("work_id") == work_id:
                aged = datetime.now(timezone.utc) - timedelta(seconds=seconds)
                event["event_at"] = aged.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            events.append(event)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work_id = "hook-ledger-smoke"
        visible = json.dumps({"channel": "telegram", "to": "test-user"})
        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--request-summary",
            "hook ledger smoke",
            "--owner-session-key",
            "agent:main:main",
            "--checklist",
            json.dumps(["hook observed"]),
            "--success-criteria",
            json.dumps(["duplicate hook is harmless"]),
            "--visible-delivery",
            visible,
        )

        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"cmd": "python3 -m pytest tests/unit -q", "idempotency_key": "secret"},
            "tool_response": {"exit_code": 0, "blocked_idempotency_key": "secret"},
            "cwd": str(ROOT),
            "model": "gpt-5.5",
            "permission_mode": "dontAsk",
            "transcript_path": None,
        }
        observed = run(root, "hook-observe", "--work-id", work_id, "--payload", json.dumps(payload))
        duplicate = run(root, "hook-observe", "--work-id", work_id, "--payload", json.dumps(payload))
        changed_timestamp = {**payload, "timestamp": "redelivery-later"}
        duplicate_changed_timestamp = run(root, "hook-observe", "--work-id", work_id, "--payload", json.dumps(changed_timestamp))
        concurrent_payload = {**payload, "tool_use_id": "tool-concurrent"}
        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = list(executor.map(
                lambda _: run(root, "hook-observe", "--work-id", work_id, "--payload", json.dumps(concurrent_payload)),
                range(2),
            ))
        state = run(root, "state", "--work-id", work_id)["items"][0]

        if not observed["ok"] or observed["duplicate"]:
            raise AssertionError("first hook observation should append")
        state_after_observe = run(root, "state", "--work-id", work_id)["items"][0]
        if state_after_observe.get("last_progress_at") != state_after_observe.get("created_at"):
            raise AssertionError("passive hook observation must not refresh ledger progress freshness")
        candidate_payload = {**payload, "tool_use_id": "tool-candidate-action"}
        candidate = run(root, "hook-observe", "--work-id", work_id, "--payload", json.dumps(candidate_payload), "--next-recovery-action", "candidate only")
        candidate_state = run(root, "state", "--work-id", work_id)["items"][0]
        if candidate_state.get("next_recovery_action") == "candidate only":
            raise AssertionError("hook candidate next action must not become durable recovery authority")
        if not duplicate["duplicate"]:
            raise AssertionError("duplicate hook observation should be deduped")
        if not duplicate_changed_timestamp["duplicate"]:
            raise AssertionError("duplicate hook with changed timestamp should be deduped")
        concurrent_appends = sum(1 for item in concurrent_results if not item["duplicate"])
        if concurrent_appends != 1:
            raise AssertionError("concurrent duplicate hook delivery should append once")
        state = run(root, "state", "--work-id", work_id)["items"][0]
        if len(state.get("hook_fingerprints", [])) != 3:
            raise AssertionError("state should keep exactly three unique hook fingerprints")
        if "idempotency_key" in json.dumps(state):
            raise AssertionError("runtime idempotency keys must be redacted from hook observations")

        run(root, "complete", "--work-id", work_id)
        stop_payload = {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "stop_hook_active": False,
            "last_assistant_message": "Done",
            "cwd": str(ROOT),
            "model": "gpt-5.5",
            "permission_mode": "dontAsk",
            "transcript_path": None,
        }
        decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(stop_payload))["decision"]
        if decision["decision"] != "nudge":
            raise AssertionError("completed_unreported stop should nudge visible report")
        github_write_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-github-write",
            "tool_name": "mcp__codex_apps__github_merge_pull_request",
            "tool_input": {"repository_full_name": "owner/repo", "pr_number": 7},
        }
        github_write_decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(github_write_payload))["decision"]
        if github_write_decision["decision"] != "block":
            raise AssertionError("GitHub write connector tools should require approval clearance")
        git_push_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-git-push",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git push origin main"},
        }
        git_push_decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(git_push_payload))["decision"]
        if git_push_decision["decision"] != "block":
            raise AssertionError("plain git push should require approval clearance")
        git_dash_c_push_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-git-dash-c-push",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git -C /tmp/repo push origin main"},
        }
        git_dash_c_push_decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(git_dash_c_push_payload))["decision"]
        if git_dash_c_push_decision["decision"] != "block":
            raise AssertionError("git -C ... push should require approval clearance")
        git_structured_push_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-git-structured-push",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git", "args": ["-C", "/tmp/repo", "push", "origin", "main"]},
        }
        git_structured_push_decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(git_structured_push_payload))["decision"]
        if git_structured_push_decision["decision"] != "block":
            raise AssertionError("structured git push should require approval clearance")
        git_commit_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-git-commit",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "git commit -m test"},
        }
        git_commit_decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(git_commit_payload))["decision"]
        if git_commit_decision["decision"] != "allow":
            raise AssertionError("local git commit should not be blocked by the remote-write guardrail")
        readonly_search_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-readonly-search",
            "tool_name": "exec_command",
            "tool_input": {"cmd": "rg mcp__codex_apps__github_update_file plugins/goalflow/index.js"},
        }
        readonly_search_decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(readonly_search_payload))["decision"]
        if readonly_search_decision["decision"] != "allow":
            raise AssertionError("read-only searches mentioning GitHub write tool names should not be blocked")
        github_call_payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_use_id": "tool-github-call",
            "tool_name": "exec_command",
            "tool_input": {"source": "await tools.mcp__codex_apps__github_update_file({})"},
        }
        github_call_decision = run(root, "hook-guardrail", "--work-id", work_id, "--payload", json.dumps(github_call_payload))["decision"]
        if github_call_decision["decision"] != "block":
            raise AssertionError("call-like GitHub write connector text should be blocked")

        missed_work_id = "hook-missed-smoke"
        run(
            root,
            "start",
            "--work-id",
            missed_work_id,
            "--request-summary",
            "missed hook durable recovery",
            "--owner-session-key",
            "agent:main:main",
            "--checklist",
            json.dumps(["complete without hook"]),
            "--success-criteria",
            json.dumps(["scan detects completed unreported"]),
            "--visible-delivery",
            visible,
            "--artifact-paths",
            str(root / "artifact.txt"),
            "--expected-outputs",
            str(root / "artifact.txt"),
        )
        run(root, "complete", "--work-id", missed_work_id)
        scan = run(root, "scan", "--cooldown-seconds", "0")
        missed_recovery = [item for item in scan["recoveries"] if item["work_id"] == missed_work_id]
        if not missed_recovery or missed_recovery[0]["reason"] != "completed_unreported":
            raise AssertionError("missed hook path should still recover from durable ledger state")

        stale_hook_work_id = "hook-active-stale-cooldown"
        run(
            root,
            "start",
            "--work-id",
            stale_hook_work_id,
            "--request-summary",
            "active stale hook cooldown smoke",
            "--owner-session-key",
            "agent:main:main",
            "--checklist",
            json.dumps(["stale", "wake", "hook"]),
            "--success-criteria",
            json.dumps(["passive hook does not bypass wake cooldown"]),
            "--visible-delivery",
            visible,
            "--expected-outputs",
            "long-running-result",
        )
        age_events(root, stale_hook_work_id, seconds=3601)
        stale_scan = run(root, "scan", "--cooldown-seconds", "1800")
        stale_recovery = [item for item in stale_scan["recoveries"] if item["work_id"] == stale_hook_work_id]
        if not stale_recovery:
            raise AssertionError("active stale work should produce a recovery before wake delivery")
        run(
            root,
            "wake-delivered",
            "--work-id",
            stale_hook_work_id,
            "--recovery-fingerprint",
            stale_recovery[0]["recovery_fingerprint"],
        )
        run(root, "hook-observe", "--work-id", stale_hook_work_id, "--payload", json.dumps({**payload, "tool_use_id": "tool-after-wake"}))
        stale_suppressed = run(root, "scan", "--cooldown-seconds", "1800")
        stale_recovered_again = [item for item in stale_suppressed["recoveries"] if item["work_id"] == stale_hook_work_id]
        if stale_recovered_again:
            raise AssertionError("passive hook observation must not bypass active stale recovery cooldown")

        print(json.dumps({
            "ok": True,
            "smokes": {
                "hook_observation_appended": observed["event"]["event_type"],
                "hook_does_not_refresh_progress": True,
                "hook_next_action_non_authoritative": candidate["ok"],
                "duplicate_deduped": duplicate["duplicate"],
                "changed_timestamp_deduped": duplicate_changed_timestamp["duplicate"],
                "concurrent_append_count": concurrent_appends,
                "state_hook_fingerprints": len(state["hook_fingerprints"]),
                "stop_guardrail": decision["reason"],
                "github_write_guardrail": github_write_decision["reason"],
                "git_push_guardrail": git_push_decision["reason"],
                "git_dash_c_push_guardrail": git_dash_c_push_decision["reason"],
                "git_structured_push_guardrail": git_structured_push_decision["reason"],
                "git_commit_local_allowed": git_commit_decision["decision"] == "allow",
                "readonly_search_allowed": readonly_search_decision["decision"] == "allow",
                "github_write_call_guardrail": github_call_decision["reason"],
                "missed_hook_recovery": missed_recovery[0]["reason"],
                "active_stale_hook_after_wake_suppressed": not stale_recovered_again,
            },
        }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

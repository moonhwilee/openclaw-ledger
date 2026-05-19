#!/usr/bin/env python3
"""Deterministic smoke tests for the workspace work ledger.

The smoke uses an isolated temporary root and never touches the real ledger.
It proves the completed-but-unreported recovery-report path:

completed_unreported work -> recovery packet -> wake-delivered record ->
report-sent record -> reported terminal state.
"""

from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[2]
LEDGER = WORKSPACE / "src" / "work_ledger.py"
sys.path.insert(0, str(WORKSPACE / "src"))

LEDGER_SPEC = importlib.util.spec_from_file_location("work_ledger_module", LEDGER)
if LEDGER_SPEC is None or LEDGER_SPEC.loader is None:
    raise RuntimeError("could not load work_ledger.py")
LEDGER_MODULE = importlib.util.module_from_spec(LEDGER_SPEC)
LEDGER_SPEC.loader.exec_module(LEDGER_MODULE)


def run(root: Path, *args: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(LEDGER), "--root", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"invalid JSON output for {' '.join(args)}: {result.stdout}") from exc


def run_expect_fail(root: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(LEDGER), "--root", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(args)}\nstdout:\n{result.stdout}")
    return result.stderr + result.stdout


def assert_true(value: Any, message: str) -> None:
    if not value:
        raise AssertionError(message)


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


def smoke_recovery_report_path() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-ledger-wake-report"
        visible_delivery = json.dumps({"channel": "telegram", "target": "telegram:test-user"})

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test unfinished work recovery reporting",
            "--expected-outputs",
            "smoke-report",
            "--next-recovery-action",
            "Verify smoke artifacts and send visible report.",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "complete", "wake", "report"]),
            "--success-criteria",
            json.dumps(["recovery packet exists", "report_sent reaches reported"]),
        )
        run(root, "complete", "--work-id", work_id, "--note", "work finished but report not sent yet")

        scan = run(root, "scan", "--cooldown-seconds", "1800")
        assert_true(scan["has_recoveries"], "completed_unreported work should produce a recovery packet")
        assert_true(len(scan["recoveries"]) == 1, "expected exactly one recovery packet")
        packet = scan["recoveries"][0]
        assert_true(packet["reason"] == "completed_unreported", "unexpected recovery reason")
        assert_true(packet["work_id"] == work_id, "packet work_id mismatch")
        assert_true(packet["visible_delivery"]["channel"] == "telegram", "visible delivery missing")

        run(
            root,
            "wake-delivered",
            "--work-id",
            work_id,
            "--recovery-fingerprint",
            packet["recovery_fingerprint"],
            "--note",
            "smoke wake delivered",
        )

        suppressed = run(root, "scan", "--cooldown-seconds", "1800")
        assert_true(not suppressed["has_recoveries"], "delivered wake should be suppressed during cooldown")
        run(root, "progress", "--work-id", work_id, "--note", "bookkeeping after wake")
        progress_suppressed = run(root, "scan", "--cooldown-seconds", "1800")
        assert_true(
            not progress_suppressed["has_recoveries"],
            "bookkeeping progress after wake should not bypass recovery cooldown",
        )
        run(
            root,
            "wait-reminder-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "smoke-wait-reminder",
        )
        reminder_suppressed = run(root, "scan", "--cooldown-seconds", "1800")
        assert_true(
            not reminder_suppressed["has_recoveries"],
            "wait reminders after wake should not bypass recovery cooldown",
        )

        after_cooldown = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(after_cooldown["has_recoveries"], "wake-delivered must not be terminal after cooldown")
        assert_true(
            after_cooldown["recoveries"][0]["recovery_fingerprint"] == packet["recovery_fingerprint"],
            "same unfinished work should recover again after cooldown",
        )

        run(
            root,
            "verify",
            "--work-id",
            work_id,
            "--note",
            "post-completion verification should not reopen terminal-unreported work",
        )
        verified_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(
            verified_state["status"] == "completed_unreported",
            "post-completion verify must not reopen completed_unreported work",
        )

        run(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "smoke-visible-report",
        )

        state = run(root, "state", "--work-id", work_id)
        item = state["items"][0]
        assert_true(item["status"] == "reported", "report_sent should mark state reported")
        assert_true(item["completion_report_sent"] is True, "report_sent should mark completion_report_sent")
        assert_true(isinstance(item.get("report_sent_at"), str), "report_sent_at should be persisted")
        assert_true(
            item["visible_delivery_proof"].get("message_id") == "smoke-visible-report",
            "visible report delivery id should be persisted",
        )
        final_scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(not final_scan["has_recoveries"], "reported work should not recover")

        return {
            "work_id": work_id,
            "recovery_reason": packet["reason"],
            "final_status": item["status"],
            "visible_report_recorded": item["visible_delivery_proof"].get("message_id") == "smoke-visible-report",
            "wake_suppressed_after_delivery": not suppressed["has_recoveries"],
            "progress_after_wake_suppressed": not progress_suppressed["has_recoveries"],
            "reminder_after_wake_suppressed": not reminder_suppressed["has_recoveries"],
            "wake_recoverable_after_cooldown": after_cooldown["has_recoveries"],
            "post_completion_verify_kept_terminal": verified_state["status"] == "completed_unreported",
            "reported_scan_clean": not final_scan["has_recoveries"],
        }


def smoke_report_sent_requires_delivery() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-report-delivery-required"
        visible_delivery = json.dumps({"channel": "telegram", "target": "telegram:test-user"})

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test report-sent delivery proof",
            "--expected-outputs",
            "smoke-report",
            "--next-recovery-action",
            "Send visible completion report.",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "complete", "report"]),
            "--success-criteria",
            json.dumps(["report_sent requires delivery proof"]),
        )
        run(root, "complete", "--work-id", work_id, "--note", "finished but not visibly reported")
        missing_delivery = run_expect_fail(root, "report-sent", "--work-id", work_id)
        missing_message_id = run_expect_fail(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
        )
        wrong_route = run_expect_fail(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "other-user"}),
            "--delivery-message-id",
            "wrong-route-report",
        )
        extra_route_key = run_expect_fail(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user", "session_key": "session:other"}),
            "--delivery-message-id",
            "extra-route-report",
        )
        state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(state["status"] == "completed_unreported", "failed report-sent must not close the work")
        assert_true(
            "--visible-delivery is required" in missing_delivery,
            "missing visible_delivery should be rejected explicitly",
        )
        assert_true(
            "--delivery-message-id is required" in missing_message_id,
            "missing delivery_message_id should be rejected explicitly",
        )
        assert_true(
            "route mismatch" in wrong_route,
            "report-sent should reject a visible delivery route that differs from the original target",
        )
        assert_true(
            "route mismatch" in extra_route_key,
            "report-sent should reject extra delivery route keys",
        )
        run(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            json.dumps({"channel": "telegram", "to": "test-user"}),
            "--delivery-message-id",
            "alias-route-report",
        )
        reported_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(reported_state["status"] == "reported", "target/to aliases and Telegram prefixes should be accepted for the same route")
        return {
            "work_id": work_id,
            "status_after_failed_reports": state["status"],
            "missing_visible_delivery_rejected": "--visible-delivery is required" in missing_delivery,
            "missing_delivery_message_id_rejected": "--delivery-message-id is required" in missing_message_id,
            "wrong_route_rejected": "route mismatch" in wrong_route,
            "extra_route_key_rejected": "route mismatch" in extra_route_key,
            "target_to_alias_accepted": reported_state["status"] == "reported",
        }


def smoke_visible_update_route_does_not_contaminate_report_route() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-visible-update-route-isolated"
        completion_route = json.dumps({"channel": "telegram", "target": "test-user"})
        update_route = json.dumps({"session_key": "session:progress-only"})

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            completion_route,
            "--request-summary",
            "Smoke test progress route isolation",
            "--expected-outputs",
            "visible report",
            "--checklist",
            json.dumps(["start", "update", "complete", "report"]),
            "--success-criteria",
            json.dumps(["progress route does not change report route"]),
        )
        run(
            root,
            "visible-update",
            "--work-id",
            work_id,
            "--visible-delivery",
            update_route,
            "--delivery-message-id",
            "progress-message",
        )
        run(root, "complete", "--work-id", work_id, "--note", "finished after progress update")
        scan = run(root, "scan", "--cooldown-seconds", "0")
        packet = scan["recoveries"][0]
        assert_true(packet["visible_delivery"].get("target") == "test-user", "recovery packet should keep completion route")
        assert_true("session_key" not in packet["visible_delivery"], "recovery packet route should not include progress route")
        assert_true(
            packet.get("visible_delivery_proof", {}).get("last_update_message_id") == "progress-message",
            "progress proof should be separate from route",
        )
        run(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            completion_route,
            "--delivery-message-id",
            "completion-message",
        )
        state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(state["status"] == "reported", "original completion route should still close work")
        assert_true("session_key" not in state.get("visible_delivery", {}), "progress session route must not contaminate completion route")
        assert_true(
            state.get("completion_visible_delivery", {}).get("target") == "test-user",
            "completion route should be preserved separately",
        )
        return {
            "work_id": work_id,
            "final_status": state["status"],
            "completion_route_target": state["completion_visible_delivery"]["target"],
            "progress_route_isolated": "session_key" not in state.get("visible_delivery", {}),
            "proof_route_separate": packet.get("visible_delivery_proof", {}).get("last_update_message_id") == "progress-message",
        }


def smoke_orphan_uses_idle_activity_for_freshness() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    task = {
        "taskId": "task-active",
        "createdAt": now_ms - 60 * 60 * 1000,
        "startedAt": now_ms - 55 * 60 * 1000,
        "lastEventAt": now_ms - 60 * 1000,
    }
    age = LEDGER_MODULE.task_age_seconds(task, now_ms)
    idle = LEDGER_MODULE.task_idle_seconds(task, now_ms)
    assert_true(age >= 55 * 60, "age should preserve total runtime")
    assert_true(idle <= 60, "idle should prefer recent lastEventAt")
    return {"age_seconds": age, "idle_seconds": idle}


def smoke_report_sent_rejects_active_work() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-report-sent-rejects-active"
        visible_delivery = json.dumps({"channel": "telegram", "target": "test-user"})

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test report-sent cannot close active work",
            "--expected-outputs",
            "smoke-report",
            "--next-recovery-action",
            "Finish work before reporting.",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "reject premature report"]),
            "--success-criteria",
            json.dumps(["active report-sent is rejected"]),
        )
        premature = run_expect_fail(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "premature-report",
        )
        state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(state["status"] == "running", "premature report-sent must not close running work")
        assert_true(
            "allowed only after complete or fail" in premature,
            "premature report-sent should explain the state requirement",
        )
        run(root, "complete", "--work-id", work_id, "--note", "finished now")
        run(
            root,
            "report-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "final-report",
        )
        final_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(final_state["status"] == "reported", "report-sent after complete should still close work")
        run(root, "verify", "--work-id", work_id, "--note", "late verification after visible report")
        run(root, "wait", "--work-id", work_id, "--status", "waiting_subagent", "--note", "late wait after visible report")
        run(root, "fail", "--work-id", work_id, "--failure-reason", "late failure after visible report")
        absorbed_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(absorbed_state["status"] == "reported", "reported work should absorb later lifecycle events")
        return {
            "work_id": work_id,
            "premature_rejected": "allowed only after complete or fail" in premature,
            "status_after_premature": state["status"],
            "final_status": final_state["status"],
            "reported_absorbed_late_lifecycle_events": absorbed_state["status"] == "reported",
        }


def smoke_abandoned_absorbs_late_lifecycle_events() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-abandoned-absorbs-late-events"
        visible_delivery = json.dumps({"channel": "telegram", "target": "test-user"})

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test abandoned terminal absorption",
            "--expected-outputs",
            "smoke-report",
            "--next-recovery-action",
            "No recovery after abandon.",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "abandon", "ignore late lifecycle"]),
            "--success-criteria",
            json.dumps(["abandoned remains terminal"]),
        )
        run(root, "abandon", "--work-id", work_id, "--note", "user no longer needs this")
        run(root, "verify", "--work-id", work_id, "--note", "late verification after abandon")
        run(root, "wait", "--work-id", work_id, "--status", "waiting_subagent", "--note", "late wait after abandon")
        run(root, "complete", "--work-id", work_id, "--note", "late complete after abandon")
        state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(state["status"] == "abandoned", "abandoned work should absorb later lifecycle events")
        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(not any(item["work_id"] == work_id for item in scan["recoveries"]), "abandoned work should not recover")
        return {
            "work_id": work_id,
            "final_status": state["status"],
            "abandoned_scan_clean": not any(item["work_id"] == work_id for item in scan["recoveries"]),
        }


def smoke_orphans_ignore_fresh_tasks() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        now_ms = int(time.time() * 1000)
        fresh_task = {
            "taskId": "fresh-task",
            "runId": "fresh-run",
            "runtime": "subagent",
            "status": "running",
            "label": "fresh subagent",
            "createdAt": now_ms - 5 * 60 * 1000,
            "startedAt": now_ms - 5 * 60 * 1000,
            "lastEventAt": now_ms - 60 * 1000,
        }
        old_task = {
            "taskId": "old-task",
            "runId": "old-run",
            "runtime": "subagent",
            "status": "running",
            "label": "old subagent",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        handled_task = {
            "taskId": "handled-task",
            "runId": "handled-run",
            "runtime": "subagent",
            "status": "running",
            "label": "handled stale subagent",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        handled_task_id_only_task = {
            "taskId": "handled-task",
            "runtime": "subagent",
            "status": "running",
            "label": "handled taskId only row",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        legacy_warning_task = {
            "taskId": "legacy-warning-task",
            "runId": "legacy-warning-run",
            "runtime": "subagent",
            "status": "running",
            "label": "legacy warning proof task",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        corrupt_warning_task = {
            "taskId": "corrupt-warning-task",
            "runId": "corrupt-warning-run",
            "runtime": "subagent",
            "status": "running",
            "label": "corrupt warning proof task",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        invalid_handled_task = {
            "taskId": "invalid-handled-task",
            "runId": "invalid-handled-run",
            "runtime": "subagent",
            "status": "running",
            "label": "invalid handled task",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        duplicate_cli_task = {
            "taskId": "duplicate-cli-task",
            "runId": "duplicate-run",
            "runtime": "cli",
            "status": "running",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        duplicate_subagent_task = {
            "taskId": "duplicate-subagent-task",
            "runId": "duplicate-run",
            "runtime": "subagent",
            "status": "running",
            "label": "duplicate subagent representative",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        drift_cli_task = {
            "taskId": "drift-cli-task",
            "runId": "drift-run",
            "runtime": "cli",
            "status": "running",
            "label": "representation drift cli row",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        drift_subagent_task = {
            "taskId": "drift-subagent-task",
            "runId": "drift-run",
            "runtime": "subagent",
            "status": "running",
            "label": "representation drift subagent row",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        legacy_task_id_warning_task = {
            "taskId": "legacy-task-id-warning-task",
            "runId": "legacy-task-id-warning-run",
            "runtime": "subagent",
            "status": "running",
            "label": "legacy taskId warning task",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        drift_task_id_only_task = {
            "taskId": "drift-cli-task",
            "runtime": "cli",
            "status": "running",
            "label": "representation drift taskId only row",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        referenced_duplicate_cli_task = {
            "taskId": "referenced-duplicate-cli-task",
            "runId": "referenced-duplicate-run",
            "runtime": "cli",
            "status": "running",
            "label": "referenced duplicate cli row",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        referenced_duplicate_subagent_task = {
            "taskId": "referenced-duplicate-subagent-task",
            "runId": "referenced-duplicate-run",
            "runtime": "subagent",
            "status": "running",
            "label": "referenced duplicate subagent row",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        referenced_child_task = {
            "taskId": "referenced-task",
            "runId": "referenced-run",
            "childSessionKey": "child-session-1",
            "runtime": "subagent",
            "status": "running",
            "label": "referenced child subagent",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        referenced_codex_task = {
            "taskId": "referenced-codex-task",
            "runId": "codex-thread:019e3c3a-0000-7000-b000-000000000001",
            "runtime": "subagent",
            "status": "running",
            "label": "referenced Codex subagent",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        anonymous_task = {
            "runtime": "subagent",
            "status": "running",
            "label": "anonymous stale task",
            "createdAt": now_ms - 45 * 60 * 1000,
            "startedAt": now_ms - 45 * 60 * 1000,
            "lastEventAt": now_ms - 45 * 60 * 1000,
        }
        run(
            root,
            "start",
            "--work-id",
            "smoke-orphan-child-session-ref",
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user"}),
            "--request-summary",
            "Smoke test childSessionKey subagent orphan matching",
            "--expected-outputs",
            "subagent result",
            "--next-recovery-action",
            "Wait for referenced child task.",
            "--side-effect-class",
            "local_files",
            "--subagents",
            json.dumps([
                {"childSessionKey": "child-session-1"},
                {"agent_id": "019e3c3a-0000-7000-b000-000000000001"},
                {"taskId": "referenced-duplicate-cli-task"},
            ]),
            "--checklist",
            json.dumps(["start", "match child session"]),
            "--success-criteria",
            json.dumps(["referenced child task is not orphaned"]),
        )
        original = LEDGER_MODULE.load_openclaw_tasks
        try:
            LEDGER_MODULE.load_openclaw_tasks = lambda status: ([
                fresh_task,
                old_task,
                handled_task,
                legacy_warning_task,
                corrupt_warning_task,
                invalid_handled_task,
                duplicate_cli_task,
                duplicate_subagent_task,
                drift_cli_task,
                referenced_duplicate_cli_task,
                referenced_duplicate_subagent_task,
                anonymous_task,
                referenced_child_task,
                referenced_codex_task,
            ] if status == "running" else [], None)
            result = LEDGER_MODULE.find_orphan_active_work(root, min_age_seconds=30 * 60)
            old_fingerprint = next(item["orphan_fingerprint"] for item in result["orphans"] if item.get("taskId") == "old-task")
            handled_fingerprint = next(item["orphan_fingerprint"] for item in result["orphans"] if item.get("taskId") == "handled-task")
            legacy_fingerprint = next(item["orphan_fingerprint"] for item in result["orphans"] if item.get("taskId") == "legacy-warning-task")
            corrupt_fingerprint = next(item["orphan_fingerprint"] for item in result["orphans"] if item.get("taskId") == "corrupt-warning-task")
            invalid_handled_fingerprint = next(item["orphan_fingerprint"] for item in result["orphans"] if item.get("taskId") == "invalid-handled-task")
            drift_item = next(item for item in result["orphans"] if item.get("taskId") == "drift-cli-task")
            drift_fingerprint = drift_item["orphan_fingerprint"]
            drift_fingerprints = drift_item["orphan_fingerprints"]
            missing_delivery_rejected = False
            missing_warning_route_rejected = False
            missing_handled_note_rejected = False
            invalid_resolution_rejected = False
            not_user_relevant_rejected = False
            try:
                LEDGER_MODULE.record_orphan_warning(root, old_fingerprint, visible_delivery={"channel": "telegram", "target": "test-user"})
            except SystemExit as exc:
                missing_delivery_rejected = "--delivery-message-id is required" in str(exc)
            try:
                LEDGER_MODULE.record_orphan_warning(root, old_fingerprint, delivery_message_id="orphan-warning-message")
            except SystemExit as exc:
                missing_warning_route_rejected = "--visible-delivery is required" in str(exc)
            try:
                LEDGER_MODULE.record_orphan_handled(root, handled_fingerprint, resolution="terminal_no_action")
            except SystemExit as exc:
                missing_handled_note_rejected = "--note is required" in str(exc)
            try:
                LEDGER_MODULE.record_orphan_handled(root, handled_fingerprint, resolution="still_running", note="invalid state should be rejected")
            except SystemExit as exc:
                invalid_resolution_rejected = "--resolution must be one of:" in str(exc)
            try:
                LEDGER_MODULE.record_orphan_handled(root, handled_fingerprint, resolution="not_user_relevant", note="active unrelated tasks must not be durably buried")
            except SystemExit as exc:
                not_user_relevant_rejected = "--resolution must be one of:" in str(exc)
            LEDGER_MODULE.record_orphan_warning(root, old_fingerprint, visible_delivery={"channel": "telegram", "target": "test-user"}, delivery_message_id="orphan-warning-message")
            LEDGER_MODULE.record_orphan_warning(
                root,
                drift_fingerprint,
                orphan_fingerprints=drift_fingerprints,
                visible_delivery={"channel": "telegram", "target": "test-user"},
                delivery_message_id="drift-warning-message",
            )
            handled_fingerprints = next(item["orphan_fingerprints"] for item in result["orphans"] if item.get("taskId") == "handled-task")
            LEDGER_MODULE.record_orphan_handled(
                root,
                handled_fingerprint,
                resolution="terminal_no_action",
                note="terminal handle needed no user-visible message",
                orphan_fingerprints=handled_fingerprints,
            )
            warnings = LEDGER_MODULE.load_orphan_warnings(root)
            legacy_task_id_fingerprint = LEDGER_MODULE.orphan_identity_fingerprints(legacy_task_id_warning_task)[-1]
            warnings[legacy_task_id_fingerprint] = {
                "warned_at": LEDGER_MODULE.now_iso(),
                "orphan_fingerprint": legacy_task_id_fingerprint,
                "visible_delivery": {"channel": "telegram", "target": "test-user"},
                "delivery_message_id": "legacy-task-id-warning-message",
            }
            warnings[legacy_fingerprint] = {
                "warned_at": LEDGER_MODULE.now_iso(),
                "orphan_fingerprint": legacy_fingerprint,
                "delivery_message_id": "legacy-message-without-route",
            }
            warnings[corrupt_fingerprint] = {
                "warned_at": LEDGER_MODULE.now_iso(),
                "orphan_fingerprint": corrupt_fingerprint,
                "visible_delivery": {},
                "delivery_message_id": "corrupt-message-with-empty-route",
            }
            warnings[invalid_handled_fingerprint] = {
                "handled_at": LEDGER_MODULE.now_iso(),
                "orphan_fingerprint": invalid_handled_fingerprint,
                "resolution": "not_user_relevant",
                "note": "legacy invalid resolution should not suppress",
            }
            LEDGER_MODULE.atomic_write_json(LEDGER_MODULE.orphan_warnings_path(root), warnings)
            old_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            old_task["status"] = "queued"
            old_task["label"] = "same task with updated label"
            old_task["runtime"] = "task"
            handled_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            handled_task_id_only_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            legacy_warning_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            duplicate_cli_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            duplicate_subagent_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            drift_cli_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            drift_subagent_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            drift_task_id_only_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            legacy_task_id_warning_task["lastEventAt"] = now_ms - 31 * 60 * 1000
            LEDGER_MODULE.load_openclaw_tasks = lambda status: ([
                fresh_task,
                old_task,
                handled_task_id_only_task,
                legacy_warning_task,
                corrupt_warning_task,
                invalid_handled_task,
                duplicate_cli_task,
                duplicate_subagent_task,
                drift_subagent_task,
                drift_task_id_only_task,
                legacy_task_id_warning_task,
                anonymous_task,
                referenced_child_task,
                referenced_codex_task,
            ] if status == "running" else [], None)
            suppressed = LEDGER_MODULE.find_orphan_active_work(root, min_age_seconds=30 * 60)
            original_time = LEDGER_MODULE.time.time
            try:
                LEDGER_MODULE.time.time = lambda: original_time() + LEDGER_MODULE.DEFAULT_ORPHAN_WARNING_SUPPRESSION_SECONDS + 1
                suppressed_after_ttl = LEDGER_MODULE.find_orphan_active_work(root, min_age_seconds=30 * 60)
            finally:
                LEDGER_MODULE.time.time = original_time
        finally:
            LEDGER_MODULE.load_openclaw_tasks = original
        orphan_ids = {item.get("taskId") for item in result["orphans"]}
        ignored_ids = {item.get("taskId") for item in result["ignored"]}
        suppressed_orphan_ids = {item.get("taskId") for item in suppressed["orphans"]}
        suppressed_ignored = {item.get("taskId"): item.get("reason") for item in suppressed["ignored"]}
        suppressed_after_ttl_orphan_ids = {item.get("taskId") for item in suppressed_after_ttl["orphans"]}
        suppressed_after_ttl_ignored = {item.get("taskId"): item.get("reason") for item in suppressed_after_ttl["ignored"]}
        anonymous_orphans = [item for item in result["orphans"] if item.get("label") == "anonymous stale task"]
        assert_true("fresh-task" not in orphan_ids, "fresh task should not be reported as an orphan")
        assert_true("fresh-task" in ignored_ids, "fresh task should be listed as ignored")
        assert_true("old-task" in orphan_ids, "old unreferenced task should remain an orphan warning")
        assert_true("duplicate-subagent-task" in orphan_ids, "same-runId duplicate should keep the more specific subagent row")
        assert_true("duplicate-cli-task" not in orphan_ids, "same-runId duplicate should not report both rows")
        assert_true("referenced-duplicate-subagent-task" not in orphan_ids, "same-runId duplicate should be skipped when any duplicate row is ledger-referenced")
        assert_true("referenced-duplicate-cli-task" not in orphan_ids, "ledger-referenced duplicate row should not be an orphan")
        assert_true(missing_delivery_rejected, "orphan warnings should require delivery proof")
        assert_true(missing_warning_route_rejected, "orphan warnings should require visible delivery route")
        assert_true(missing_handled_note_rejected, "handled orphan records should require a note")
        assert_true(invalid_resolution_rejected, "orphan handled records should reject unsafe/ambiguous resolutions")
        assert_true(not_user_relevant_rejected, "not_user_relevant should not be a durable orphan-handled resolution")
        assert_true(
            LEDGER_MODULE.orphan_identity_fingerprint({"taskId": "cli-task", "runId": "same-run"})
            == LEDGER_MODULE.orphan_identity_fingerprint({"taskId": "subagent-task", "runId": "same-run"}),
            "runId should be the canonical orphan fingerprint identity when present",
        )
        assert_true("old-task" not in suppressed_orphan_ids, "already warned orphan should be suppressed")
        assert_true(suppressed_ignored.get("old-task") == "warned", "suppressed orphan should be listed as already warned")
        assert_true("legacy-warning-task" in suppressed_orphan_ids, "legacy warning without visible route proof should not suppress")
        assert_true("corrupt-warning-task" in suppressed_orphan_ids, "corrupt warning with empty visible route should not suppress")
        assert_true("invalid-handled-task" in suppressed_orphan_ids, "invalid handled resolution should not suppress on read")
        assert_true("drift-subagent-task" not in suppressed_orphan_ids, "same-runId representation drift should preserve warning suppression")
        assert_true("drift-cli-task" not in suppressed_orphan_ids, "taskId-only representation drift should preserve alias warning suppression")
        assert_true("legacy-task-id-warning-task" not in suppressed_orphan_ids, "legacy taskId warning records should still suppress by alias")
        assert_true("handled-task" not in suppressed_orphan_ids, "handled orphan should be suppressed")
        assert_true(suppressed_ignored.get("handled-task") == "handled", "handled orphan should be listed as handled")
        assert_true("handled-task" not in suppressed_after_ttl_orphan_ids, "handled orphan should remain suppressed after warning TTL")
        assert_true(suppressed_after_ttl_ignored.get("handled-task") == "handled", "handled orphan suppression should not expire like warnings")
        assert_true(anonymous_orphans and anonymous_orphans[0].get("suppression_supported") is False, "anonymous orphan should be reported as unsuppressable")
        assert_true("referenced-task" not in orphan_ids, "childSessionKey referenced task should not be an orphan")
        assert_true("referenced-codex-task" not in orphan_ids, "codex-thread runId referenced by agent_id should not be an orphan")
        return {
            "fresh_ignored": "fresh-task" in ignored_ids,
            "old_reported": "old-task" in orphan_ids,
            "old_suppressed_after_warning": "old-task" not in suppressed_orphan_ids,
            "missing_orphan_warning_delivery_rejected": missing_delivery_rejected,
            "missing_orphan_warning_route_rejected": missing_warning_route_rejected,
            "missing_orphan_handled_note_rejected": missing_handled_note_rejected,
            "invalid_orphan_handled_resolution_rejected": invalid_resolution_rejected,
            "not_user_relevant_handled_rejected": not_user_relevant_rejected,
            "legacy_warning_without_route_not_suppressed": "legacy-warning-task" in suppressed_orphan_ids,
            "corrupt_warning_without_route_not_suppressed": "corrupt-warning-task" in suppressed_orphan_ids,
            "invalid_handled_record_not_suppressed": "invalid-handled-task" in suppressed_orphan_ids,
            "same_run_id_duplicate_deduped": "duplicate-subagent-task" in orphan_ids and "duplicate-cli-task" not in orphan_ids,
            "referenced_same_run_id_duplicate_not_orphaned": "referenced-duplicate-subagent-task" not in orphan_ids and "referenced-duplicate-cli-task" not in orphan_ids,
            "same_run_id_representation_drift_suppressed": "drift-subagent-task" not in suppressed_orphan_ids,
            "task_id_only_representation_drift_suppressed": "drift-cli-task" not in suppressed_orphan_ids,
            "legacy_task_id_warning_alias_suppressed": "legacy-task-id-warning-task" not in suppressed_orphan_ids,
            "handled_suppressed_without_delivery": "handled-task" not in suppressed_orphan_ids,
            "task_id_only_handled_drift_suppressed": "handled-task" not in suppressed_orphan_ids,
            "handled_suppression_does_not_expire_with_warning_ttl": "handled-task" not in suppressed_after_ttl_orphan_ids,
            "anonymous_orphan_unsuppressable": bool(anonymous_orphans and anonymous_orphans[0].get("suppression_supported") is False),
            "child_session_reference_matched": "referenced-task" not in orphan_ids,
            "codex_thread_reference_matched": "referenced-codex-task" not in orphan_ids,
            "min_age_seconds": result["min_age_seconds"],
        }


def smoke_insufficient_recovery_context() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-insufficient-context"

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user"}),
            "--request-summary",
            "Smoke test missing recovery context",
            "--expected-outputs",
            "smoke-analysis-result",
            "--next-recovery-action",
            "Do not guess; reconcile missing context.",
            "--side-effect-class",
            "local_files",
            "--checklist",
            json.dumps(["start", "detect missing context"]),
            "--success-criteria",
            json.dumps(["insufficient context is explicit"]),
        )
        run(root, "complete", "--work-id", work_id, "--note", "completed without expected outputs")

        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(scan["has_recoveries"], "missing expected outputs should still produce a packet")
        packet = scan["recoveries"][0]
        assert_true(packet["reason"] == "insufficient_recovery_context", "missing context should be explicit")
        assert_true("recovery_anchor" in packet["recovery_context_gaps"], "recovery_anchor gap should be listed")
        assert_true(
            "Do not guess" in packet["required_recovery_instruction"],
            "insufficient context packet should prevent guessing",
        )
        return {
            "work_id": work_id,
            "reason": packet["reason"],
            "gaps": packet["recovery_context_gaps"],
        }


def smoke_missing_expected_outputs_context() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-missing-expected-outputs"

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user"}),
            "--request-summary",
            "Smoke test missing expected outputs",
            "--next-recovery-action",
            "Do not guess; ask for or reconstruct expected outputs.",
            "--side-effect-class",
            "local_files",
            "--checklist",
            json.dumps(["start", "detect missing expected outputs"]),
            "--success-criteria",
            json.dumps(["missing expected outputs are explicit"]),
        )
        run(root, "complete", "--work-id", work_id, "--note", "completed without expected outputs")

        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(scan["has_recoveries"], "missing expected outputs should produce a packet")
        packet = scan["recoveries"][0]
        assert_true(packet["reason"] == "insufficient_recovery_context", "missing expected outputs should be explicit")
        assert_true("expected_outputs" in packet["recovery_context_gaps"], "expected_outputs gap should be listed")
        return {
            "work_id": work_id,
            "reason": packet["reason"],
            "gaps": packet["recovery_context_gaps"],
        }


def smoke_per_entry_stale_after() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-stale-after"

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user"}),
            "--request-summary",
            "Smoke test per-entry stale threshold",
            "--expected-outputs",
            "smoke-stale-output",
            "--next-recovery-action",
            "Inspect smoke stale state.",
            "--side-effect-class",
            "local_files",
            "--stale-after-seconds",
            "300",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "wait until stale"]),
            "--success-criteria",
            json.dumps(["entry goes stale using per-entry threshold"]),
        )

        fresh = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(not fresh["has_recoveries"], "entry should not be stale immediately")
        age_events(root, work_id, seconds=301)
        stale = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(stale["has_recoveries"], "entry should become stale after its per-entry threshold")
        packet = stale["recoveries"][0]
        assert_true(packet["stale_after_seconds"] == 300, "packet should expose per-entry stale threshold")
        assert_true(packet["reason"].startswith("running_stale_"), "reason should reflect running stale state")
        run(
            root,
            "visible-update",
            "--work-id",
            work_id,
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user"}),
            "--delivery-message-id",
            "active-visible-update",
        )
        run(
            root,
            "wake-delivered",
            "--work-id",
            work_id,
            "--recovery-fingerprint",
            packet["recovery_fingerprint"],
            "--note",
            "active stale wake delivered after visible update",
        )
        suppressed = run(root, "scan", "--cooldown-seconds", "1800")
        assert_true(not suppressed["has_recoveries"], "active visible-update bookkeeping should not bypass wake cooldown")

        return {
            "work_id": work_id,
            "stale_after_seconds": packet["stale_after_seconds"],
            "reason": packet["reason"],
            "visible_update_after_packet_suppressed": not suppressed["has_recoveries"],
        }


def smoke_waiting_user_minimum_stale_after() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-waiting-user-minimum"

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user"}),
            "--request-summary",
            "Smoke test waiting_user stale minimum",
            "--expected-outputs",
            "smoke-waiting-output",
            "--next-recovery-action",
            "Wait for user response.",
            "--side-effect-class",
            "local_files",
            "--stale-after-seconds",
            "300",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "wait for user"]),
            "--success-criteria",
            json.dumps(["waiting_user minimum prevents early wake"]),
        )
        run(root, "wait", "--work-id", work_id, "--status", "waiting_user", "--note", "waiting for user")

        age_events(root, work_id, seconds=301)
        fresh = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(not fresh["has_recoveries"], "waiting_user should not use a 300s running threshold")

        age_events(root, work_id, seconds=3601)
        stale = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(stale["has_recoveries"], "waiting_user should become stale after its minimum")
        packet = stale["recoveries"][0]
        assert_true(packet["reason"].startswith("waiting_user_stale_"), "reason should reflect waiting_user stale state")
        return {
            "work_id": work_id,
            "early_wake_suppressed": not fresh["has_recoveries"],
            "reason": packet["reason"],
        }


def smoke_gateway_side_effect_idempotency_policy() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-gateway-side-effect-idempotency"
        visible_delivery = json.dumps({"channel": "telegram", "target": "test-user"})

        missing_idempotency = run_expect_fail(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test Gateway side-effect repeat safety",
            "--expected-outputs",
            "gateway restart result",
            "--next-recovery-action",
            "Reconcile Gateway state; do not repeat restart without owner approval.",
            "--side-effect-class",
            "gateway_runtime",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "detect stale gateway action"]),
            "--success-criteria",
            json.dumps(["Gateway side effects carry idempotency and never-repeat policy"]),
        )
        assert_true(
            "--idempotency-key is required" in missing_idempotency,
            "Gateway side effects must require an idempotency key",
        )

        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test Gateway side-effect repeat safety",
            "--expected-outputs",
            "gateway restart result",
            "--next-recovery-action",
            "Reconcile Gateway state; do not repeat restart without owner approval.",
            "--side-effect-class",
            "gateway_runtime",
            "--idempotency-key",
            "gateway-restart-smoke-001",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "detect stale gateway action"]),
            "--success-criteria",
            json.dumps(["Gateway side effects carry idempotency and never-repeat policy"]),
        )
        state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(state["repeat_policy"] == "never_repeat_without_user_approval", "Gateway side effects should default to never-repeat")
        assert_true(state["idempotency_key"] == "gateway-restart-smoke-001", "idempotency_key should persist")

        age_events(root, work_id, seconds=3601)
        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(scan["has_recoveries"], "stale Gateway side-effect work should produce a recovery packet")
        packet = scan["recoveries"][0]
        assert_true(packet["repeat_policy"] == "never_repeat_without_user_approval", "packet should expose never-repeat policy")
        assert_true(packet["idempotency_key"] == "gateway-restart-smoke-001", "packet should expose idempotency key")
        assert_true(packet["safe_to_repeat"] is False, "Gateway side-effect recovery must not be marked safe_to_repeat")
        assert_true(
            "do not repeat external/destructive side effects without user approval"
            in packet["required_recovery_instruction"],
            "packet should instruct the recovered session not to repeat risky side effects",
        )
        return {
            "work_id": work_id,
            "missing_idempotency_rejected": "--idempotency-key is required" in missing_idempotency,
            "repeat_policy": packet["repeat_policy"],
            "idempotency_key": packet["idempotency_key"],
            "safe_to_repeat": packet["safe_to_repeat"],
        }


def smoke_resume_start_does_not_hide_unreported_completion() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-resume-start-keeps-unreported"
        visible_delivery = json.dumps({"channel": "telegram", "target": "test-user"})
        run(
            root,
            "start",
            "--work-id",
            work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test resume-start does not reopen unreported completion",
            "--expected-outputs",
            "visible report",
            "--artifact-paths",
            "artifact.txt",
            "--checklist",
            json.dumps(["complete", "resume-start"]),
            "--success-criteria",
            json.dumps(["completed_unreported remains recoverable"]),
        )
        run(root, "complete", "--work-id", work_id, "--note", "done but not reported")
        before = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(before["status"] == "completed_unreported", "complete should leave work unreported")
        resumed = run_expect_fail(
            root,
            "start",
            "--work-id",
            work_id,
            "--resume-start",
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "late resume should not hide report obligation",
            "--expected-outputs",
            "visible report",
            "--artifact-paths",
            "artifact.txt",
            "--checklist",
            json.dumps(["resume"]),
            "--success-criteria",
            json.dumps(["still unreported"]),
        )
        assert_true(
            "resume-start is not allowed after complete/fail leaves work unreported" in resumed,
            "resume-start should be rejected after unreported terminal state",
        )
        after = run(root, "state", "--work-id", work_id)["items"][0]
        scan = run(root, "scan", "--cooldown-seconds", "0")
        recovered = [item for item in scan["recoveries"] if item["work_id"] == work_id]
        assert_true(after["status"] == "completed_unreported", "resume-start must not reopen unreported completion")
        assert_true(recovered and recovered[0]["reason"] == "completed_unreported", "unreported completion should remain recoverable")
        return {
            "work_id": work_id,
            "resume_start_rejected": "resume-start is not allowed" in resumed,
            "status_after_resume_start": after["status"],
            "recovery_reason": recovered[0]["reason"],
        }


def main() -> int:
    result = {
        "ok": True,
        "smokes": {
            "recovery_report_path": smoke_recovery_report_path(),
            "report_sent_requires_delivery": smoke_report_sent_requires_delivery(),
            "visible_update_route_does_not_contaminate_report_route": smoke_visible_update_route_does_not_contaminate_report_route(),
            "report_sent_rejects_active_work": smoke_report_sent_rejects_active_work(),
            "abandoned_absorbs_late_lifecycle_events": smoke_abandoned_absorbs_late_lifecycle_events(),
            "orphans_ignore_fresh_tasks": smoke_orphans_ignore_fresh_tasks(),
            "orphan_uses_idle_activity_for_freshness": smoke_orphan_uses_idle_activity_for_freshness(),
            "insufficient_recovery_context": smoke_insufficient_recovery_context(),
            "missing_expected_outputs_context": smoke_missing_expected_outputs_context(),
            "per_entry_stale_after": smoke_per_entry_stale_after(),
            "waiting_user_minimum_stale_after": smoke_waiting_user_minimum_stale_after(),
            "gateway_side_effect_idempotency_policy": smoke_gateway_side_effect_idempotency_policy(),
            "resume_start_does_not_hide_unreported_completion": smoke_resume_start_does_not_hide_unreported_completion(),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import os
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


def smoke_json_flag_compatibility() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        scan_after_subcommand = run(root, "scan", "--json", "--cooldown-seconds", "0")
        state_after_subcommand = run(root, "state", "--json")
        scan_before_subcommand = run(root, "--json", "scan", "--cooldown-seconds", "0")
        watchdog_after_subcommand = run(root, "watchdog-check", "--json", "--cooldown-seconds", "0", "--min-age-seconds", "0")
        orphans_after_subcommand = run(root, "orphans", "--json", "--min-age-seconds", "0")
        prune_after_subcommand = run(root, "prune-terminal", "--json", "--days", "1")
        matrix_work_id = "smoke-json-compat-matrix"
        visible_delivery = json.dumps({"channel": "telegram", "target": "test-user"})
        start_after_subcommand = run(
            root,
            "start",
            "--json",
            "--work-id",
            matrix_work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test json flag matrix",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "complete-reported"]),
            "--success-criteria",
            json.dumps(["json flag accepted after lifecycle subcommands"]),
        )
        complete_reported_after_subcommand = run(
            root,
            "complete-reported",
            "--json",
            "--work-id",
            matrix_work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "json-compat-message",
        )
        hook_work_id = "smoke-json-compat-hook"
        run(
            root,
            "--json",
            "start",
            "--work-id",
            hook_work_id,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test hook json flag",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["hook-observe"]),
            "--success-criteria",
            json.dumps(["hook-observe accepts json after subcommand"]),
        )
        hook_observe_after_subcommand = run(
            root,
            "hook-observe",
            "--json",
            "--work-id",
            hook_work_id,
            "--payload",
            json.dumps({"hook_event_name": "PostToolUse", "tool_name": "test", "tool_use_id": "tool-json"}),
        )
        assert_true(scan_after_subcommand["ok"] is True, "scan --json should be accepted")
        assert_true(state_after_subcommand["ok"] is True, "state --json should be accepted")
        assert_true(scan_before_subcommand["ok"] is True, "--json scan should be accepted")
        assert_true("status" in watchdog_after_subcommand, "watchdog-check --json should be accepted")
        assert_true("has_orphans" in orphans_after_subcommand, "orphans --json should be accepted")
        assert_true(prune_after_subcommand["ok"] is True, "prune-terminal --json should be accepted")
        assert_true(start_after_subcommand["ok"] is True, "start --json should be accepted")
        assert_true(complete_reported_after_subcommand["report_event"]["event_type"] == "report_sent", "complete-reported --json should be accepted")
        assert_true(hook_observe_after_subcommand["ok"] is True, "hook-observe --json should be accepted")
        return {
            "scan_after_subcommand": scan_after_subcommand["ok"],
            "state_after_subcommand": state_after_subcommand["ok"],
            "scan_before_subcommand": scan_before_subcommand["ok"],
            "watchdog_after_subcommand": "status" in watchdog_after_subcommand,
            "orphans_after_subcommand": "has_orphans" in orphans_after_subcommand,
            "prune_after_subcommand": prune_after_subcommand["ok"],
            "start_after_subcommand": start_after_subcommand["ok"],
            "complete_reported_after_subcommand": complete_reported_after_subcommand["report_event"]["event_type"] == "report_sent",
            "hook_observe_after_subcommand": hook_observe_after_subcommand["ok"],
        }


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


def age_events_by_type(root: Path, work_id: str, event_types: set[str], seconds: int) -> None:
    path = root / "state" / "work-ledger" / "events.jsonl"
    events = []
    aged = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line)
            if event.get("work_id") == work_id and event.get("event_type") in event_types:
                event["event_at"] = aged.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            events.append(event)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def age_pending_completion_report_send(root: Path, work_id: str, seconds: int) -> None:
    path = root / "state" / "work-ledger" / "events.jsonl"
    events = []
    aged = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line)
            if event.get("work_id") == work_id and event.get("pending_completion_report_send"):
                event["event_at"] = aged.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            events.append(event)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def append_raw_event_line(root: Path, line: str) -> None:
    path = root / "state" / "work-ledger" / "events.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        if not line.endswith("\n"):
            fh.write("\n")


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
        session_only_start = run_expect_fail(
            root,
            "start",
            "--work-id",
            "smoke-session-only-completion-route",
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            json.dumps({"session_key": "agent:main:telegram:direct:test-user"}),
            "--request-summary",
            "Session-only completion route should be rejected",
            "--checklist",
            json.dumps(["start"]),
            "--success-criteria",
            json.dumps(["completion route requires channel target"]),
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
        progress_missing_delivery = run_expect_fail(root, "visible-update", "--work-id", work_id)
        progress_missing_message_id = run_expect_fail(
            root,
            "visible-update",
            "--work-id",
            work_id,
            "--visible-delivery",
            json.dumps({"session_key": "agent:main:telegram:direct:test-user"}),
        )
        reminder_missing_delivery = run_expect_fail(root, "wait-reminder-sent", "--work-id", work_id)
        reminder_missing_message_id = run_expect_fail(
            root,
            "wait-reminder-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "test-user"}),
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
            "channel plus target/to" in session_only_start,
            "completion report routes should reject session-only visible_delivery",
        )
        assert_true(
            "route mismatch" in wrong_route,
            "report-sent should reject a visible delivery route that differs from the original target",
        )
        assert_true(
            "route mismatch" in extra_route_key,
            "report-sent should reject extra delivery route keys",
        )
        assert_true(
            "--visible-delivery is required" in progress_missing_delivery,
            "visible-update should require visible_delivery proof",
        )
        assert_true(
            "--delivery-message-id is required" in progress_missing_message_id,
            "visible-update should require delivery_message_id proof",
        )
        assert_true(
            "--visible-delivery is required" in reminder_missing_delivery,
            "wait-reminder-sent should require visible_delivery proof",
        )
        assert_true(
            "--delivery-message-id is required" in reminder_missing_message_id,
            "wait-reminder-sent should require delivery_message_id proof",
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
            "session_only_completion_route_rejected": "channel plus target/to" in session_only_start,
            "wrong_route_rejected": "route mismatch" in wrong_route,
            "extra_route_key_rejected": "route mismatch" in extra_route_key,
            "progress_delivery_proof_required": "--visible-delivery is required" in progress_missing_delivery,
            "reminder_delivery_proof_required": "--visible-delivery is required" in reminder_missing_delivery,
            "target_to_alias_accepted": reported_state["status"] == "reported",
        }


def smoke_message_sent_hook_records_report_proof() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-message-sent-report-proof"
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
            "Smoke test message sent proof closes completion report",
            "--side-effect-class",
            "local_files",
            "--checklist",
            json.dumps(["complete", "message sent proof"]),
            "--success-criteria",
            json.dumps(["matching message:sent telemetry records report_sent"]),
        )
        run(root, "complete", "--work-id", work_id, "--note", "finished before proof telemetry")
        unbound_same_route = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "messageId": "same-route-without-pending",
            }),
        )
        unbound_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(unbound_state["status"] == "completed_unreported", "same-route message:sent without a pending completion send must not close work")
        assert_true(
            unbound_same_route.get("recorded_report_sent") is not True,
            "unbound same-route message:sent should stay observational",
        )
        wrong_route = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "other-user",
                "messageId": "wrong-route-message",
            }),
        )
        wrong_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(wrong_state["status"] == "completed_unreported", "wrong-route message:sent must not close work")
        assert_true(
            wrong_route.get("recorded_report_sent") is not True,
            "wrong-route message:sent should stay observational",
        )

        send_attempt = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "hook_event_name": "PreToolUse",
                "session_id": "agent:main:telegram:direct:test-user",
                "tool_use_id": "tool-visible-report",
                "tool_name": "message",
                "tool_input": {
                    "action": "send",
                    "channel": "telegram",
                    "target": "test-user",
                    "message": "Status: 완료",
                },
            }),
        )
        pending_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(send_attempt.get("recorded_completion_report_send") is True, "matching completion report send should be recorded as pending")
        assert_true(pending_state.get("pending_completion_report_send"), "pending completion report send should be durable")
        mismatch = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "tool_use_id": "different-tool",
                "messageId": "wrong-tool-message",
            }),
        )
        mismatch_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(mismatch.get("recorded_report_sent") is not True, "wrong tool_use_id must not close report proof")
        assert_true(mismatch_state["status"] == "completed_unreported", "wrong tool_use_id should leave work unreported")
        no_session = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "messageId": "no-session-message",
            }),
        )
        no_session_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(no_session.get("recorded_report_sent") is not True, "message:sent without owner session must not close report proof")
        assert_true(no_session_state["status"] == "completed_unreported", "missing session should leave work unreported")
        missing_tool_id = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "messageId": "missing-tool-id-message",
            }),
        )
        missing_tool_id_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(missing_tool_id.get("recorded_report_sent") is not True, "message:sent without the pending tool_use_id must not close report proof")
        assert_true(missing_tool_id_state["status"] == "completed_unreported", "missing tool_use_id should leave work unreported")

        proof = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "tool_use_id": "tool-visible-report",
                "messageId": "visible-report-message",
            }),
        )
        duplicate = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "tool_use_id": "tool-visible-report",
                "messageId": "visible-report-message",
            }),
        )
        reported_state = run(root, "state", "--work-id", work_id)["items"][0]
        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(proof.get("recorded_report_sent") is True, "matching message:sent should record report_sent")
        assert_true(duplicate.get("duplicate") is True, "duplicate message:sent proof should be idempotent")
        assert_true(reported_state["status"] == "reported", "message:sent proof should mark work reported")
        assert_true(
            reported_state["visible_delivery_proof"].get("message_id") == "visible-report-message",
            "message:sent delivery id should be persisted as report proof",
        )
        assert_true(not scan["has_recoveries"], "reported work should not remain recoverable")
        return {
            "work_id": work_id,
            "unbound_same_route_ignored": unbound_state["status"] == "completed_unreported",
            "wrong_route_ignored": wrong_state["status"] == "completed_unreported",
            "send_attempt_recorded": send_attempt.get("recorded_completion_report_send") is True,
            "wrong_tool_use_id_ignored": mismatch_state["status"] == "completed_unreported",
            "missing_session_ignored": no_session_state["status"] == "completed_unreported",
            "missing_tool_use_id_ignored": missing_tool_id_state["status"] == "completed_unreported",
            "message_sent_recorded_report": proof.get("recorded_report_sent") is True,
            "duplicate_message_sent_deduped": duplicate.get("duplicate") is True,
            "final_status": reported_state["status"],
            "scan_clean": not scan["has_recoveries"],
        }


def smoke_active_visible_delivery_recovery_reconciles_before_duplicate() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-active-visible-delivery-recovery"
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
            "Smoke test active visible delivery recovery packet",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--stale-after-seconds",
            "300",
            "--expected-outputs",
            "visible report",
            "--checklist",
            json.dumps(["send visible final", "record proof"]),
            "--success-criteria",
            json.dumps(["recovery reconciles observed delivery before duplicate"]),
        )
        sent = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "messageId": "active-visible-message",
            }),
        )
        age_events(root, work_id, seconds=301)
        scan = run(root, "scan", "--cooldown-seconds", "0")
        packet = next(item for item in scan["recoveries"] if item["work_id"] == work_id)
        possible_delivery = packet.get("possible_unrecorded_completion_delivery") or {}
        instruction = packet.get("required_recovery_instruction") or ""
        assert_true(sent.get("recorded_report_sent") is not True, "active visible delivery must not close proof automatically")
        assert_true(possible_delivery.get("message_id") == "active-visible-message", "recovery packet should expose the observed delivery id")
        assert_true("Do not blindly send another completion report" in instruction, "recovery should warn before duplicate report")
        return {
            "message_id": possible_delivery.get("message_id"),
            "status": packet.get("status"),
            "warns_before_duplicate": "Do not blindly send another completion report" in instruction,
        }


def smoke_terminal_visible_delivery_recovery_reconciles_before_duplicate() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-terminal-visible-delivery-recovery"
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
            "Smoke test terminal visible delivery recovery packet",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--expected-outputs",
            "visible report",
            "--checklist",
            json.dumps(["complete", "send visible final", "record proof"]),
            "--success-criteria",
            json.dumps(["terminal recovery reconciles observed delivery before duplicate"]),
        )
        run(root, "complete", "--work-id", work_id, "--note", "finished before report proof")
        sent = run(
            root,
            "hook-observe",
            "--work-id",
            work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "messageId": "terminal-visible-message",
            }),
        )
        scan = run(root, "scan", "--cooldown-seconds", "0")
        packet = next(item for item in scan["recoveries"] if item["work_id"] == work_id)
        possible_delivery = packet.get("possible_unrecorded_completion_delivery") or {}
        instruction = packet.get("required_recovery_instruction") or ""
        assert_true(sent.get("recorded_report_sent") is not True, "unbound terminal visible delivery must not close proof automatically")
        assert_true(possible_delivery.get("message_id") == "terminal-visible-message", "recovery packet should expose observed terminal delivery id")
        assert_true("Do not blindly send another completion report" in instruction, "terminal recovery should warn before duplicate report")
        return {
            "message_id": possible_delivery.get("message_id"),
            "status": packet.get("status"),
            "warns_before_duplicate": "Do not blindly send another completion report" in instruction,
        }


def smoke_message_sent_without_tool_use_id_is_time_bounded() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)

        def make_pending(work_id: str) -> None:
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
                "Smoke test no tool_use_id message sent proof window",
                "--side-effect-class",
                "local_files",
                "--checklist",
                json.dumps(["complete", "report"]),
                "--success-criteria",
                json.dumps(["no tool_use_id proof is time bounded"]),
            )
            run(root, "complete", "--work-id", work_id, "--note", "finished before proof telemetry")
            send = run(
                root,
                "hook-observe",
                "--work-id",
                work_id,
                "--payload",
                json.dumps({
                    "hook_event_name": "PreToolUse",
                    "session_id": "agent:main:telegram:direct:test-user",
                    "tool_name": "message",
                    "tool_input": {
                        "action": "send",
                        "channel": "telegram",
                        "target": "test-user",
                        "message": "Status: 완료",
                    },
                }),
            )
            assert_true(send.get("recorded_completion_report_send") is True, "native send without tool_use_id should still create pending proof")

        fresh_work_id = "smoke-message-sent-no-tool-fresh"
        make_pending(fresh_work_id)
        fresh = run(
            root,
            "hook-observe",
            "--work-id",
            fresh_work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "messageId": "fresh-no-tool-message",
            }),
        )
        fresh_state = run(root, "state", "--work-id", fresh_work_id)["items"][0]
        assert_true(fresh.get("recorded_report_sent") is True, "fresh same-session no-tool proof should close report")
        assert_true(fresh_state["status"] == "reported", "fresh no-tool proof should mark reported")

        stale_work_id = "smoke-message-sent-no-tool-stale"
        make_pending(stale_work_id)
        age_pending_completion_report_send(root, stale_work_id, seconds=301)
        stale = run(
            root,
            "hook-observe",
            "--work-id",
            stale_work_id,
            "--payload",
            json.dumps({
                "type": "message",
                "action": "sent",
                "channel": "telegram",
                "target": "test-user",
                "sessionKey": "agent:main:telegram:direct:test-user",
                "messageId": "stale-no-tool-message",
            }),
        )
        stale_state = run(root, "state", "--work-id", stale_work_id)["items"][0]
        assert_true(stale.get("recorded_report_sent") is not True, "stale no-tool proof must not close report")
        assert_true(stale_state["status"] == "completed_unreported", "stale no-tool proof should remain recoverable")
        return {
            "fresh_status": fresh_state["status"],
            "stale_status": stale_state["status"],
            "fresh_recorded": fresh.get("recorded_report_sent") is True,
            "stale_ignored": stale_state["status"] == "completed_unreported",
        }


def smoke_referenced_codex_uuid_not_terminal_task_lookup() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-referenced-codex-uuid"
        codex_id = "019e3c3a-0000-7000-b000-000000000002"

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
            "Smoke test Codex UUID subagent refs are not treated as OpenClaw task ids",
            "--expected-outputs",
            "subagent result",
            "--side-effect-class",
            "local_files",
            "--subagents",
            json.dumps([{"id": codex_id, "role": "review"}]),
            "--checklist",
            json.dumps(["wait for subagent"]),
            "--success-criteria",
            json.dumps(["bare codex uuid does not produce notFound terminal reconciliation"]),
        )
        run(
            root,
            "wait",
            "--work-id",
            work_id,
            "--status",
            "waiting_subagent",
            "--subagent-session-keys",
            codex_id,
            "--note",
            "waiting",
        )

        calls: list[str] = []
        original = LEDGER_MODULE.load_openclaw_task_lookup

        def fake_lookup(lookup: str) -> tuple[dict[str, Any] | None, str | None]:
            calls.append(lookup)
            return {"status": "notFound", "lookup": lookup}, None

        LEDGER_MODULE.load_openclaw_task_lookup = fake_lookup
        try:
            result = LEDGER_MODULE.find_referenced_terminal_tasks(root)
        finally:
            LEDGER_MODULE.load_openclaw_task_lookup = original

        assert_true(result["ok"] is True, "terminal task scan should succeed")
        assert_true(not result["has_terminal_refs"], "bare Codex UUID subagent id should not become a terminal task")
        assert_true(codex_id not in calls, "bare Codex UUID subagent id should not be looked up as an OpenClaw task")
        return {
            "work_id": work_id,
            "lookups": calls,
            "has_terminal_refs": result["has_terminal_refs"],
        }


def smoke_openclaw_bin_env_used_for_task_commands() -> dict[str, Any]:
    custom_openclaw = "/tmp/smoke-custom-openclaw"
    calls: list[list[str]] = []
    original_run = LEDGER_MODULE.subprocess.run
    original_env = os.environ.get("OPENCLAW_BIN")

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd == [custom_openclaw, "tasks", "show", "--json", "task-env"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"status": "running", "taskId": cmd[-1]}), stderr="")
        if cmd == [custom_openclaw, "tasks", "list", "--json", "--status", "running"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"tasks": []}), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    try:
        os.environ["OPENCLAW_BIN"] = custom_openclaw
        LEDGER_MODULE.subprocess.run = fake_run
        task, error = LEDGER_MODULE.load_openclaw_task_lookup("task-env")
        tasks, list_error = LEDGER_MODULE.load_openclaw_tasks("running")
    finally:
        LEDGER_MODULE.subprocess.run = original_run
        if original_env is None:
            os.environ.pop("OPENCLAW_BIN", None)
        else:
            os.environ["OPENCLAW_BIN"] = original_env

    assert_true(error is None and task and task["taskId"] == "task-env", "task lookup should parse fake task result")
    assert_true(list_error is None and tasks == [], "task list should parse fake task list result")
    assert_true(
        calls == [
            [custom_openclaw, "tasks", "show", "--json", "task-env"],
            [custom_openclaw, "tasks", "list", "--json", "--status", "running"],
        ],
        "OPENCLAW_BIN should be used with exact task command argv",
    )
    return {
        "command_count": len(calls),
        "command_bins": [call[0] for call in calls],
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
            packet.get("visible_delivery_proof", {}).get("last_update_message_id") is None,
            "non-completion-route progress proof should not be treated as visible proof",
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
            "non_completion_route_proof_ignored": packet.get("visible_delivery_proof", {}).get("last_update_message_id") is None,
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


def smoke_complete_reported_records_terminal_proof() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-complete-reported"
        mismatch_work_id = "smoke-complete-reported-mismatch"
        precompleted_work_id = "smoke-complete-reported-precompleted"
        prefailed_work_id = "smoke-complete-reported-prefailed"
        visible_delivery = json.dumps({"channel": "telegram", "target": "telegram:test-user"})
        wrong_visible_delivery = json.dumps({"channel": "telegram", "target": "other-user"})

        def start_work(item: str) -> None:
            run(
                root,
                "start",
                "--work-id",
                item,
                "--owner-session-key",
                "agent:main:telegram:direct:test-user",
                "--visible-delivery",
                visible_delivery,
                "--request-summary",
                "Smoke test atomic complete plus report proof",
                "--expected-outputs",
                "smoke-report",
                "--next-recovery-action",
                "Use complete-reported after final visible delivery.",
                "--side-effect-class",
                "local_files",
                "--no-artifact-expected",
                "--checklist",
                json.dumps(["start", "send final report", "record proof"]),
                "--success-criteria",
                json.dumps(["complete and report proof are recorded together"]),
            )

        start_work(work_id)
        result = run(
            root,
            "complete-reported",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "complete-reported-message",
            "--note",
            "verified and final report delivered",
            "--verification",
            json.dumps({"focused_check": "passed"}),
        )
        state = run(root, "state", "--work-id", work_id)["items"][0]
        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(result["completed_event"]["event_type"] == "complete", "complete-reported must append a complete event")
        assert_true(result["report_event"]["event_type"] == "report_sent", "complete-reported must append report_sent proof")
        assert_true(state["status"] == "reported", "complete-reported should leave work reported")
        assert_true(state["completion_report_sent"] is True, "complete-reported should mark completion_report_sent")
        assert_true(state["verification"].get("focused_check") == "passed", "complete verification should be preserved")
        assert_true(
            state["visible_delivery_proof"].get("message_id") == "complete-reported-message",
            "complete-reported should persist final delivery id",
        )
        assert_true(not scan["has_recoveries"], "complete-reported work should not recover")
        duplicate_error = run_expect_fail(
            root,
            "complete-reported",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "duplicate-complete-reported-message",
        )
        duplicate_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true("active or unreported completed/failed" in duplicate_error, "complete-reported should reject already reported work")
        assert_true(duplicate_state["events_count"] == state["events_count"], "duplicate complete-reported must not append events")
        assert_true(
            duplicate_state["visible_delivery_proof"].get("message_id") == "complete-reported-message",
            "duplicate complete-reported must not overwrite the original proof",
        )

        start_work(mismatch_work_id)
        mismatch_error = run_expect_fail(
            root,
            "complete-reported",
            "--work-id",
            mismatch_work_id,
            "--visible-delivery",
            wrong_visible_delivery,
            "--delivery-message-id",
            "wrong-route-message",
            "--note",
            "should not mutate",
        )
        mismatch_state = run(root, "state", "--work-id", mismatch_work_id)["items"][0]
        assert_true("route mismatch" in mismatch_error, "wrong-route complete-reported should explain mismatch")
        assert_true(mismatch_state["status"] == "running", "wrong-route complete-reported must not append complete")
        assert_true(mismatch_state["events_count"] == 1, "wrong-route complete-reported must not append any event")

        start_work(precompleted_work_id)
        run(root, "complete", "--work-id", precompleted_work_id, "--note", "already complete")
        precompleted = run(
            root,
            "complete-reported",
            "--work-id",
            precompleted_work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "precompleted-message",
        )
        precompleted_state = run(root, "state", "--work-id", precompleted_work_id)["items"][0]
        assert_true(precompleted["completed_event"] is None, "precompleted work should not get a duplicate complete")
        assert_true(precompleted_state["status"] == "reported", "precompleted work should report cleanly")
        assert_true(precompleted_state["events_count"] == 3, "precompleted work should have start, complete, report")

        start_work(prefailed_work_id)
        run(root, "fail", "--work-id", prefailed_work_id, "--failure-reason", "failed before report")
        prefailed = run(
            root,
            "complete-reported",
            "--work-id",
            prefailed_work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "prefailed-message",
            "--report-note",
            "failure report delivered",
        )
        prefailed_state = run(root, "state", "--work-id", prefailed_work_id)["items"][0]
        assert_true(prefailed["completed_event"] is None, "failed_unreported work should not get a complete event")
        assert_true(prefailed_state["status"] == "reported", "failed_unreported work should report cleanly")
        assert_true(prefailed_state.get("failure_reason") == "failed before report", "failure metadata should be preserved")
        return {
            "work_id": work_id,
            "final_status": state["status"],
            "message_id_recorded": state["visible_delivery_proof"].get("message_id"),
            "wrong_route_left_status": mismatch_state["status"],
            "precompleted_events_count": precompleted_state["events_count"],
            "prefailed_status": prefailed_state["status"],
            "prefailed_reason_preserved": prefailed_state.get("failure_reason") == "failed before report",
            "duplicate_rejected": duplicate_state["events_count"] == state["events_count"],
            "scan_clean": not scan["has_recoveries"],
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
        age_events(root, work_id, seconds=1801)

        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(scan["has_recoveries"], "missing recovery anchor should still produce a packet")
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
        assert_true(packet["reason"] == "completed_unreported", "unreported completion should not be masked by context gaps")
        assert_true("expected_outputs" in packet["recovery_context_gaps"], "expected_outputs gap should be listed")
        return {
            "work_id": work_id,
            "reason": packet["reason"],
            "gaps": packet["recovery_context_gaps"],
        }


def smoke_failed_unreported_context_gaps_not_masked() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-failed-context-gaps"

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
            "Smoke test failed_unreported with missing context",
            "--side-effect-class",
            "local_files",
            "--checklist",
            json.dumps(["start", "fail"]),
            "--success-criteria",
            json.dumps(["failed_unreported is not masked by context gaps"]),
        )
        run(root, "fail", "--work-id", work_id, "--note", "failed without expected outputs")

        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(scan["has_recoveries"], "failed_unreported should produce a packet")
        packet = scan["recoveries"][0]
        assert_true(packet["reason"] == "failed_unreported", "failed terminal state should not be masked by context gaps")
        assert_true("expected_outputs" in packet["recovery_context_gaps"], "expected_outputs gap should be listed")
        assert_true("recovery_anchor" in packet["recovery_context_gaps"], "recovery_anchor gap should be listed")
        return {
            "work_id": work_id,
            "reason": packet["reason"],
            "gaps": packet["recovery_context_gaps"],
        }


def smoke_quick_start_visible_stale_defaults() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        visible_delivery = json.dumps({"channel": "telegram", "target": "test-user"})

        coding = run(
            root,
            "quick-start",
            "--kind",
            "coding",
            "--work-id",
            "smoke-quick-coding-stale",
            "--summary",
            "visible coding work",
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--expected-outputs",
            "code diff",
            "--no-artifact-expected",
        )
        local_files = run(
            root,
            "quick-start",
            "--kind",
            "local-files",
            "--work-id",
            "smoke-quick-local-files-stale",
            "--summary",
            "visible local file work",
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--expected-outputs",
            "file diff",
            "--no-artifact-expected",
        )
        states = {item["work_id"]: item for item in run(root, "state")["items"]}
        assert_true(states[coding["work_id"]]["stale_after_seconds"] == 12 * 60, "coding quick-start should use 12 minute stale threshold")
        assert_true(states[local_files["work_id"]]["stale_after_seconds"] == 15 * 60, "local-files quick-start should use 15 minute stale threshold")
        return {
            "coding_stale_after_seconds": states[coding["work_id"]]["stale_after_seconds"],
            "local_files_stale_after_seconds": states[local_files["work_id"]]["stale_after_seconds"],
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
        state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(
            state["stale_after_seconds"] == 24 * 60 * 60,
            "waiting_user should default to one day before becoming stale",
        )

        age_events(root, work_id, seconds=301)
        fresh = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(not fresh["has_recoveries"], "waiting_user should not use a 300s running threshold")

        age_events(root, work_id, seconds=3601)
        still_fresh = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(not still_fresh["has_recoveries"], "waiting_user should not use the old one-hour minimum")

        age_events(root, work_id, seconds=(24 * 60 * 60) + 1)
        stale = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(stale["has_recoveries"], "waiting_user should become stale after one day")
        packet = stale["recoveries"][0]
        assert_true(packet["reason"].startswith("waiting_user_stale_"), "reason should reflect waiting_user stale state")
        assert_true(packet["stale_after_seconds"] == 24 * 60 * 60, "packet should expose the waiting_user threshold")
        assert_true("wait-reminder-sent" in packet["required_recovery_instruction"], "waiting_user packet should require wait-reminder-sent proof")
        assert_true("fresh wait event" not in packet["required_recovery_instruction"], "waiting_user packet should not ask for ambiguous wait event proof")
        return {
            "work_id": work_id,
            "early_wake_suppressed": not fresh["has_recoveries"],
            "one_hour_wake_suppressed": not still_fresh["has_recoveries"],
            "stale_after_seconds": packet["stale_after_seconds"],
            "reason": packet["reason"],
        }


def smoke_waiting_user_context_gaps_not_masked() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-waiting-user-context-gaps"

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
            "Smoke test waiting_user stale with missing context",
            "--side-effect-class",
            "local_files",
            "--checklist",
            json.dumps(["start", "wait"]),
            "--success-criteria",
            json.dumps(["waiting_user stale is not masked by context gaps"]),
        )
        run(root, "wait", "--work-id", work_id, "--status", "waiting_user", "--note", "waiting for user")
        age_events(root, work_id, seconds=(24 * 60 * 60) + 1)

        scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(scan["has_recoveries"], "waiting_user stale should produce a packet even with context gaps")
        packet = scan["recoveries"][0]
        assert_true(packet["reason"].startswith("waiting_user_stale_"), "waiting_user stale reason should not be masked")
        assert_true("expected_outputs" in packet["recovery_context_gaps"], "expected_outputs gap should be listed")
        assert_true("recovery_anchor" in packet["recovery_context_gaps"], "recovery_anchor gap should be listed")
        return {
            "work_id": work_id,
            "reason": packet["reason"],
            "gaps": packet["recovery_context_gaps"],
        }


def smoke_waiting_user_wait_does_not_refresh_activity() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-waiting-user-silent-wait"
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
            "Smoke test waiting_user wait is not visible proof",
            "--expected-outputs",
            "owner decision",
            "--next-recovery-action",
            "Wait for user decision.",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["start", "wait"]),
            "--success-criteria",
            json.dumps(["silent waits do not defer stale recovery"]),
        )
        run(root, "wait", "--work-id", work_id, "--status", "waiting_user", "--note", "waiting for user")
        age_events_by_type(root, work_id, {"start"}, seconds=(24 * 60 * 60) + 1)
        state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(state["last_activity_at"] == state["created_at"], "silent waiting_user wait must not refresh activity")

        stale = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(stale["has_recoveries"], "old waiting_user work should recover even after a fresh silent wait")
        packet = stale["recoveries"][0]
        assert_true(packet["reason"].startswith("waiting_user_stale_"), "reason should reflect waiting_user stale state")

        run(
            root,
            "visible-update",
            "--work-id",
            work_id,
            "--visible-delivery",
            json.dumps({"channel": "telegram", "target": "other-user"}),
            "--delivery-message-id",
            "wrong-visible-update",
        )
        wrong_update = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(
            wrong_update["last_activity_at"] == state["last_activity_at"],
            "visible update for a different route must not refresh waiting_user activity",
        )
        assert_true(
            wrong_update.get("visible_delivery_proof", {}).get("last_update_message_id") is None,
            "visible update for a different route must not record visible proof",
        )

        run(
            root,
            "progress",
            "--work-id",
            work_id,
            "--note",
            "silent bookkeeping while still waiting",
        )
        progress_state = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(
            progress_state["last_activity_at"] == state["last_activity_at"],
            "progress while waiting_user must not refresh activity",
        )

        run(
            root,
            "visible-update",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "visible-update",
        )
        update_refreshed = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(
            update_refreshed["last_visible_update_at"] == update_refreshed["last_activity_at"],
            "visible update proof should refresh waiting_user activity",
        )

        run(
            root,
            "wait-reminder-sent",
            "--work-id",
            work_id,
            "--visible-delivery",
            visible_delivery,
            "--delivery-message-id",
            "visible-reminder",
        )
        refreshed = run(root, "state", "--work-id", work_id)["items"][0]
        assert_true(
            refreshed["last_wait_reminder_at"] == refreshed["last_activity_at"],
            "visible reminder proof should refresh waiting_user activity",
        )
        return {
            "work_id": work_id,
            "silent_wait_did_not_refresh": state["last_activity_at"] == state["created_at"],
            "wrong_route_update_did_not_refresh": wrong_update["last_activity_at"] == state["last_activity_at"],
            "wrong_route_update_proof_ignored": wrong_update.get("visible_delivery_proof", {}).get("last_update_message_id") is None,
            "progress_did_not_refresh": progress_state["last_activity_at"] == state["last_activity_at"],
            "visible_update_refreshed": update_refreshed["last_visible_update_at"] == update_refreshed["last_activity_at"],
            "visible_reminder_refreshed": refreshed["last_wait_reminder_at"] == refreshed["last_activity_at"],
        }


def smoke_prune_terminal_dry_run_and_apply() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        visible_delivery = json.dumps({"channel": "telegram", "target": "test-user"})
        old_reported = "smoke-prune-old-reported"
        old_abandoned = "smoke-prune-old-abandoned"
        active_waiting = "smoke-prune-active-waiting"
        recent_reported = "smoke-prune-recent-reported"
        reused_work = "smoke-prune-reused-work-id"

        for work_id in (old_reported, old_abandoned, active_waiting, recent_reported, reused_work):
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
                f"Smoke test prune terminal work {work_id}",
                "--expected-outputs",
                "smoke-output",
                "--side-effect-class",
                "local_files",
                "--no-artifact-expected",
                "--checklist",
                json.dumps(["start"]),
                "--success-criteria",
                json.dumps(["prune respects active and recent work"]),
            )

        for work_id in (old_reported, recent_reported, reused_work):
            run(root, "complete", "--work-id", work_id, "--note", "done")
            run(
                root,
                "report-sent",
                "--work-id",
                work_id,
                "--visible-delivery",
                visible_delivery,
                "--delivery-message-id",
                f"{work_id}-report",
            )
        run(root, "abandon", "--work-id", old_abandoned, "--note", "superseded")
        run(root, "wait", "--work-id", active_waiting, "--status", "waiting_user", "--note", "still waiting")

        for work_id in (old_reported, old_abandoned, active_waiting, reused_work):
            age_events(root, work_id, seconds=31 * 24 * 60 * 60)

        dry_run = run(root, "prune-terminal", "--days", "30")
        dry_run_ids = {item["work_id"] for item in dry_run["candidates"]}
        assert_true(dry_run["apply"] is False, "prune-terminal should dry-run by default")
        assert_true(dry_run_ids == {old_reported, old_abandoned, reused_work}, "dry-run should include only old terminal work")
        assert_true(len(run(root, "state")["items"]) == 5, "dry-run must not remove state")

        run(
            root,
            "start",
            "--work-id",
            reused_work,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke test reused work id after old terminal work",
            "--expected-outputs",
            "new-smoke-output",
            "--side-effect-class",
            "local_files",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["restart"]),
            "--success-criteria",
            json.dumps(["prune apply recomputes candidates under lock"]),
        )
        corrupt_line = "{this is not valid json but should remain"
        append_raw_event_line(root, corrupt_line)

        applied = run(root, "prune-terminal", "--days", "30", "--apply")
        remaining_ids = {item["work_id"] for item in run(root, "state")["items"]}
        assert_true(old_reported not in remaining_ids, "old reported work should be pruned")
        assert_true(old_abandoned not in remaining_ids, "old abandoned work should be pruned")
        assert_true(active_waiting in remaining_ids, "active waiting work must not be pruned")
        assert_true(recent_reported in remaining_ids, "recent reported work must not be pruned")
        assert_true(reused_work in remaining_ids, "reused active work id must not be pruned from a stale dry-run candidate")

        events_text = (root / "state" / "work-ledger" / "events.jsonl").read_text(encoding="utf-8")
        assert_true(old_reported not in events_text, "pruned reported events should be removed")
        assert_true(old_abandoned not in events_text, "pruned abandoned events should be removed")
        assert_true(active_waiting in events_text, "active work events should remain")
        assert_true(reused_work in events_text, "reused active work events should remain")
        assert_true(corrupt_line in events_text, "corrupt raw event lines should be preserved")
        return {
            "dry_run_count": dry_run["prune_count"],
            "applied_count": applied["prune_count"],
            "remaining_ids": sorted(remaining_ids),
            "corrupt_line_preserved": corrupt_line in events_text,
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
        assert_true(
            "complete-reported" in packet["required_recovery_instruction"],
            "active recovery packet should instruct complete-reported after visible delivery",
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


def smoke_unreported_terminal_precedes_terminal_refs() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        visible_delivery = json.dumps({"channel": "telegram", "target": "telegram:test-user"})
        completed_work = "smoke-unreported-terminal-priority"
        terminal_ref_work = "smoke-terminal-ref-secondary"
        ref = "agent:main:subagent:terminal-secondary"

        run(
            root,
            "start",
            "--work-id",
            completed_work,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke unreported terminal priority",
            "--expected-outputs",
            "visible report",
            "--no-artifact-expected",
            "--checklist",
            json.dumps(["complete", "report"]),
            "--success-criteria",
            json.dumps(["unreported terminal beats terminal refs"]),
        )
        run(root, "complete", "--work-id", completed_work, "--note", "done but unreported")

        run(
            root,
            "start",
            "--work-id",
            terminal_ref_work,
            "--owner-session-key",
            "agent:main:telegram:direct:test-user",
            "--visible-delivery",
            visible_delivery,
            "--request-summary",
            "Smoke terminal ref secondary",
            "--expected-outputs",
            "terminal review",
            "--checklist",
            json.dumps(["wait", "terminal-ref"]),
            "--success-criteria",
            json.dumps(["terminal ref remains secondary"]),
        )
        run(
            root,
            "wait",
            "--work-id",
            terminal_ref_work,
            "--status",
            "waiting_subagent",
            "--subagent-session-keys",
            ref,
            "--note",
            "waiting for terminal review",
        )

        original_lookup = LEDGER_MODULE.load_openclaw_task_lookup

        def fake_lookup(lookup: str) -> tuple[dict[str, Any] | None, str | None]:
            if lookup == ref:
                return {
                    "taskId": "task-terminal-secondary",
                    "runId": "run-terminal-secondary",
                    "runtime": "subagent",
                    "status": "succeeded",
                    "terminalSummary": "completed",
                    "endedAt": 12345,
                }, None
            return None, None

        try:
            LEDGER_MODULE.load_openclaw_task_lookup = fake_lookup
            check = LEDGER_MODULE.watchdog_check(root, cooldown_seconds=0)
        finally:
            LEDGER_MODULE.load_openclaw_task_lookup = original_lookup

        assert_true(check["wake_reason"] == "recovery", "unreported terminal recovery should beat terminal ref reconciliation")
        assert_true(check["recoveries"][0]["work_id"] == completed_work, "unreported terminal work should be first recovery")
        assert_true(check["recoveries"][0]["reason"] == "completed_unreported", "recovery reason should remain completed_unreported")
        return {
            "wake_reason": check["wake_reason"],
            "recovery_reason": check["recoveries"][0]["reason"],
            "recovered_work_id": check["recoveries"][0]["work_id"],
        }


def smoke_terminal_refs_precede_stale_recovery() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-terminal-ref-precedence"
        ref = "agent:main:subagent:terminal-review"
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
            "Smoke terminal subagent reconciliation precedence",
            "--expected-outputs",
            "terminal review",
            "--checklist",
            json.dumps(["wait", "terminal-ref"]),
            "--success-criteria",
            json.dumps(["terminal refs beat stale recovery"]),
        )
        run(
            root,
            "wait",
            "--work-id",
            work_id,
            "--status",
            "waiting_subagent",
            "--subagent-session-keys",
            ref,
            "--note",
            "waiting for terminal review",
        )
        age_events(root, work_id, 7200)

        original_lookup = LEDGER_MODULE.load_openclaw_task_lookup

        def fake_lookup(lookup: str) -> tuple[dict[str, Any] | None, str | None]:
            if lookup == ref:
                return {
                    "taskId": "task-terminal-review",
                    "runId": "run-terminal-review",
                    "runtime": "subagent",
                    "status": "succeeded",
                    "label": "terminal review",
                    "terminalSummary": "completed",
                    "progressSummary": "review result text",
                    "endedAt": 12345,
                }, None
            return None, None

        try:
            LEDGER_MODULE.load_openclaw_task_lookup = fake_lookup
            check = LEDGER_MODULE.watchdog_check(root, cooldown_seconds=0)
            assert_true(check["wake_reason"] == "referenced_task_reconciliation", "terminal refs should beat generic stale recovery")
            terminal_refs = check["terminal_refs"]["terminal_refs"]
            assert_true(len(terminal_refs) == 1, "expected one terminal ref")
            assert_true(terminal_refs[0]["progressSummary"] == "review result text", "progressSummary should be included")
            assert_true(terminal_refs[0].get("terminal_ref_fingerprint"), "terminal ref fingerprint should be included")
            assert_true(len(terminal_refs[0].get("terminal_ref_fingerprints", [])) == 2, "terminal ref aliases should include stable and legacy fingerprints")

            run(
                root,
                "terminal-ref-handled",
                "--work-id",
                work_id,
                "--ref",
                ref,
                "--terminal-status",
                "succeeded",
                "--terminal-ref-fingerprints",
                json.dumps(terminal_refs[0]["terminal_ref_fingerprints"]),
                "--resolution",
                "integrated",
                "--note",
                "terminal result integrated into recovery analysis",
            )
            handled_check = LEDGER_MODULE.watchdog_check(root, cooldown_seconds=0)
            assert_true(handled_check["wake_reason"] == "recovery", "handled terminal ref should fall back to stale recovery")
            assert_true(handled_check["terminal_refs"]["ignored"][0]["reason"] == "handled", "handled terminal ref should be ignored durably")
        finally:
            LEDGER_MODULE.load_openclaw_task_lookup = original_lookup

        return {
            "work_id": work_id,
            "wake_reason_before_handled": check["wake_reason"],
            "progress_summary_included": terminal_refs[0]["progressSummary"] == "review result text",
            "handled_falls_back_to_recovery": handled_check["wake_reason"] == "recovery",
        }


def smoke_shared_terminal_ref_surfaces_each_work() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        ref = "agent:main:subagent:shared-review"
        visible_delivery = json.dumps({"channel": "telegram", "target": "telegram:test-user"})

        for work_id in ("smoke-shared-terminal-ref-a", "smoke-shared-terminal-ref-b"):
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
                f"Smoke shared terminal ref {work_id}",
                "--expected-outputs",
                "terminal review",
                "--checklist",
                json.dumps(["wait", "terminal-ref"]),
                "--success-criteria",
                json.dumps(["shared terminal ref is surfaced per work"]),
            )
            run(
                root,
                "wait",
                "--work-id",
                work_id,
                "--status",
                "waiting_subagent",
                "--subagent-session-keys",
                ref,
                "--note",
                "waiting for shared terminal review",
            )

        original_lookup = LEDGER_MODULE.load_openclaw_task_lookup
        calls: list[str] = []

        def fake_lookup(lookup: str) -> tuple[dict[str, Any] | None, str | None]:
            calls.append(lookup)
            return {
                "taskId": "task-shared-review",
                "runId": "run-shared-review",
                "runtime": "subagent",
                "status": "succeeded",
                "terminalSummary": "completed",
                "endedAt": 12345,
            }, None

        try:
            LEDGER_MODULE.load_openclaw_task_lookup = fake_lookup
            result = LEDGER_MODULE.find_referenced_terminal_tasks(root)
        finally:
            LEDGER_MODULE.load_openclaw_task_lookup = original_lookup

        work_ids = {item["work_id"] for item in result["terminal_refs"]}
        assert_true(result["has_terminal_refs"], "shared terminal ref should produce terminal refs")
        assert_true(work_ids == {"smoke-shared-terminal-ref-a", "smoke-shared-terminal-ref-b"}, "shared terminal ref should surface for each active work")
        assert_true(calls == [ref], "shared terminal ref lookup should be cached")
        return {
            "surfaced_work_ids": sorted(work_ids),
            "lookup_calls": calls,
        }


def main() -> int:
    result = {
        "ok": True,
        "smokes": {
            "json_flag_compatibility": smoke_json_flag_compatibility(),
            "recovery_report_path": smoke_recovery_report_path(),
            "report_sent_requires_delivery": smoke_report_sent_requires_delivery(),
            "message_sent_hook_records_report_proof": smoke_message_sent_hook_records_report_proof(),
            "message_sent_without_tool_use_id_is_time_bounded": smoke_message_sent_without_tool_use_id_is_time_bounded(),
            "active_visible_delivery_recovery_reconciles_before_duplicate": smoke_active_visible_delivery_recovery_reconciles_before_duplicate(),
            "terminal_visible_delivery_recovery_reconciles_before_duplicate": smoke_terminal_visible_delivery_recovery_reconciles_before_duplicate(),
            "referenced_codex_uuid_not_terminal_task_lookup": smoke_referenced_codex_uuid_not_terminal_task_lookup(),
            "openclaw_bin_env_used_for_task_commands": smoke_openclaw_bin_env_used_for_task_commands(),
            "visible_update_route_does_not_contaminate_report_route": smoke_visible_update_route_does_not_contaminate_report_route(),
            "report_sent_rejects_active_work": smoke_report_sent_rejects_active_work(),
            "complete_reported_records_terminal_proof": smoke_complete_reported_records_terminal_proof(),
            "abandoned_absorbs_late_lifecycle_events": smoke_abandoned_absorbs_late_lifecycle_events(),
            "orphans_ignore_fresh_tasks": smoke_orphans_ignore_fresh_tasks(),
            "orphan_uses_idle_activity_for_freshness": smoke_orphan_uses_idle_activity_for_freshness(),
            "insufficient_recovery_context": smoke_insufficient_recovery_context(),
            "missing_expected_outputs_context": smoke_missing_expected_outputs_context(),
            "failed_unreported_context_gaps_not_masked": smoke_failed_unreported_context_gaps_not_masked(),
            "quick_start_visible_stale_defaults": smoke_quick_start_visible_stale_defaults(),
            "per_entry_stale_after": smoke_per_entry_stale_after(),
            "waiting_user_minimum_stale_after": smoke_waiting_user_minimum_stale_after(),
            "waiting_user_context_gaps_not_masked": smoke_waiting_user_context_gaps_not_masked(),
            "waiting_user_wait_does_not_refresh_activity": smoke_waiting_user_wait_does_not_refresh_activity(),
            "prune_terminal_dry_run_and_apply": smoke_prune_terminal_dry_run_and_apply(),
            "gateway_side_effect_idempotency_policy": smoke_gateway_side_effect_idempotency_policy(),
            "resume_start_does_not_hide_unreported_completion": smoke_resume_start_does_not_hide_unreported_completion(),
            "unreported_terminal_precedes_terminal_refs": smoke_unreported_terminal_precedes_terminal_refs(),
            "terminal_refs_precede_stale_recovery": smoke_terminal_refs_precede_stale_recovery(),
            "shared_terminal_ref_surfaces_each_work": smoke_shared_terminal_ref_surfaces_each_work(),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

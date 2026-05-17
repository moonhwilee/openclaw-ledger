#!/usr/bin/env python3
"""Deterministic smoke tests for the workspace work ledger.

The smoke uses an isolated temporary root and never touches the real ledger.
It proves the completed-but-unreported recovery-report path:

completed_unreported work -> recovery packet -> wake-delivered record ->
report-sent record -> reported terminal state.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[2]
LEDGER = WORKSPACE / "src" / "work_ledger.py"


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

        after_cooldown = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(after_cooldown["has_recoveries"], "wake-delivered must not be terminal after cooldown")
        assert_true(
            after_cooldown["recoveries"][0]["recovery_fingerprint"] == packet["recovery_fingerprint"],
            "same unfinished work should recover again after cooldown",
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
            item["visible_delivery"].get("message_id") == "smoke-visible-report",
            "visible report delivery id should be persisted",
        )
        final_scan = run(root, "scan", "--cooldown-seconds", "0")
        assert_true(not final_scan["has_recoveries"], "reported work should not recover")

        return {
            "work_id": work_id,
            "recovery_reason": packet["reason"],
            "final_status": item["status"],
            "visible_report_recorded": item["visible_delivery"].get("message_id") == "smoke-visible-report",
            "wake_suppressed_after_delivery": not suppressed["has_recoveries"],
            "wake_recoverable_after_cooldown": after_cooldown["has_recoveries"],
            "reported_scan_clean": not final_scan["has_recoveries"],
        }


def smoke_report_sent_requires_delivery() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="work-ledger-smoke-") as tmp:
        root = Path(tmp)
        work_id = "smoke-report-delivery-required"
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
        return {
            "work_id": work_id,
            "status_after_failed_reports": state["status"],
            "missing_visible_delivery_rejected": "--visible-delivery is required" in missing_delivery,
            "missing_delivery_message_id_rejected": "--delivery-message-id is required" in missing_message_id,
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

        return {
            "work_id": work_id,
            "stale_after_seconds": packet["stale_after_seconds"],
            "reason": packet["reason"],
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


def main() -> int:
    result = {
        "ok": True,
        "smokes": {
            "recovery_report_path": smoke_recovery_report_path(),
            "report_sent_requires_delivery": smoke_report_sent_requires_delivery(),
            "insufficient_recovery_context": smoke_insufficient_recovery_context(),
            "missing_expected_outputs_context": smoke_missing_expected_outputs_context(),
            "per_entry_stale_after": smoke_per_entry_stale_after(),
            "waiting_user_minimum_stale_after": smoke_waiting_user_minimum_stale_after(),
            "gateway_side_effect_idempotency_policy": smoke_gateway_side_effect_idempotency_policy(),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

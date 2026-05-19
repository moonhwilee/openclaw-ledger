#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "hook_action_classifier_golden.json"
CONTRACT = ROOT / "src" / "hook_event_contract.py"

spec = importlib.util.spec_from_file_location("hook_event_contract", CONTRACT)
assert spec and spec.loader
HOOK_CONTRACT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HOOK_CONTRACT)


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def hook_event(payload: dict[str, Any]) -> str | None:
    if payload.get("hook_event_name") in {"PreToolUse", "PermissionRequest", "PostToolUse", "Stop"}:
        return payload["hook_event_name"]
    if payload.get("type") == "message" and payload.get("action") in {"send", "sent"}:
        return f"message:{payload.get('action')}"
    if payload.get("type") == "command":
        return f"command:{payload.get('action') or 'PreToolUse'}"
    return None


def route_matches(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    tool_input = payload.get("tool_input")
    expected = state.get("visible_delivery") or {}
    if not isinstance(tool_input, dict):
        return False
    owner = state.get("owner_session_key")
    payload_session = payload.get("session_key") or payload.get("sessionKey") or payload.get("session_id")
    input_session = tool_input.get("session_key") or tool_input.get("sessionKey") or tool_input.get("session_id")
    if not owner or (not payload_session and not input_session):
        return False
    if payload_session and payload_session != owner:
        return False
    if input_session and input_session != owner:
        return False
    if tool_input.get("channel") != expected.get("channel"):
        return False
    if normalize_target(expected.get("channel"), tool_input.get("target") or tool_input.get("to")) != normalize_target(expected.get("channel"), expected.get("target") or expected.get("to")):
        return False
    return all(
        (expected.get(key) in (None, "")) == (tool_input.get(key) in (None, ""))
        and (expected.get(key) in (None, "") or tool_input.get(key) == expected.get(key))
        for key in ("accountId", "threadId")
    )


def normalize_target(channel: Any, target: Any) -> str:
    text = str(target or "")
    if channel == "telegram" and text.startswith("telegram:"):
        return text.split(":", 1)[1]
    if channel == "telegram" and text.startswith("telegram-"):
        return text.split("-", 1)[1]
    return text


def delivered_route_matches(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    expected = state.get("visible_delivery") or {}
    if payload.get("channel") != expected.get("channel"):
        return False
    return str(payload.get("target") or payload.get("to") or "") == str(expected.get("target") or expected.get("to") or "")


def has_delivery_id(payload: dict[str, Any]) -> bool:
    return any(payload.get(key) for key in ("message_id", "messageId", "delivery_message_id"))


def expected_report_proof(case: dict[str, Any]) -> str | None:
    if hook_event(case["payload"]) != "message:sent":
        return None
    return "record" if delivered_route_matches(case["payload"], case.get("state") or {}) else "ignore"


def expected_decision(system: str, case: dict[str, Any]) -> str:
    payload = case["payload"]
    state = case.get("state") or {}
    action_class = case["action_class"]
    event = hook_event(payload)
    if event == "message:sent":
        return "allow"
    if event == "Stop":
        return "nudge" if system == "ledger" and state.get("status") in {"completed_unreported", "failed_unreported"} and not state.get("completion_report_sent") else "allow"
    if (
        action_class == "external_message"
        and state.get("status") in {"completed_unreported", "failed_unreported"}
        and not state.get("completion_report_sent")
        and case.get("requires_explicit_visible_route")
        and not route_matches(payload, state)
    ):
        return "block"
    if (
        system == "ledger"
        and action_class == "external_message"
        and state.get("status") in {"completed_unreported", "failed_unreported"}
        and not state.get("completion_report_sent")
        and route_matches(payload, state)
    ):
        return "allow"
    if action_class in {"repo_changes", "external_message", "public_post", "destructive", "gateway_runtime"} and case.get("requires_user_approval_candidate"):
        return "block"
    return "allow"


def main() -> int:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    classes = set(fixture["action_classes"])
    assert_true(classes == {"read_only", "local_files", "repo_changes", "external_message", "public_post", "destructive", "gateway_runtime"}, "action classes must stay aligned with Ledger vocabulary")
    assert_true(len(fixture["cases"]) <= 22, "golden fixture must stay at or below the documented 22-case representative cap")
    ids: set[str] = set()
    for case in fixture["cases"]:
        case_id = case["id"]
        assert_true(hook_event(case["payload"]) is not None, f"{case_id}: payload must use a recognized hook shape")
        assert_true(case_id not in ids, f"duplicate case id: {case_id}")
        ids.add(case_id)
        assert_true(case["action_class"] in classes, f"{case_id}: unknown action class")
        assert_true(case["confidence"] in {"high", "medium", "low"}, f"{case_id}: invalid confidence")
        assert_true("classifier_approval_sensitive_candidate" in case, f"{case_id}: missing classifier approval expectation")
        classification = HOOK_CONTRACT.classify_hook_action(case["payload"])
        assert_true(classification["schema_version"] == "hook.action_classifier.v0.1", f"{case_id}: classifier schema mismatch")
        assert_true(classification["hook_action_class"] == case["action_class"], f"{case_id}: fixture classifier mismatch")
        assert_true(classification["confidence"] == case["confidence"], f"{case_id}: classifier confidence mismatch")
        assert_true(classification["is_non_idempotent"] == case["is_non_idempotent"], f"{case_id}: classifier idempotency mismatch")
        assert_true(classification["approval_sensitive_candidate"] == case["classifier_approval_sensitive_candidate"], f"{case_id}: classifier approval candidate mismatch")
        if "classifier_current_guardrail_unsafe_candidate" in case:
            assert_true(classification["current_guardrail_unsafe_candidate"] == case["classifier_current_guardrail_unsafe_candidate"], f"{case_id}: current unsafe literal expectation mismatch")
        assert_true(classification["current_guardrail_unsafe_candidate"] == HOOK_CONTRACT.is_unsafe_tool_attempt(case["payload"]), f"{case_id}: current unsafe candidate must match bounded unsafe detector")
        assert_true(isinstance(classification["reasons"], list) and classification["reasons"], f"{case_id}: classifier must explain reasons")
        assert_true(isinstance(classification["summary"], str) and classification["summary"], f"{case_id}: classifier must summarize")
        assert_true(expected_decision("ledger", case) == case["ledger_target_decision"], f"{case_id}: ledger target decision mismatch")
        assert_true(expected_decision("goalflow", case) == case["goalflow_target_decision"], f"{case_id}: goalflow target decision mismatch")
        if hook_event(case["payload"]) == "message:sent":
            assert_true(has_delivery_id(case["payload"]), f"{case_id}: visible report proof telemetry must include a delivery id")
            assert_true(expected_report_proof(case) == case.get("ledger_report_proof_target"), f"{case_id}: ledger report proof target mismatch")
        if case.get("runtime_parity") is False:
            assert_true("known_runtime_gap" in case, f"{case_id}: runtime_parity=false requires known_runtime_gap")
            assert_true("ledger_current_decision" in case and "goalflow_current_decision" in case, f"{case_id}: runtime_parity=false requires current decisions")
        if "ledger_current_decision" in case or case.get("runtime_parity") is True:
            runtime = HOOK_CONTRACT.ledger_guardrail(case["payload"], case.get("state") or {})
            expected = case.get("ledger_current_decision", case["ledger_target_decision"])
            assert_true(runtime["decision"] == expected, f"{case_id}: ledger runtime decision mismatch")
        if "goalflow_current_decision" in case or case.get("runtime_parity") is True:
            runtime = HOOK_CONTRACT.goalflow_guardrail(case["payload"], case.get("state") or {})
            expected = case.get("goalflow_current_decision", case["goalflow_target_decision"])
            assert_true(runtime["decision"] == expected, f"{case_id}: goalflow runtime decision mismatch")
    print(json.dumps({"ok": True, "cases": len(fixture["cases"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

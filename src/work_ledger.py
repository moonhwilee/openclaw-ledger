#!/usr/bin/env python3
"""Workspace-local work ledger for recoverable OpenClaw tasks.

This is intentionally outside OpenClaw internals. It records enough state for a
fresh main-session turn to resume unfinished multi-step work without guessing.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hook_event_contract import ledger_guardrail, ledger_observation, ledger_required_visible_report_attempt


SCHEMA_VERSION = 1
DEFAULT_ROOT = Path(
    os.environ.get(
        "OPENCLAW_WORKSPACE",
        str(Path.home() / ".openclaw" / "workspace"),
    )
)
ACTIVE_STATES = {"running", "waiting_subagent", "waiting_user", "verifying"}
TERMINAL_STATES = {"reported", "abandoned"}
UNREPORTED_TERMINAL_STATES = {"completed_unreported", "failed_unreported"}
NON_MATERIAL_RECOVERY_EVENTS = {"recovery_wake_delivered", "hook_observed", "visible_update_sent", "wait_reminder_sent"}
ROUTE_PROOF_KEYS = {
    "message_id",
    "last_update_message_id",
    "last_wait_reminder_message_id",
    "delivery_message_id",
}
VISIBLE_DELIVERY_ROUTE_KEYS = {"session_key", "sessionKey", "channel", "target", "to", "accountId", "threadId"}
PENDING_COMPLETION_REPORT_PROOF_WINDOW_SECONDS = 5 * 60
ACTIVE_STATUS_EVENT_TRANSITIONS = {
    "wait": "__wait_status__",
    "verify": "verifying",
    "complete": "completed_unreported",
    "fail": "failed_unreported",
    "abandon": "abandoned",
}
UNREPORTED_TERMINAL_EVENT_TRANSITIONS = {
    "report_sent": "reported",
    "abandon": "abandoned",
}
WORK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SIDE_EFFECT_CLASSES = {
    "read_only",
    "local_files",
    "repo_changes",
    "external_message",
    "public_post",
    "destructive",
    "gateway_runtime",
}
EVENT_TYPES = {
    "start",
    "progress",
    "wait",
    "verify",
    "complete",
    "fail",
    "visible_update_sent",
    "wait_reminder_sent",
    "report_sent",
    "abandon",
    "recovery_wake_delivered",
    "hook_observed",
}
REPEAT_POLICIES = {"repeatable", "reconcile_first", "never_repeat_without_user_approval"}
DEFAULT_REPEAT_POLICY = {
    "read_only": "repeatable",
    "local_files": "reconcile_first",
    "repo_changes": "reconcile_first",
    "external_message": "never_repeat_without_user_approval",
    "public_post": "never_repeat_without_user_approval",
    "destructive": "never_repeat_without_user_approval",
    "gateway_runtime": "never_repeat_without_user_approval",
}
DEFAULT_THRESHOLDS_SECONDS = {
    "running": 30 * 60,
    "waiting_subagent": 60 * 60,
    "waiting_user": 24 * 60 * 60,
    "verifying": 20 * 60,
    "completed_unreported": 0,
    "failed_unreported": 0,
}
DEFAULT_ORPHAN_MIN_AGE_SECONDS = 30 * 60
DEFAULT_ORPHAN_WARNING_SUPPRESSION_SECONDS = 24 * 60 * 60
DEFAULT_TERMINAL_RETENTION_DAYS = 30
ORPHAN_HANDLED_RESOLUTIONS = {
    "terminal_no_action",
    "referenced_after_refresh",
}
TERMINAL_REF_HANDLED_RESOLUTIONS = {
    "integrated",
    "superseded",
    "terminal_no_action",
    "reported_failure",
}
MIN_STALE_AFTER_SECONDS = {
    "running": 5 * 60,
    "waiting_subagent": 15 * 60,
    "waiting_user": 60 * 60,
    "verifying": 5 * 60,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def load_json_arg(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON argument: {exc}") from exc


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def validate_positive_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if value <= 0:
        raise SystemExit(f"{name} must be a positive integer")
    return value


def validate_stale_after_seconds(value: int | None, status: str = "running") -> int | None:
    value = validate_positive_int(value, "--stale-after-seconds")
    if value is None:
        return None
    minimum = MIN_STALE_AFTER_SECONDS.get(status, 5 * 60)
    if value < minimum:
        raise SystemExit(f"--stale-after-seconds must be at least {minimum} seconds for {status}")
    return value


def validate_work_id(work_id: str) -> None:
    if not WORK_ID_RE.fullmatch(work_id):
        raise SystemExit("invalid work_id: use 1-128 chars from letters, digits, _, ., :, -; no slashes or path traversal")


def state_file_path(root: Path, work_id: str) -> Path:
    if WORK_ID_RE.fullmatch(work_id):
        filename = f"{work_id}.json"
    else:
        digest = hashlib.sha256(work_id.encode("utf-8", errors="replace")).hexdigest()
        filename = f"unsafe-{digest}.json"
    return state_dir(root) / filename


def load_json_string_list_arg(value: str | None, name: str) -> list[str]:
    parsed = load_json_arg(value, [])
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) and item.strip() for item in parsed):
        raise SystemExit(f"{name} must be a non-empty JSON array of strings")
    return parsed


def load_optional_json_string_list_arg(value: str | None, name: str) -> list[str] | None:
    if value is None:
        return None
    return load_json_string_list_arg(value, name)


def load_json_object_arg(value: str | None, name: str, *, required: bool = False) -> dict[str, Any] | None:
    if value is None:
        if required:
            raise SystemExit(f"{name} is required")
        return None
    parsed = load_json_arg(value, None)
    if not isinstance(parsed, dict):
        raise SystemExit(f"{name} must be a JSON object")
    if required and not parsed:
        raise SystemExit(f"{name} must not be empty")
    return parsed


def validate_visible_delivery(
    value: dict[str, Any] | None,
    name: str,
    *,
    required: bool = False,
    require_channel_target: bool = False,
) -> dict[str, Any] | None:
    if value is None:
        if required:
            raise SystemExit(f"{name} is required")
        return None
    has_session = isinstance(value.get("session_key") or value.get("sessionKey"), str)
    has_channel_target = isinstance(value.get("channel"), str) and isinstance(value.get("target") or value.get("to"), str)
    if require_channel_target and not has_channel_target:
        raise SystemExit(f"{name} must include channel plus target/to")
    if not (has_session or has_channel_target):
        raise SystemExit(f"{name} must include session_key/sessionKey or channel plus target/to")
    return value


def canonical_visible_delivery_route(value: dict[str, Any]) -> dict[str, Any]:
    route: dict[str, Any] = {}
    session_key = value.get("session_key") or value.get("sessionKey")
    if session_key not in (None, ""):
        route["session_key"] = session_key
    channel = value.get("channel")
    target = value.get("target") or value.get("to")
    if channel not in (None, ""):
        route["channel"] = channel
    if target not in (None, ""):
        route["target"] = normalize_visible_delivery_target(channel, target)
    for key in ("accountId", "threadId"):
        if value.get(key) not in (None, ""):
            route[key] = value[key]
    return route


def normalize_visible_delivery_target(channel: Any, target: Any) -> Any:
    if channel != "telegram" or not isinstance(target, str):
        return target
    if target.startswith("telegram:"):
        return target.split(":", 1)[1]
    if target.startswith("telegram-"):
        return target.split("-", 1)[1]
    return target


def assert_visible_delivery_matches_existing(existing: dict[str, Any], reported: dict[str, Any]) -> None:
    expected_route = canonical_visible_delivery_route(
        existing.get("completion_visible_delivery") or existing.get("visible_delivery") or {}
    )
    reported_route = canonical_visible_delivery_route(reported)
    if not expected_route:
        return
    if reported_route != expected_route:
        raise SystemExit("report-sent visible_delivery route mismatch")
    for key, expected_value in expected_route.items():
        reported_value = reported_route.get(key)
        if reported_value in (None, ""):
            raise SystemExit(f"report-sent visible_delivery missing original route key: {key}")
        if reported_value != expected_value:
            raise SystemExit(f"report-sent visible_delivery route mismatch for {key}")


def visible_delivery_matches_completion_route(state: dict[str, Any], visible_delivery: dict[str, Any] | None) -> bool:
    expected_route = canonical_visible_delivery_route(
        state.get("completion_visible_delivery") or state.get("visible_delivery") or {}
    )
    if not expected_route or not visible_delivery:
        return False
    return canonical_visible_delivery_route(visible_delivery) == expected_route


def delivery_message_id_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("delivery_message_id", "message_id", "messageId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int):
            return str(value)
    return None


def delivery_route_without_session(value: dict[str, Any]) -> dict[str, Any]:
    route = canonical_visible_delivery_route(value)
    route.pop("session_key", None)
    return route


def payload_session_matches_owner(payload: dict[str, Any], owner_session: Any) -> bool:
    payload_session = payload.get("session_key") or payload.get("sessionKey") or payload.get("session_id")
    return bool(payload_session) and payload_session == owner_session


def pending_completion_report_send_event(work_id: str, payload: dict[str, Any], state: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
    if not ledger_required_visible_report_attempt(payload, state):
        return None
    expected = canonical_visible_delivery_route(
        state.get("completion_visible_delivery") or state.get("visible_delivery") or {}
    )
    if not expected:
        return None
    return {
        "event_type": "hook_observed",
        "work_id": work_id,
        "note": "Observed allowed visible completion report send attempt.",
        "hook_observations": [observation],
        "hook_fingerprints": [observation["fingerprint"]],
        "pending_completion_report_send": {
            "route": expected,
            "fingerprint": observation["fingerprint"],
            "delivery_fingerprint": observation.get("delivery_fingerprint"),
            "tool_use_id": payload.get("tool_use_id"),
        },
    }


def report_sent_event_from_message_sent(work_id: str, payload: dict[str, Any], state: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("type") != "message" or payload.get("action") != "sent":
        return None
    if state.get("status") not in UNREPORTED_TERMINAL_STATES or state.get("completion_report_sent"):
        return None
    pending = state.get("pending_completion_report_send")
    if not isinstance(pending, dict):
        return None
    pending_tool_use_id = pending.get("tool_use_id")
    payload_tool_use_id = payload.get("tool_use_id")
    pending_at = parse_time(pending.get("observed_at"))
    if not pending_at or time.time() - pending_at > PENDING_COMPLETION_REPORT_PROOF_WINDOW_SECONDS:
        return None
    if pending_tool_use_id and payload_tool_use_id and payload_tool_use_id != pending_tool_use_id:
        return None
    delivery_message_id = delivery_message_id_from_payload(payload)
    if not delivery_message_id:
        return None
    expected = canonical_visible_delivery_route(
        state.get("completion_visible_delivery") or state.get("visible_delivery") or {}
    )
    if not expected:
        return None
    if not payload_session_matches_owner(payload, state.get("owner_session_key")):
        return None
    delivered = delivery_route_without_session(payload)
    expected_delivery = {key: value for key, value in expected.items() if key != "session_key"}
    if delivered != expected_delivery:
        return None
    return {
        "event_type": "report_sent",
        "work_id": work_id,
        "note": "Visible completion report delivery proof observed from message:sent telemetry.",
        "visible_delivery": expected,
        "delivery_message_id": delivery_message_id,
        "hook_observations": [observation],
        "hook_fingerprints": [observation["fingerprint"]],
    }


def validate_owner_session_key(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise SystemExit("--owner-session-key is required")
    if value.startswith("telegram:") or value.startswith("discord:") or value.startswith("slack:") or value.isdigit():
        raise SystemExit("--owner-session-key must be an OpenClaw session key, not a delivery route; put chat/channel routing in --visible-delivery")
    if not (value.startswith("agent:") or value.startswith("session:")):
        raise SystemExit("--owner-session-key must start with agent: or session:")
    return value


def ledger_dir(root: Path) -> Path:
    return root / "state" / "work-ledger"


def events_path(root: Path) -> Path:
    return ledger_dir(root) / "events.jsonl"


def state_dir(root: Path) -> Path:
    return ledger_dir(root) / "state"


def lock_path(root: Path) -> Path:
    return ledger_dir(root) / ".lock"


@contextlib.contextmanager
def file_lock(root: Path):
    ledger_dir(root).mkdir(parents=True, exist_ok=True)
    with lock_path(root).open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def next_sequence(root: Path) -> int:
    seq = 0
    for event in read_events(root, include_corrupt=False):
        seq = max(seq, int(event.get("sequence", 0)))
    return seq + 1


def append_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    with file_lock(root):
        return append_event_unlocked(root, event)


def append_event_unlocked(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    return append_events_unlocked(root, [event])[0]


def append_events_unlocked(root: Path, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        return []
    sequence = next_sequence(root)
    prepared: list[dict[str, Any]] = []
    event_at = now_iso()
    for offset, event in enumerate(events):
        item = dict(event)
        item.setdefault("schema_version", SCHEMA_VERSION)
        item.setdefault("event_at", event_at)
        item["sequence"] = sequence + offset
        prepared.append(item)
    events_path(root).parent.mkdir(parents=True, exist_ok=True)
    with events_path(root).open("a", encoding="utf-8") as fh:
        for event in prepared:
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    write_all_states_unlocked(root)
    return prepared

def read_events(root: Path, include_corrupt: bool = True) -> list[dict[str, Any]]:
    path = events_path(root)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                if include_corrupt:
                    events.append({"event_type": "_corrupt", "line_no": line_no, "raw": text[:200]})
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def grouped_events(root: Path) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for event in read_events(root, include_corrupt=False):
        work_id = event.get("work_id")
        if isinstance(work_id, str) and work_id:
            groups.setdefault(work_id, []).append(event)
    return groups


def merge_list(existing: list[Any], incoming: list[Any]) -> list[Any]:
    seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in existing}
    result = list(existing)
    for item in incoming:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def transition_status(current_status: str, event: dict[str, Any]) -> str:
    event_type = event.get("event_type")
    if current_status in UNREPORTED_TERMINAL_STATES:
        return UNREPORTED_TERMINAL_EVENT_TRANSITIONS.get(event_type, current_status)
    if event_type == "start":
        return "running"
    if current_status in TERMINAL_STATES:
        return current_status
    next_status = ACTIVE_STATUS_EVENT_TRANSITIONS.get(event_type)
    if next_status == "__wait_status__":
        return event.get("status") or "waiting_subagent"
    return next_status or current_status


def derive_state(work_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    events = sorted(events, key=lambda e: int(e.get("sequence", 0)))
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "work_id": work_id,
        "status": "running",
        "checklist": [],
        "success_criteria": [],
        "expected_outputs": [],
        "artifact_paths": [],
        "verification": {},
        "subagents": [],
        "openclaw_task_ids": [],
        "subagent_session_keys": [],
        "side_effect_class": "read_only",
        "side_effects_performed": [],
        "external_actions_attempted": [],
        "hook_observations": [],
        "hook_fingerprints": [],
        "no_artifact_expected": False,
        "visible_delivery": {},
        "completion_visible_delivery": {},
        "visible_delivery_proof": {},
        "completion_report_sent": False,
        "last_event": None,
        "last_progress_at": None,
        "last_visible_update_at": None,
        "last_wait_reminder_at": None,
        "terminal_unreported_event": None,
        "terminal_unreported_next_recovery_action": None,
        "pending_completion_report_send": None,
        "last_activity_at": None,
        "stale_after_seconds": None,
        "created_at": None,
        "updated_at": None,
        "next_recovery_action": "inspect_ledger_and_current_state",
        "events_count": 0,
    }
    current_status = "running"

    for event in events:
        event_type = event.get("event_type")
        state["events_count"] += 1
        state["last_event"] = event
        state["updated_at"] = event.get("event_at")
        if event_type not in NON_MATERIAL_RECOVERY_EVENTS:
            state["last_material_event"] = event
        if state["created_at"] is None:
            state["created_at"] = event.get("created_at") or event.get("event_at")

        for key in (
            "owner_session_key",
            "user_message_id",
            "user_message_timestamp",
            "request_summary",
            "cwd",
            "branch",
            "commit",
            "idempotency_key",
            "next_recovery_action",
            "cron_run_id",
            "repeat_policy",
            "stale_after_seconds",
            "no_artifact_expected",
        ):
            if event.get(key) not in (None, ""):
                state[key] = event[key]

        if event.get("side_effect_class"):
            state["side_effect_class"] = event["side_effect_class"]
            state["repeat_policy"] = DEFAULT_REPEAT_POLICY.get(event["side_effect_class"], "reconcile_first")
        if event.get("repeat_policy"):
            state["repeat_policy"] = event["repeat_policy"]
        if event.get("checklist"):
            state["checklist"] = event["checklist"]
        if event.get("success_criteria"):
            state["success_criteria"] = event["success_criteria"]
        if event.get("verification"):
            state["verification"] = event["verification"]
        if event.get("pending_completion_report_send"):
            pending = dict(event["pending_completion_report_send"])
            pending.setdefault("observed_at", event.get("event_at"))
            state["pending_completion_report_send"] = pending
        if event.get("visible_delivery"):
            if event_type == "start":
                state["visible_delivery"] = {**state.get("visible_delivery", {}), **event["visible_delivery"]}
                state["completion_visible_delivery"] = canonical_visible_delivery_route(event["visible_delivery"])
            elif event_type == "report_sent":
                # Do not let progress/reminder delivery routes change the
                # original completion-report route. Report-sent validation uses
                # completion_visible_delivery and stores only proof ids here.
                pass
        for key in (
            "expected_outputs",
            "artifact_paths",
            "subagents",
            "openclaw_task_ids",
            "subagent_session_keys",
            "side_effects_performed",
            "external_actions_attempted",
            "hook_observations",
            "hook_fingerprints",
        ):
            if event.get(key):
                state[key] = merge_list(state.get(key, []), event[key])

        silent_waiting_user = event_type == "wait" and (event.get("status") or current_status) == "waiting_user"
        silent_waiting_user_progress = event_type == "progress" and current_status == "waiting_user"
        if event_type in {"start", "progress", "wait", "verify"} and not silent_waiting_user and not silent_waiting_user_progress:
            state["last_progress_at"] = event.get("event_at")
        next_status = transition_status(current_status, event)
        if event_type == "wait" and next_status == "waiting_user" and event.get("stale_after_seconds") is None:
            state["stale_after_seconds"] = DEFAULT_THRESHOLDS_SECONDS["waiting_user"]
        if event_type == "complete" and next_status == "completed_unreported" and current_status != next_status:
            state["terminal_unreported_event"] = event
            state["terminal_unreported_next_recovery_action"] = state.get("next_recovery_action")
        elif event_type == "fail" and next_status == "failed_unreported" and current_status != next_status:
            state["failure_reason"] = event.get("failure_reason") or event.get("note")
            state["terminal_unreported_event"] = event
            state["terminal_unreported_next_recovery_action"] = state.get("next_recovery_action")
        current_status = next_status
        if event_type == "report_sent" and current_status == "reported":
            state["completion_report_sent"] = True
            state["report_sent_at"] = event.get("event_at")
            if event.get("delivery_message_id"):
                state.setdefault("visible_delivery_proof", {})["message_id"] = event["delivery_message_id"]
        elif event_type == "visible_update_sent":
            update_route_matches = visible_delivery_matches_completion_route(state, event.get("visible_delivery"))
            if update_route_matches:
                state["last_visible_update_at"] = event.get("event_at")
                state["last_visible_update_delivery"] = canonical_visible_delivery_route(event.get("visible_delivery") or {})
            if update_route_matches and event.get("delivery_message_id"):
                state.setdefault("visible_delivery_proof", {})["last_update_message_id"] = event["delivery_message_id"]
        elif event_type == "wait_reminder_sent":
            reminder_route_matches = visible_delivery_matches_completion_route(state, event.get("visible_delivery"))
            if reminder_route_matches:
                state["last_visible_update_at"] = event.get("event_at")
                state["last_wait_reminder_at"] = event.get("event_at")
                state["last_wait_reminder_delivery"] = canonical_visible_delivery_route(event.get("visible_delivery") or {})
            if reminder_route_matches and event.get("delivery_message_id"):
                state.setdefault("visible_delivery_proof", {})["last_wait_reminder_message_id"] = event["delivery_message_id"]
        elif event_type == "abandon" and current_status == "abandoned":
            state["abandon_reason"] = event.get("abandon_reason") or event.get("note")

        if event_type == "recovery_wake_delivered":
            state["last_recovery_packet_at"] = event.get("event_at")
            state["last_recovery_fingerprint"] = event.get("recovery_fingerprint")

    activity_candidates = [parse_time(state.get("last_progress_at"))]
    if visible_delivery_matches_completion_route(state, state.get("last_visible_update_delivery")):
        activity_candidates.append(parse_time(state.get("last_visible_update_at")))
    if visible_delivery_matches_completion_route(state, state.get("last_wait_reminder_delivery")):
        activity_candidates.append(parse_time(state.get("last_wait_reminder_at")))
    last_activity_ts = max(activity_candidates)
    if last_activity_ts:
        state["last_activity_at"] = datetime.fromtimestamp(last_activity_ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    state.setdefault("repeat_policy", DEFAULT_REPEAT_POLICY.get(state.get("side_effect_class", "read_only"), "reconcile_first"))
    state["status"] = current_status
    if state.get("stale_after_seconds") is None:
        state["stale_after_seconds"] = DEFAULT_THRESHOLDS_SECONDS.get(current_status)
    return state


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        with contextlib.suppress(OSError):
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_all_states_unlocked(root: Path) -> list[dict[str, Any]]:
    states = [derive_state(work_id, events) for work_id, events in grouped_events(root).items()]
    for state in states:
        atomic_write_json(state_file_path(root, state["work_id"]), state)
    atomic_write_json(ledger_dir(root) / "index.json", {"updated_at": now_iso(), "items": states})
    return states


def write_all_states(root: Path) -> list[dict[str, Any]]:
    with file_lock(root):
        return write_all_states_unlocked(root)


def terminal_prune_candidates(states: list[dict[str, Any]], *, days: int) -> tuple[set[str], list[dict[str, Any]], str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    prune_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []

    for state in states:
        work_id = state.get("work_id")
        if not isinstance(work_id, str) or not work_id:
            continue
        if state.get("status") not in TERMINAL_STATES:
            continue
        terminal_at = (
            parse_time(state.get("report_sent_at"))
            or parse_time((state.get("last_material_event") or {}).get("event_at"))
            or parse_time(state.get("updated_at"))
            or parse_time(state.get("created_at"))
        )
        if not terminal_at:
            continue
        terminal_dt = datetime.fromtimestamp(terminal_at, timezone.utc)
        if terminal_dt < cutoff:
            prune_ids.add(work_id)
            candidates.append({
                "work_id": work_id,
                "status": state.get("status"),
                "terminal_at": terminal_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "request_summary": state.get("request_summary"),
            })

    return prune_ids, candidates, cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def event_lines_excluding_work_ids(root: Path, prune_ids: set[str]) -> tuple[list[str], int]:
    path = events_path(root)
    if not path.exists():
        return [], 0
    kept_lines: list[str] = []
    removed = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                kept_lines.append(line)
                continue
            if event.get("work_id") in prune_ids:
                removed += 1
                continue
            kept_lines.append(line)
    return kept_lines, removed


def prune_terminal_work(root: Path, *, days: int = DEFAULT_TERMINAL_RETENTION_DAYS, apply: bool = False) -> dict[str, Any]:
    states = load_states(root)
    prune_ids, candidates, cutoff = terminal_prune_candidates(states, days=days)

    result = {
        "ok": True,
        "apply": apply,
        "days": days,
        "cutoff": cutoff,
        "prune_count": len(prune_ids),
        "candidates": candidates,
        "policy": "Only reported/abandoned terminal work older than the retention window is pruned; active work is never pruned.",
    }
    if not apply or not prune_ids:
        return result

    with file_lock(root):
        locked_states = [derive_state(work_id, events) for work_id, events in grouped_events(root).items()]
        prune_ids, candidates, cutoff = terminal_prune_candidates(locked_states, days=days)
        result["cutoff"] = cutoff
        result["prune_count"] = len(prune_ids)
        result["candidates"] = candidates
        if not prune_ids:
            result["events_removed"] = 0
            result["remaining_state_count"] = len(locked_states)
            return result

        kept_lines, removed = event_lines_excluding_work_ids(root, prune_ids)
        path = events_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for line in kept_lines:
                    fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        for work_id in prune_ids:
            with contextlib.suppress(FileNotFoundError):
                state_file_path(root, work_id).unlink()
        remaining = write_all_states_unlocked(root)
    result["events_removed"] = removed
    result["remaining_state_count"] = len(remaining)
    return result


def orphan_warnings_path(root: Path) -> Path:
    return ledger_dir(root) / "orphan_warnings.json"


def terminal_ref_handled_path(root: Path) -> Path:
    return ledger_dir(root) / "terminal_ref_handled.json"


def load_orphan_warnings(root: Path) -> dict[str, Any]:
    path = orphan_warnings_path(root)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_terminal_ref_handled(root: Path) -> dict[str, Any]:
    path = terminal_ref_handled_path(root)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def terminal_ref_fingerprint_payload(
    work_id: str,
    ref: str,
    terminal_status: str,
    *,
    task: dict[str, Any] | None = None,
) -> dict[str, str]:
    payload = {
        "work_id": work_id,
        "ref": ref,
        "terminal_status": terminal_status,
    }
    if task:
        for key in ("taskId", "runId", "endedAt", "terminalOutcome", "terminalSummary"):
            value = task.get(key)
            if value is not None:
                payload[key] = str(value)
    return payload


def terminal_ref_fingerprint(work_id: str, ref: str, terminal_status: str, *, task: dict[str, Any] | None = None) -> str:
    payload = terminal_ref_fingerprint_payload(work_id, ref, terminal_status, task=task)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def normalize_terminal_ref_fingerprints(primary: str, aliases: list[str] | None = None) -> list[str]:
    values = [primary]
    if aliases:
        values.extend(aliases)
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise SystemExit("invalid terminal ref fingerprint")
        if value not in normalized:
            normalized.append(value)
    return normalized


def terminal_ref_handled_is_valid(item: dict[str, Any]) -> bool:
    note = item.get("note")
    return (
        bool(item.get("handled_at"))
        and item.get("resolution") in TERMINAL_REF_HANDLED_RESOLUTIONS
        and isinstance(note, str)
        and bool(note.strip())
    )


def record_terminal_ref_handled(
    root: Path,
    *,
    work_id: str,
    ref: str,
    terminal_status: str,
    resolution: str,
    note: str | None = None,
    terminal_ref_fingerprints: list[str] | None = None,
) -> dict[str, Any]:
    if not WORK_ID_RE.fullmatch(work_id):
        raise SystemExit("invalid work_id")
    if not ref:
        raise SystemExit("--ref is required")
    if not terminal_status:
        raise SystemExit("--terminal-status is required")
    if resolution not in TERMINAL_REF_HANDLED_RESOLUTIONS:
        allowed = ", ".join(sorted(TERMINAL_REF_HANDLED_RESOLUTIONS))
        raise SystemExit(f"--resolution must be one of: {allowed}")
    if not note:
        raise SystemExit("--note is required")
    primary = terminal_ref_fingerprint(work_id, ref, terminal_status)
    fingerprints = normalize_terminal_ref_fingerprints(primary, terminal_ref_fingerprints)
    with file_lock(root):
        handled = load_terminal_ref_handled(root)
        item = {
            "handled_at": now_iso(),
            "terminal_ref_fingerprint": primary,
            "work_id": work_id,
            "ref": ref,
            "terminal_status": terminal_status,
            "resolution": resolution,
            "note": note,
        }
        if len(fingerprints) > 1:
            item["terminal_ref_fingerprints"] = fingerprints
        for fingerprint in fingerprints:
            handled[fingerprint] = dict(item, terminal_ref_fingerprint=fingerprint)
        atomic_write_json(terminal_ref_handled_path(root), handled)
    return item


def record_orphan_warning(
    root: Path,
    orphan_fingerprint: str,
    *,
    visible_delivery: dict[str, Any] | None = None,
    delivery_message_id: str | None = None,
    note: str | None = None,
    orphan_fingerprints: list[str] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", orphan_fingerprint):
        raise SystemExit("invalid orphan fingerprint")
    fingerprints = normalize_orphan_fingerprints(orphan_fingerprint, orphan_fingerprints)
    visible_delivery = validate_visible_delivery(visible_delivery, "--visible-delivery", required=True)
    if not delivery_message_id:
        raise SystemExit("--delivery-message-id is required")
    with file_lock(root):
        warnings = load_orphan_warnings(root)
        item = {
            "warned_at": now_iso(),
            "orphan_fingerprint": orphan_fingerprint,
            "visible_delivery": canonical_visible_delivery_route(visible_delivery),
        }
        if delivery_message_id:
            item["delivery_message_id"] = delivery_message_id
        if note:
            item["note"] = note
        if len(fingerprints) > 1:
            item["orphan_fingerprints"] = fingerprints
        for fingerprint in fingerprints:
            warnings[fingerprint] = dict(item, orphan_fingerprint=fingerprint)
        atomic_write_json(orphan_warnings_path(root), warnings)
    return item


def record_orphan_handled(
    root: Path,
    orphan_fingerprint: str,
    *,
    resolution: str,
    note: str | None = None,
    orphan_fingerprints: list[str] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", orphan_fingerprint):
        raise SystemExit("invalid orphan fingerprint")
    fingerprints = normalize_orphan_fingerprints(orphan_fingerprint, orphan_fingerprints)
    if resolution not in ORPHAN_HANDLED_RESOLUTIONS:
        allowed = ", ".join(sorted(ORPHAN_HANDLED_RESOLUTIONS))
        raise SystemExit(f"--resolution must be one of: {allowed}")
    if not note:
        raise SystemExit("--note is required")
    with file_lock(root):
        warnings = load_orphan_warnings(root)
        item = {
            "handled_at": now_iso(),
            "orphan_fingerprint": orphan_fingerprint,
            "resolution": resolution,
        }
        if note:
            item["note"] = note
        if len(fingerprints) > 1:
            item["orphan_fingerprints"] = fingerprints
        for fingerprint in fingerprints:
            warnings[fingerprint] = dict(item, orphan_fingerprint=fingerprint)
        atomic_write_json(orphan_warnings_path(root), warnings)
    return item


def normalize_orphan_fingerprints(orphan_fingerprint: str, aliases: list[str] | None = None) -> list[str]:
    values = [orphan_fingerprint]
    if aliases:
        values.extend(aliases)
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise SystemExit("invalid orphan fingerprint")
        if value not in normalized:
            normalized.append(value)
    return normalized


def orphan_warning_has_delivery_proof(warning: dict[str, Any]) -> bool:
    visible_delivery = warning.get("visible_delivery")
    if not isinstance(visible_delivery, dict):
        return False
    return bool(warning.get("delivery_message_id")) and bool(canonical_visible_delivery_route(visible_delivery))


def orphan_handled_is_valid(warning: dict[str, Any]) -> bool:
    note = warning.get("note")
    return (
        bool(warning.get("handled_at"))
        and warning.get("resolution") in ORPHAN_HANDLED_RESOLUTIONS
        and isinstance(note, str)
        and bool(note.strip())
    )


def load_states(root: Path) -> list[dict[str, Any]]:
    return write_all_states(root)


def recovery_fingerprint(state: dict[str, Any]) -> str:
    terminal_unreported_event = state.get("terminal_unreported_event") or {}
    use_terminal_anchor = state.get("status") in UNREPORTED_TERMINAL_STATES and terminal_unreported_event
    payload = {
        "work_id": state.get("work_id"),
        "status": state.get("status"),
        "last_material_event_sequence": terminal_unreported_event.get("sequence") if use_terminal_anchor else None,
        "next_recovery_action": (
            state.get("terminal_unreported_next_recovery_action") if use_terminal_anchor else state.get("next_recovery_action")
        ),
        "expected_outputs": state.get("expected_outputs"),
        "artifact_paths": state.get("artifact_paths"),
        "verification": state.get("verification"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def recovery_context_gaps(state: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    required_scalar_fields = (
        "work_id",
        "owner_session_key",
        "request_summary",
        "status",
        "side_effect_class",
        "repeat_policy",
        "next_recovery_action",
        "last_activity_at",
        "stale_after_seconds",
    )
    for field in required_scalar_fields:
        if state.get(field) in (None, "", []):
            gaps.append(field)
    visible_delivery = state.get("visible_delivery")
    if not isinstance(visible_delivery, dict) or not visible_delivery:
        gaps.append("visible_delivery")
    if not state.get("expected_outputs"):
        gaps.append("expected_outputs")
    has_recovery_anchor = (
        bool(state.get("artifact_paths"))
        or bool(state.get("openclaw_task_ids"))
        or bool(state.get("subagent_session_keys"))
        or bool(state.get("subagents"))
        or bool(state.get("no_artifact_expected"))
    )
    if not has_recovery_anchor:
        gaps.append("recovery_anchor")
    return gaps


def is_stale(state: dict[str, Any], now_ts: float, thresholds: dict[str, int]) -> tuple[bool, str]:
    status = state.get("status")
    if status in TERMINAL_STATES:
        return False, "terminal"
    if status in {"completed_unreported", "failed_unreported"}:
        return True, status
    threshold = state.get("stale_after_seconds") or thresholds.get(status)
    if threshold is None:
        return False, "no_threshold"
    minimum = MIN_STALE_AFTER_SECONDS.get(status)
    if minimum is not None:
        threshold = max(int(threshold), minimum)
    last_at = parse_time(state.get("last_activity_at") or state.get("last_progress_at") or state.get("updated_at") or state.get("created_at"))
    age = now_ts - last_at
    if age >= threshold:
        return True, f"{status}_stale_{int(age)}s"
    return False, f"fresh_{int(age)}s"


def make_recovery_packet(root: Path, state: dict[str, Any], reason: str) -> dict[str, Any]:
    status = state.get("status")
    context_gaps = recovery_context_gaps(state)
    if reason == "insufficient_recovery_context":
        recovery_instruction = (
            "This ledger entry lacks enough durable context for confident recovery. "
            "Do not guess the original request or repeat side effects. Inspect the ledger events, "
            "current artifacts/tasks/subagents; then either "
            "repair the ledger with explicit context, ask the user for the missing decision, or mark the work blocked/abandoned."
        )
    elif status == "waiting_user":
        recovery_instruction = (
            "This work is waiting for user input and may not be stuck. Reconcile current conversation "
            "state first. If the user has answered, continue the work, verify, send the visible completion "
            "report, then record complete-reported with the delivery id. If still blocked on user input, send at most one concise "
            "waiting reminder and record a fresh wait event instead of report-sent."
        )
    else:
        recovery_instruction = (
            "You are recovering unfinished work, not merely notifying. Read this ledger state, "
            "inspect current artifacts/tasks before acting, do not repeat external/destructive "
            "side effects without user approval, execute the next safe recovery action, verify, "
            "send one visible completion report, then record complete-reported with the delivery id."
        )
    visible_delivery_route = state.get("completion_visible_delivery") or canonical_visible_delivery_route(state.get("visible_delivery") or {})
    packet = {
        "schema_version": SCHEMA_VERSION,
        "packet_type": "work_recovery",
        "packet_created_at": now_iso(),
        "reason": reason,
        "recovery_fingerprint": recovery_fingerprint(state),
        "work_id": state.get("work_id"),
        "owner_session_key": state.get("owner_session_key"),
        "user_message_id": state.get("user_message_id"),
        "request_summary": state.get("request_summary"),
        "status": state.get("status"),
        "checklist": state.get("checklist", []),
        "success_criteria": state.get("success_criteria", []),
        "next_recovery_action": state.get("next_recovery_action"),
        "repeat_policy": state.get("repeat_policy"),
        "idempotency_key": state.get("idempotency_key"),
        "stale_after_seconds": state.get("stale_after_seconds"),
        "safe_to_repeat": state.get("repeat_policy") == "repeatable",
        "side_effect_class": state.get("side_effect_class"),
        "side_effects_performed": state.get("side_effects_performed", []),
        "external_actions_attempted": state.get("external_actions_attempted", []),
        "expected_outputs": state.get("expected_outputs", []),
        "artifact_paths": state.get("artifact_paths", []),
        "verification": state.get("verification", {}),
        "cwd": state.get("cwd"),
        "branch": state.get("branch"),
        "commit": state.get("commit"),
        "work_created_at": state.get("created_at"),
        "work_updated_at": state.get("updated_at"),
        "last_material_event": state.get("last_material_event"),
        "visible_delivery": visible_delivery_route,
        "visible_delivery_proof": state.get("visible_delivery_proof", {}),
        "user_message_timestamp": state.get("user_message_timestamp"),
        "last_visible_update_at": state.get("last_visible_update_at"),
        "last_wait_reminder_at": state.get("last_wait_reminder_at"),
        "ledger_paths": {
            "events": str(events_path(root)),
            "state_dir": str(state_dir(root)),
        },
        "subagents": state.get("subagents", []),
        "openclaw_task_ids": state.get("openclaw_task_ids", []),
        "subagent_session_keys": state.get("subagent_session_keys", []),
        "last_event": state.get("last_event"),
        "recovery_context_gaps": context_gaps,
        "required_recovery_instruction": recovery_instruction,
    }
    return packet


def scan_recoveries(root: Path, cooldown_seconds: int) -> list[dict[str, Any]]:
    states = load_states(root)
    now_ts = time.time()
    packets: list[dict[str, Any]] = []
    for state in states:
        stale, reason = is_stale(state, now_ts, DEFAULT_THRESHOLDS_SECONDS)
        if not stale:
            continue
        if recovery_context_gaps(state):
            reason = "insufficient_recovery_context"
        packet = make_recovery_packet(root, state, reason)
        last_fingerprint = state.get("last_recovery_fingerprint")
        last_wake_at = parse_time(state.get("last_recovery_packet_at"))
        same_packet = last_fingerprint == packet["recovery_fingerprint"]
        if same_packet and last_wake_at and now_ts - last_wake_at < cooldown_seconds:
            continue
        packets.append(packet)
    return packets


def normalized_task_identity_values(value: str) -> set[str]:
    values = {value}
    if value.startswith("codex-thread:"):
        values.add(value.split(":", 1)[1])
    elif re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
        values.add(f"codex-thread:{value}")
    return values


def is_bare_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value))


def task_identity_values(task: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("taskId", "runId", "childSessionKey", "sessionKey", "agent_id", "agentId"):
        value = task.get(key)
        if isinstance(value, str) and value:
            values.update(normalized_task_identity_values(value))
    return values


def task_specificity_score(task: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        1 if task.get("runtime") == "subagent" else 0,
        1 if task.get("childSessionKey") or task.get("sessionKey") else 0,
        1 if task.get("label") else 0,
        len(task_identity_values(task)),
    )


def task_age_seconds(task: dict[str, Any], now_ms: int) -> int | None:
    candidates = [
        task.get("startedAt"),
        task.get("createdAt"),
        task.get("lastEventAt"),
    ]
    timestamps = [value for value in candidates if isinstance(value, (int, float)) and value > 0]
    if not timestamps:
        return None
    return max(0, int((now_ms - min(timestamps)) / 1000))


def task_idle_seconds(task: dict[str, Any], now_ms: int) -> int | None:
    for key in ("lastEventAt", "startedAt", "createdAt"):
        value = task.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return max(0, int((now_ms - value) / 1000))
    return None


def orphan_identity_payload(task: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("runId", "childSessionKey", "sessionKey", "taskId"):
        value = task.get(key)
        if isinstance(value, str) and value:
            return {"key": key, "value": value}
    return None


def orphan_identity_payloads(task: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key in ("runId", "childSessionKey", "sessionKey", "taskId"):
        value = task.get(key)
        if not isinstance(value, str) or not value:
            continue
        item = (key, value)
        if item in seen:
            continue
        seen.add(item)
        payloads.append({"key": key, "value": value})
    return payloads


def orphan_identity_fingerprints(task: dict[str, Any]) -> list[str]:
    return [
        hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        for payload in orphan_identity_payloads(task)
    ]


def orphan_identity_fingerprint(task: dict[str, Any]) -> str | None:
    fingerprints = orphan_identity_fingerprints(task)
    if not fingerprints:
        return None
    return fingerprints[0]


def collect_active_task_refs(root: Path) -> set[str]:
    refs: set[str] = set()
    for state in load_states(root):
        if state.get("status") not in ACTIVE_STATES:
            continue
        for key in ("openclaw_task_ids", "subagent_session_keys"):
            for value in state.get(key, []):
                if isinstance(value, str) and value:
                    refs.update(normalized_task_identity_values(value))
        for subagent in state.get("subagents", []):
            if not isinstance(subagent, dict):
                continue
            for key in ("id", "taskId", "runId", "sessionKey", "childSessionKey", "agent_id", "agentId"):
                value = subagent.get(key)
                if isinstance(value, str) and value:
                    refs.update(normalized_task_identity_values(value))
    return refs


def collect_active_task_ref_details(root: Path) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for state in load_states(root):
        if state.get("status") not in ACTIVE_STATES:
            continue
        for key in ("openclaw_task_ids", "subagent_session_keys"):
            for value in state.get(key, []):
                if not isinstance(value, str) or not value:
                    continue
                for normalized in normalized_task_identity_values(value):
                    marker = (str(state.get("work_id") or ""), normalized)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    refs.append({"work_id": state.get("work_id"), "status": state.get("status"), "ref": normalized, "raw_ref": value, "source": key})
        for subagent in state.get("subagents", []):
            if not isinstance(subagent, dict):
                continue
            for key in ("id", "taskId", "runId", "sessionKey", "childSessionKey", "agent_id", "agentId"):
                value = subagent.get(key)
                if not isinstance(value, str) or not value:
                    continue
                for normalized in normalized_task_identity_values(value):
                    marker = (str(state.get("work_id") or ""), normalized)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    refs.append({
                        "work_id": state.get("work_id"),
                        "status": state.get("status"),
                        "ref": normalized,
                        "raw_ref": value,
                        "source": f"subagents.{key}",
                        "subagent": {k: v for k, v in subagent.items() if k in {"id", "role", "taskId", "runId", "sessionKey", "childSessionKey", "agent_id", "agentId"}},
                    })
    return refs


def load_openclaw_task_lookup(lookup: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        proc = subprocess.run(
            ["openclaw", "tasks", "show", "--json", lookup],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or f"openclaw tasks show exited {proc.returncode}").strip()
        lowered = text.lower()
        if "not found" in lowered or "no task" in lowered:
            return {"status": "notFound", "lookup": lookup}, None
        return None, text
    output = proc.stdout.strip() or proc.stderr.strip()
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        return None, f"invalid task JSON for {lookup}: {exc}"
    if isinstance(parsed, dict) and isinstance(parsed.get("task"), dict):
        return parsed["task"], None
    if isinstance(parsed, dict):
        return parsed, None
    return None, f"unexpected task JSON for {lookup}"


def normalized_openclaw_task_status(task: dict[str, Any]) -> str | None:
    status = task.get("status")
    if isinstance(status, str):
        return status
    if isinstance(status, dict):
        for key in ("completed", "errored"):
            if key in status:
                return key
    return None


def find_referenced_terminal_tasks(root: Path) -> dict[str, Any]:
    terminal_statuses = {
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "lost",
        "shutdown",
        "notFound",
        "not_found",
        "completed",
        "errored",
    }
    terminal_refs: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    errors: list[str] = []
    lookup_results: dict[str, tuple[dict[str, Any] | None, str | None]] = {}
    handled = load_terminal_ref_handled(root)
    for ref in collect_active_task_ref_details(root):
        lookup = ref.get("ref")
        if not isinstance(lookup, str) or not lookup:
            continue
        raw_ref = ref.get("raw_ref")
        source = str(ref.get("source") or "")
        if lookup.startswith("codex-thread:") or (isinstance(raw_ref, str) and raw_ref.startswith("codex-thread:")):
            continue
        if (
            source in {"subagents.id", "subagents.agent_id", "subagents.agentId", "subagent_session_keys"}
            and is_bare_uuid(raw_ref)
        ):
            continue
        if lookup not in lookup_results:
            lookup_results[lookup] = load_openclaw_task_lookup(lookup)
        task, error = lookup_results[lookup]
        if error:
            errors.append(f"{lookup}: {error}")
            continue
        if not task:
            continue
        status = normalized_openclaw_task_status(task)
        if status in terminal_statuses:
            item = {
                **ref,
                "taskId": task.get("taskId"),
                "runId": task.get("runId"),
                "runtime": task.get("runtime"),
                "task_status": status,
                "label": task.get("label"),
                "terminalSummary": task.get("terminalSummary"),
                "terminalOutcome": task.get("terminalOutcome"),
                "progressSummary": task.get("progressSummary"),
                "endedAt": task.get("endedAt"),
            }
            work_id = str(item.get("work_id") or "")
            ref_value = str(item.get("ref") or "")
            fingerprint = terminal_ref_fingerprint(work_id, ref_value, status, task=task)
            legacy_fingerprint = terminal_ref_fingerprint(work_id, ref_value, status)
            item["terminal_ref_fingerprint"] = fingerprint
            item["terminal_ref_fingerprints"] = normalize_terminal_ref_fingerprints(fingerprint, [legacy_fingerprint])
            handled_item = next(
                (
                    handled.get(alias)
                    for alias in item["terminal_ref_fingerprints"]
                    if isinstance(handled.get(alias), dict) and terminal_ref_handled_is_valid(handled.get(alias, {}))
                ),
                None,
            )
            if isinstance(handled_item, dict):
                ignored.append({**item, "reason": "handled", "handled": handled_item})
                continue
            terminal_refs.append(item)
    return {
        "ok": not errors,
        "has_terminal_refs": bool(terminal_refs),
        "terminal_refs": terminal_refs,
        "ignored": ignored,
        "errors": errors,
        "checked_refs": len(lookup_results),
        "policy": "terminal referenced tasks require LLM reconciliation only; do not restart or repeat side effects from this signal alone",
    }


def load_openclaw_tasks(status: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        proc = subprocess.run(
            ["openclaw", "tasks", "list", "--json", "--status", status],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout or f"openclaw tasks list exited {proc.returncode}").strip()
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return [], f"invalid tasks JSON: {exc}"
    tasks = parsed.get("tasks", [])
    if not isinstance(tasks, list):
        return [], "tasks JSON did not contain a tasks array"
    return [task for task in tasks if isinstance(task, dict)], None


def find_orphan_active_work(root: Path, *, include_cron: bool = False, min_age_seconds: int = DEFAULT_ORPHAN_MIN_AGE_SECONDS) -> dict[str, Any]:
    ledger_refs = collect_active_task_refs(root)
    now_ms = int(time.time() * 1000)
    now_ts = time.time()
    warned = load_orphan_warnings(root)
    statuses = ("queued", "running")
    tasks: list[dict[str, Any]] = []
    errors: list[str] = []
    for status in statuses:
        status_tasks, error = load_openclaw_tasks(status)
        tasks.extend(status_tasks)
        if error:
            errors.append(f"{status}: {error}")

    orphans: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    referenced_task_identities: set[str] = set()
    for task in tasks:
        identities = task_identity_values(task)
        if identities & ledger_refs:
            referenced_task_identities.update(identities)
    seen_identities: set[str] = set()
    tasks = sorted(tasks, key=task_specificity_score, reverse=True)
    for task in tasks:
        identities = task_identity_values(task)
        if identities & referenced_task_identities:
            continue
        if identities and identities & seen_identities:
            continue
        seen_identities.update(identities)
        is_watchdog = task.get("runtime") == "cron" and task.get("label") == "work-ledger-watchdog-v1"
        if task.get("runtime") == "cron" and not include_cron:
            ignored.append({"taskId": task.get("taskId"), "runtime": task.get("runtime"), "label": task.get("label"), "reason": "cron"})
            continue
        if is_watchdog:
            ignored.append({"taskId": task.get("taskId"), "runtime": task.get("runtime"), "label": task.get("label"), "reason": "self"})
            continue
        age_seconds = task_age_seconds(task, now_ms)
        idle_seconds = task_idle_seconds(task, now_ms)
        freshness_seconds = idle_seconds if idle_seconds is not None else age_seconds
        if freshness_seconds is not None and freshness_seconds < min_age_seconds:
            ignored.append({
                "taskId": task.get("taskId"),
                "runId": task.get("runId"),
                "runtime": task.get("runtime"),
                "label": task.get("label"),
                "reason": "fresh",
                "age_seconds": age_seconds,
                "idle_seconds": idle_seconds,
                "min_age_seconds": min_age_seconds,
            })
            continue
        fingerprint = orphan_identity_fingerprint(task)
        fingerprints = orphan_identity_fingerprints(task)
        if not fingerprint:
            orphans.append({
                "taskId": task.get("taskId"),
                "runId": task.get("runId"),
                "runtime": task.get("runtime"),
                "status": task.get("status"),
                "label": task.get("label"),
                "task": task.get("task"),
                "ownerKey": task.get("ownerKey"),
                "childSessionKey": task.get("childSessionKey"),
                "createdAt": task.get("createdAt"),
                "startedAt": task.get("startedAt"),
                "lastEventAt": task.get("lastEventAt"),
                "age_seconds": age_seconds,
                "idle_seconds": idle_seconds,
                "suppression_supported": False,
                "reason": "unfingerprintable",
            })
            continue
        warning = next((warned.get(alias) for alias in fingerprints if isinstance(warned.get(alias), dict)), None)
        suppress_ts = 0.0
        suppress_reason = "warned"
        suppress_at = None
        if isinstance(warning, dict):
            has_warning_proof = orphan_warning_has_delivery_proof(warning)
            if orphan_handled_is_valid(warning) and not warning.get("warned_at"):
                suppress_reason = "handled"
                suppress_at = warning.get("handled_at")
            elif has_warning_proof:
                suppress_at = warning.get("warned_at")
            suppress_ts = parse_time(suppress_at)
        if suppress_reason == "handled" and suppress_ts:
            ignored.append({
                "taskId": task.get("taskId"),
                "runId": task.get("runId"),
                "runtime": task.get("runtime"),
                "label": task.get("label"),
                "reason": suppress_reason,
                "orphan_fingerprint": fingerprint,
                "orphan_fingerprints": fingerprints,
                "handled_at": warning.get("handled_at") if isinstance(warning, dict) else None,
                "resolution": warning.get("resolution") if isinstance(warning, dict) else None,
                "suppress_seconds": None,
            })
            continue
        if suppress_ts and now_ts - suppress_ts < DEFAULT_ORPHAN_WARNING_SUPPRESSION_SECONDS:
            ignored.append({
                "taskId": task.get("taskId"),
                "runId": task.get("runId"),
                "runtime": task.get("runtime"),
                "label": task.get("label"),
                "reason": suppress_reason,
                "orphan_fingerprint": fingerprint,
                "orphan_fingerprints": fingerprints,
                "warned_at": warning.get("warned_at") if isinstance(warning, dict) else None,
                "handled_at": warning.get("handled_at") if isinstance(warning, dict) else None,
                "resolution": warning.get("resolution") if isinstance(warning, dict) else None,
                "suppress_seconds": DEFAULT_ORPHAN_WARNING_SUPPRESSION_SECONDS,
            })
            continue
        orphans.append({
            "taskId": task.get("taskId"),
            "runId": task.get("runId"),
            "runtime": task.get("runtime"),
            "status": task.get("status"),
            "label": task.get("label"),
            "task": task.get("task"),
            "ownerKey": task.get("ownerKey"),
            "childSessionKey": task.get("childSessionKey"),
            "createdAt": task.get("createdAt"),
            "startedAt": task.get("startedAt"),
            "lastEventAt": task.get("lastEventAt"),
            "age_seconds": age_seconds,
            "idle_seconds": idle_seconds,
            "orphan_fingerprint": fingerprint,
            "orphan_fingerprints": fingerprints,
        })

    return {
        "ok": not errors,
        "has_orphans": bool(orphans),
        "orphans": orphans,
        "ignored": ignored[:20],
        "errors": errors,
        "min_age_seconds": min_age_seconds,
        "policy": "refresh_before_user_message; ignores fresh tasks by default; handled orphans are durably suppressed; visible warnings require delivery proof and suppress repeats for 24h; do not auto-create ledger entries or recover from this output alone",
    }


def watchdog_check(
    root: Path,
    *,
    include_cron: bool = False,
    min_age_seconds: int = DEFAULT_ORPHAN_MIN_AGE_SECONDS,
    cooldown_seconds: int = 30 * 60,
) -> dict[str, Any]:
    terminal_refs = find_referenced_terminal_tasks(root)
    if not terminal_refs.get("ok", False):
        return {
            "ok": False,
            "status": "error",
            "needs_wake": True,
            "wake_reason": "runner_error",
            "recoveries": [],
            "terminal_refs": terminal_refs,
            "orphans": None,
            "errors": terminal_refs.get("errors", []),
            "policy": "LLM should inspect runner errors before user-visible output; do not retry risky side effects",
        }
    if terminal_refs.get("has_terminal_refs"):
        return {
            "ok": True,
            "status": "needs_wake",
            "needs_wake": True,
            "wake_reason": "referenced_task_reconciliation",
            "recoveries": [],
            "terminal_refs": terminal_refs,
            "orphans": None,
            "policy": "LLM must reconcile referenced terminal tasks by integrating results or reporting failure; do not restart or repeat side effects from this signal alone",
        }

    recoveries = scan_recoveries(root, cooldown_seconds)
    if recoveries:
        return {
            "ok": True,
            "status": "needs_wake",
            "needs_wake": True,
            "wake_reason": "recovery",
            "recoveries": recoveries,
            "terminal_refs": terminal_refs,
            "orphans": None,
            "policy": "LLM must reconcile recovery packet before visible report; do not retry risky side effects from watchdog output alone",
        }

    orphan_result = find_orphan_active_work(root, include_cron=include_cron, min_age_seconds=min_age_seconds)
    if not orphan_result.get("ok", False):
        return {
            "ok": False,
            "status": "error",
            "needs_wake": True,
            "wake_reason": "runner_error",
            "recoveries": [],
            "orphans": orphan_result,
            "errors": orphan_result.get("errors", []),
            "policy": "LLM should inspect runner errors before user-visible output; do not retry risky side effects",
        }
    if orphan_result.get("has_orphans"):
        return {
            "ok": True,
            "status": "needs_wake",
            "needs_wake": True,
            "wake_reason": "orphan_reconciliation",
            "recoveries": [],
            "orphans": orphan_result,
            "policy": "LLM must refresh/reconcile orphans before any user-visible warning; at most one aggregated result message",
        }
    handled_orphans = [
        item
        for item in orphan_result.get("ignored", [])
        if isinstance(item, dict) and item.get("reason") == "handled"
    ]
    return {
        "ok": True,
        "status": "clean",
        "needs_wake": False,
        "wake_reason": None,
        "recoveries": [],
        "terminal_refs": terminal_refs,
        "orphans": orphan_result,
        "handled_orphans": handled_orphans,
        "policy": "clean/no-op path; no visible user message needed",
    }


QUICK_START_PRESETS: dict[str, dict[str, Any]] = {
    "coding": {
        "side_effect_class": "repo_changes",
        "checklist": ["inspect current state", "make scoped code changes", "run verification", "send visible completion report"],
        "success_criteria": ["requested code change is complete", "verification result is recorded", "visible report is sent"],
        "stale_after_seconds": 30 * 60,
        "repeat_policy": "reconcile_first",
    },
    "local-files": {
        "side_effect_class": "local_files",
        "checklist": ["inspect current files", "make scoped file changes", "verify result", "send visible completion report"],
        "success_criteria": ["requested file work is complete", "verification result is recorded", "visible report is sent"],
        "stale_after_seconds": 30 * 60,
        "repeat_policy": "reconcile_first",
    },
    "subagent": {
        "side_effect_class": "read_only",
        "checklist": ["start or observe subagent work", "record wait state", "integrate result", "send visible completion report"],
        "success_criteria": ["subagent result is reconciled", "visible report is sent"],
        "stale_after_seconds": 60 * 60,
        "repeat_policy": "reconcile_first",
    },
    "browser": {
        "side_effect_class": "local_files",
        "checklist": ["open browser only as needed", "close browser tab", "verify result", "send visible completion report"],
        "success_criteria": ["browser work is complete and closed", "visible report is sent"],
        "stale_after_seconds": 20 * 60,
        "repeat_policy": "reconcile_first",
    },
    "cron-gateway": {
        "side_effect_class": "gateway_runtime",
        "checklist": ["inspect cron/gateway state", "make approved scoped change", "verify health/state", "send visible completion report"],
        "success_criteria": ["cron/gateway state is verified", "visible report is sent"],
        "stale_after_seconds": 20 * 60,
        "repeat_policy": "never_repeat_without_user_approval",
    },
    "external": {
        "side_effect_class": "external_message",
        "checklist": ["confirm durable clearance", "perform external action once", "verify delivery/result", "send visible completion report"],
        "success_criteria": ["external action is reconciled", "visible report is sent"],
        "stale_after_seconds": 20 * 60,
        "repeat_policy": "never_repeat_without_user_approval",
    },
    "long-readonly": {
        "side_effect_class": "read_only",
        "checklist": ["inspect sources/state", "complete read-only analysis", "verify facts", "send visible completion report"],
        "success_criteria": ["analysis is complete", "visible report is sent"],
        "stale_after_seconds": 30 * 60,
        "repeat_policy": "repeatable",
    },
}


def slugify_work_id(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip().lower()).strip("-._:")
    if not slug:
        slug = "work"
    return slug[:72]


def command_quick_start(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    preset = QUICK_START_PRESETS[args.kind]
    work_id = args.work_id
    if not work_id:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        work_id = f"{args.kind}-{slugify_work_id(args.summary)}-{stamp}"
    validate_work_id(work_id)
    validate_owner_session_key(args.owner_session_key)
    visible_delivery = validate_visible_delivery(
        load_json_object_arg(args.visible_delivery, "--visible-delivery", required=True),
        "--visible-delivery",
        required=True,
        require_channel_target=True,
    )
    side_effect_class = args.side_effect_class or preset["side_effect_class"]
    repeat_policy = args.repeat_policy or preset["repeat_policy"]
    idempotency_key = args.idempotency_key
    if side_effect_class in {"external_message", "public_post", "destructive", "gateway_runtime"} and not idempotency_key:
        idempotency_key = hashlib.sha256(f"{work_id}:{side_effect_class}".encode("utf-8")).hexdigest()[:24]
    event_args = argparse.Namespace(
        work_id=work_id,
        note=args.note or f"quick-start preset: {args.kind}",
        next_recovery_action=args.next_recovery_action or "Reconcile current state, execute only the next safe action, verify, send visible report, then record complete-reported with the delivery id.",
        expected_outputs=args.expected_outputs,
        artifact_paths=args.artifact_paths,
        openclaw_task_ids=args.openclaw_task_ids,
        subagent_session_keys=args.subagent_session_keys,
        subagents=args.subagents,
        verification=None,
        side_effects_performed=None,
        external_actions_attempted=None,
        request_summary=args.summary,
        owner_session_key=args.owner_session_key,
        user_message_id=args.user_message_id,
        user_message_timestamp=args.user_message_timestamp,
        checklist=json.dumps(args.checklist or preset["checklist"]),
        success_criteria=json.dumps(args.success_criteria or preset["success_criteria"]),
        side_effect_class=side_effect_class,
        cwd=args.cwd or str(DEFAULT_ROOT),
        branch=args.branch,
        commit=args.commit,
        idempotency_key=idempotency_key,
        repeat_policy=repeat_policy,
        stale_after_seconds=args.stale_after_seconds or preset["stale_after_seconds"],
        no_artifact_expected=args.no_artifact_expected,
        resume_start=args.resume_start,
        visible_delivery=json.dumps(visible_delivery),
    )
    event = command_event(root, event_args, "start")
    return {"ok": True, "work_id": work_id, "kind": args.kind, "event": event}


def validate_start(args: argparse.Namespace) -> None:
    validate_work_id(args.work_id)
    if args.side_effect_class not in SIDE_EFFECT_CLASSES:
        raise SystemExit(f"invalid side effect class: {args.side_effect_class}")
    if args.repeat_policy is None:
        args.repeat_policy = DEFAULT_REPEAT_POLICY[args.side_effect_class]
    if not args.request_summary:
        raise SystemExit("--request-summary is required")
    validate_owner_session_key(args.owner_session_key)
    validate_visible_delivery(
        load_json_object_arg(args.visible_delivery, "--visible-delivery", required=True),
        "--visible-delivery",
        required=True,
        require_channel_target=True,
    )
    load_json_string_list_arg(args.checklist, "--checklist")
    load_json_string_list_arg(args.success_criteria, "--success-criteria")
    if args.side_effect_class in {"external_message", "public_post", "destructive", "gateway_runtime"} and not args.idempotency_key:
        raise SystemExit("--idempotency-key is required for external/destructive/gateway side effects")
    if args.repeat_policy not in REPEAT_POLICIES:
        raise SystemExit(f"invalid repeat policy: {args.repeat_policy}")
    validate_stale_after_seconds(getattr(args, "stale_after_seconds", None))


def command_event(root: Path, args: argparse.Namespace, event_type: str) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise SystemExit(f"invalid event type: {event_type}")
    validate_work_id(args.work_id)
    reported_visible_delivery: dict[str, Any] | None = None
    if event_type == "start":
        validate_start(args)
    if event_type == "report_sent":
        reported_visible_delivery = validate_visible_delivery(
            load_json_object_arg(getattr(args, "visible_delivery", None), "--visible-delivery", required=True),
            "--visible-delivery",
            required=True,
            require_channel_target=True,
        )
        if not getattr(args, "delivery_message_id", None):
            raise SystemExit("--delivery-message-id is required for report-sent")
    if event_type in {"visible_update_sent", "wait_reminder_sent"}:
        if not validate_visible_delivery(
            load_json_object_arg(getattr(args, "visible_delivery", None), "--visible-delivery", required=True),
            "--visible-delivery",
            required=True,
        ):
            raise SystemExit("--visible-delivery is required")
        if not getattr(args, "delivery_message_id", None):
            raise SystemExit(f"--delivery-message-id is required for {event_type.replace('_', '-')}")
    event: dict[str, Any] = {
        "event_type": event_type,
        "work_id": args.work_id,
        "note": getattr(args, "note", None),
        "request_summary": getattr(args, "request_summary", None),
        "owner_session_key": getattr(args, "owner_session_key", None),
        "user_message_id": getattr(args, "user_message_id", None),
        "user_message_timestamp": getattr(args, "user_message_timestamp", None),
        "cwd": getattr(args, "cwd", None),
        "branch": getattr(args, "branch", None),
        "commit": getattr(args, "commit", None),
        "side_effect_class": getattr(args, "side_effect_class", None),
        "idempotency_key": getattr(args, "idempotency_key", None),
        "repeat_policy": getattr(args, "repeat_policy", None),
        "stale_after_seconds": validate_stale_after_seconds(getattr(args, "stale_after_seconds", None)),
        "no_artifact_expected": getattr(args, "no_artifact_expected", None),
        "next_recovery_action": getattr(args, "next_recovery_action", None),
        "status": getattr(args, "status", None),
        "failure_reason": getattr(args, "failure_reason", None),
        "delivery_message_id": getattr(args, "delivery_message_id", None),
        "checklist": load_json_arg(getattr(args, "checklist", None), None),
        "success_criteria": load_json_arg(getattr(args, "success_criteria", None), None),
        "verification": load_json_arg(getattr(args, "verification", None), None),
        "visible_delivery": reported_visible_delivery if event_type == "report_sent" else validate_visible_delivery(
            load_json_object_arg(getattr(args, "visible_delivery", None), "--visible-delivery"),
            "--visible-delivery",
        ),
        "expected_outputs": split_csv(getattr(args, "expected_outputs", None)),
        "artifact_paths": split_csv(getattr(args, "artifact_paths", None)),
        "openclaw_task_ids": split_csv(getattr(args, "openclaw_task_ids", None)),
        "subagent_session_keys": split_csv(getattr(args, "subagent_session_keys", None)),
        "side_effects_performed": split_csv(getattr(args, "side_effects_performed", None)),
        "external_actions_attempted": split_csv(getattr(args, "external_actions_attempted", None)),
    }
    subagents = load_json_arg(getattr(args, "subagents", None), None)
    if subagents is not None:
        event["subagents"] = subagents
    event = {key: value for key, value in event.items() if value not in (None, [], {})}
    if event_type == "start":
        with file_lock(root):
            existing = grouped_events(root).get(args.work_id)
            if existing:
                existing_state = derive_state(args.work_id, existing)
                if existing_state.get("status") in UNREPORTED_TERMINAL_STATES:
                    raise SystemExit("resume-start is not allowed after complete/fail leaves work unreported")
                if existing_state.get("status") not in {"reported", "abandoned"} and not getattr(args, "resume_start", False):
                    raise SystemExit(f"active work_id already exists: {args.work_id}")
            return append_event_unlocked(root, event)
    with file_lock(root):
        existing_events = grouped_events(root).get(args.work_id)
        if not existing_events:
            raise SystemExit(f"unknown work_id: {args.work_id}")
        if event_type == "report_sent":
            existing_state = derive_state(args.work_id, existing_events)
            if existing_state.get("status") not in {"completed_unreported", "failed_unreported"}:
                raise SystemExit("report-sent is allowed only after complete or fail leaves work unreported")
            assert_visible_delivery_matches_existing(existing_state, event.get("visible_delivery") or {})
        return append_event_unlocked(root, event)


def command_complete_reported(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    validate_work_id(args.work_id)
    reported_visible_delivery = validate_visible_delivery(
        load_json_object_arg(args.visible_delivery, "--visible-delivery", required=True),
        "--visible-delivery",
        required=True,
        require_channel_target=True,
    )
    if not args.delivery_message_id:
        raise SystemExit("--delivery-message-id is required for complete-reported")
    complete_event: dict[str, Any] = {
        "event_type": "complete",
        "work_id": args.work_id,
        "note": args.note,
        "next_recovery_action": args.next_recovery_action,
        "verification": load_json_arg(args.verification, None),
        "expected_outputs": split_csv(args.expected_outputs),
        "artifact_paths": split_csv(args.artifact_paths),
        "openclaw_task_ids": split_csv(args.openclaw_task_ids),
        "subagent_session_keys": split_csv(args.subagent_session_keys),
        "side_effects_performed": split_csv(args.side_effects_performed),
        "external_actions_attempted": split_csv(args.external_actions_attempted),
    }
    subagents = load_json_arg(args.subagents, None)
    if subagents is not None:
        complete_event["subagents"] = subagents
    complete_event = {key: value for key, value in complete_event.items() if value not in (None, [], {})}
    with file_lock(root):
        existing_events = grouped_events(root).get(args.work_id)
        if not existing_events:
            raise SystemExit(f"unknown work_id: {args.work_id}")
        existing_state = derive_state(args.work_id, existing_events)
        assert_visible_delivery_matches_existing(existing_state, reported_visible_delivery or {})
        events_to_append: list[dict[str, Any]] = []
        if existing_state.get("status") in ACTIVE_STATES:
            events_to_append.append(complete_event)
        elif existing_state.get("status") in UNREPORTED_TERMINAL_STATES:
            pass
        else:
            raise SystemExit("complete-reported is allowed only for active or unreported completed/failed work")
        events_to_append.append({
            "event_type": "report_sent",
            "work_id": args.work_id,
            "note": args.report_note or "Visible completion report sent and proof recorded by complete-reported.",
            "visible_delivery": reported_visible_delivery,
            "delivery_message_id": args.delivery_message_id,
        })
        appended = append_events_unlocked(root, events_to_append)
    completed_event = appended[0] if len(appended) == 2 else None
    report_event = appended[-1]
    return {"completed_event": completed_event, "report_event": report_event}


def command_hook_observe(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    validate_work_id(args.work_id)
    payload = load_json_object_arg(args.payload, "--payload", required=True)
    observation = ledger_observation(payload)
    fingerprint = observation["fingerprint"]
    with file_lock(root):
        existing = grouped_events(root).get(args.work_id)
        if not existing:
            raise SystemExit(f"unknown work_id: {args.work_id}")
        state = derive_state(args.work_id, existing)
        if fingerprint in state.get("hook_fingerprints", []):
            return {"ok": True, "duplicate": True, "observation": observation}
        pending_event = pending_completion_report_send_event(args.work_id, payload, state, observation)
        if pending_event:
            return {
                "ok": True,
                "duplicate": False,
                "event": append_event_unlocked(root, pending_event),
                "observation": observation,
                "recorded_completion_report_send": True,
            }
        report_event = report_sent_event_from_message_sent(args.work_id, payload, state, observation)
        if report_event:
            assert_visible_delivery_matches_existing(state, report_event.get("visible_delivery") or {})
            return {
                "ok": True,
                "duplicate": False,
                "event": append_event_unlocked(root, report_event),
                "observation": observation,
                "recorded_report_sent": True,
            }
        event: dict[str, Any] = {
            "event_type": "hook_observed",
            "work_id": args.work_id,
            "note": args.note or f"hook observed: {observation['event']}",
            "hook_observations": [observation],
            "hook_fingerprints": [fingerprint],
        }
        if args.next_recovery_action:
            event["hook_candidate_next_recovery_action"] = args.next_recovery_action
        if observation.get("candidate_event_type") == "verify":
            event["verification"] = {
                "hook_candidate": observation.get("tool_name") or observation.get("event"),
                "hook_fingerprint": fingerprint,
            }
        return {
            "ok": True,
            "duplicate": False,
            "event": append_event_unlocked(root, {key: value for key, value in event.items() if value not in (None, [], {})}),
            "observation": observation,
        }


def command_hook_guardrail(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    validate_work_id(args.work_id)
    payload = load_json_object_arg(args.payload, "--payload", required=True)
    events = grouped_events(root).get(args.work_id)
    if not events:
        raise SystemExit(f"unknown work_id: {args.work_id}")
    state = derive_state(args.work_id, events)
    return {"ok": True, "decision": ledger_guardrail(payload, state), "state_status": state.get("status")}


def add_common_event_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--note")
    parser.add_argument("--next-recovery-action")
    parser.add_argument("--expected-outputs", help="Comma-separated paths")
    parser.add_argument("--artifact-paths", help="Comma-separated paths")
    parser.add_argument("--openclaw-task-ids", help="Comma-separated ids")
    parser.add_argument("--subagent-session-keys", help="Comma-separated session keys")
    parser.add_argument("--subagents", help="JSON array")
    parser.add_argument("--verification", help="JSON object")
    parser.add_argument("--side-effects-performed", help="Comma-separated descriptions")
    parser.add_argument("--external-actions-attempted", help="Comma-separated descriptions")


def build_parser() -> argparse.ArgumentParser:
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument(
        "--json",
        action="store_true",
        help="Output JSON. Accepted for wrapper compatibility; ledger commands already emit JSON.",
    )

    parser = argparse.ArgumentParser(
        description="Workspace-local recoverable work ledger",
        parents=[json_parent],
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Workspace root")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_command(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        parents = list(kwargs.pop("parents", []))
        parents.append(json_parent)
        return sub.add_parser(name, parents=parents, **kwargs)

    start = add_command("start")
    add_common_event_args(start)
    start.add_argument("--request-summary", required=True)
    start.add_argument("--owner-session-key")
    start.add_argument("--user-message-id")
    start.add_argument("--user-message-timestamp")
    start.add_argument("--checklist", required=True, help="JSON array")
    start.add_argument("--success-criteria", required=True, help="JSON array")
    start.add_argument("--side-effect-class", default="local_files", choices=sorted(SIDE_EFFECT_CLASSES))
    start.add_argument("--cwd", default=str(DEFAULT_ROOT))
    start.add_argument("--branch")
    start.add_argument("--commit")
    start.add_argument("--idempotency-key")
    start.add_argument("--repeat-policy", choices=sorted(REPEAT_POLICIES), default=None)
    start.add_argument("--stale-after-seconds", type=int, help="Per-entry stale threshold for active/waiting/verifying states")
    start.add_argument("--no-artifact-expected", action="store_true", help="Declare that this work has no file/task/subagent recovery anchor by design")
    start.add_argument("--resume-start", action="store_true", help="Allow another start event for an active work_id")
    start.add_argument("--visible-delivery")

    quick = add_command("quick-start", help="Start a selective ledger entry from a small preset; not for every short request")
    quick.add_argument("--kind", required=True, choices=sorted(QUICK_START_PRESETS))
    quick.add_argument("--summary", required=True)
    quick.add_argument("--work-id")
    quick.add_argument("--owner-session-key", required=True)
    quick.add_argument("--visible-delivery", required=True)
    quick.add_argument("--user-message-id")
    quick.add_argument("--user-message-timestamp")
    quick.add_argument("--expected-outputs", help="Comma-separated paths or result labels")
    quick.add_argument("--artifact-paths", help="Comma-separated paths")
    quick.add_argument("--openclaw-task-ids", help="Comma-separated ids")
    quick.add_argument("--subagent-session-keys", help="Comma-separated session keys")
    quick.add_argument("--subagents", help="JSON array")
    quick.add_argument("--side-effect-class", choices=sorted(SIDE_EFFECT_CLASSES))
    quick.add_argument("--repeat-policy", choices=sorted(REPEAT_POLICIES))
    quick.add_argument("--idempotency-key")
    quick.add_argument("--stale-after-seconds", type=int)
    quick.add_argument("--cwd")
    quick.add_argument("--branch")
    quick.add_argument("--commit")
    quick.add_argument("--note")
    quick.add_argument("--next-recovery-action")
    quick.add_argument("--checklist", type=lambda value: load_json_string_list_arg(value, "--checklist"))
    quick.add_argument("--success-criteria", type=lambda value: load_json_string_list_arg(value, "--success-criteria"))
    quick.add_argument("--no-artifact-expected", action="store_true")
    quick.add_argument("--resume-start", action="store_true")

    for name in ("progress", "wait", "verify", "complete", "fail", "visible-update", "wait-reminder-sent", "report-sent", "abandon"):
        cmd = add_command(name)
        add_common_event_args(cmd)
        if name == "wait":
            cmd.add_argument("--status", choices=["waiting_subagent", "waiting_user"], default="waiting_subagent")
        if name == "fail":
            cmd.add_argument("--failure-reason")
        if name in {"visible-update", "wait-reminder-sent", "report-sent"}:
            cmd.add_argument("--visible-delivery")
            cmd.add_argument("--delivery-message-id")

    complete_reported = add_command("complete-reported", help="Record completion and final visible report proof in one locked operation")
    add_common_event_args(complete_reported)
    complete_reported.add_argument("--visible-delivery", required=True)
    complete_reported.add_argument("--delivery-message-id", required=True)
    complete_reported.add_argument("--report-note")

    state = add_command("state")
    state.add_argument("--work-id")

    scan = add_command("scan")
    scan.add_argument("--cooldown-seconds", type=int, default=30 * 60)
    scan.add_argument("--record-wake", action="store_true", help="Deprecated no-op; record wake only after delivery with wake-delivered")

    orphans = add_command("orphans", help="Read-only reconciliation check for active OpenClaw tasks not referenced by active ledger entries")
    orphans.add_argument("--include-cron", action="store_true", help="Include cron tasks in orphan reconciliation")
    orphans.add_argument("--min-age-seconds", type=int, default=DEFAULT_ORPHAN_MIN_AGE_SECONDS, help="Only report active tasks at least this old; use 0 for all")

    watchdog = add_command("watchdog-check", help="Fast deterministic watchdog triage; clean exits need no LLM")
    watchdog.add_argument("--include-cron", action="store_true", help="Include cron tasks in orphan reconciliation")
    watchdog.add_argument("--min-age-seconds", type=int, default=DEFAULT_ORPHAN_MIN_AGE_SECONDS, help="Only report active tasks at least this old; use 0 for all")
    watchdog.add_argument("--cooldown-seconds", type=int, default=30 * 60, help="Recovery packet cooldown")

    prune = add_command("prune-terminal", help="Prune old reported/abandoned terminal work; dry-run by default")
    prune.add_argument("--days", type=int, default=DEFAULT_TERMINAL_RETENTION_DAYS)
    prune.add_argument("--apply", action="store_true", help="Actually remove matching terminal work and compact events")

    orphan_warning = add_command("orphan-warning-sent", help="Suppress repeat warnings for a reported orphan fingerprint")
    orphan_warning.add_argument("--orphan-fingerprint", required=True)
    orphan_warning.add_argument("--orphan-fingerprints", help="JSON array of equivalent orphan fingerprints to suppress together")
    orphan_warning.add_argument("--visible-delivery", required=True)
    orphan_warning.add_argument("--delivery-message-id", required=True)
    orphan_warning.add_argument("--note")

    orphan_handled = add_command("orphan-handled", help="Suppress a reconciled orphan that required no user-visible message")
    orphan_handled.add_argument("--orphan-fingerprint", required=True)
    orphan_handled.add_argument("--orphan-fingerprints", help="JSON array of equivalent orphan fingerprints to suppress together")
    orphan_handled.add_argument("--resolution", required=True, choices=sorted(ORPHAN_HANDLED_RESOLUTIONS))
    orphan_handled.add_argument("--note", required=True)

    terminal_ref_handled = add_command("terminal-ref-handled", help="Suppress a reconciled terminal task/subagent reference without completing the work")
    terminal_ref_handled.add_argument("--work-id", required=True)
    terminal_ref_handled.add_argument("--ref", required=True)
    terminal_ref_handled.add_argument("--terminal-status", required=True)
    terminal_ref_handled.add_argument("--resolution", required=True, choices=sorted(TERMINAL_REF_HANDLED_RESOLUTIONS))
    terminal_ref_handled.add_argument("--terminal-ref-fingerprints", help="JSON array of equivalent terminal ref fingerprints to suppress together")
    terminal_ref_handled.add_argument("--note", required=True)

    wake = add_command("wake-delivered")
    wake.add_argument("--work-id", required=True)
    wake.add_argument("--recovery-fingerprint", required=True)
    wake.add_argument("--note")

    hook_observe = add_command("hook-observe")
    hook_observe.add_argument("--work-id", required=True)
    hook_observe.add_argument("--payload", required=True, help="Hook payload JSON object")
    hook_observe.add_argument("--note")
    hook_observe.add_argument("--next-recovery-action")

    hook_guardrail = add_command("hook-guardrail")
    hook_guardrail.add_argument("--work-id", required=True)
    hook_guardrail.add_argument("--payload", required=True, help="Hook payload JSON object")

    add_command("rebuild")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()

    if args.command in {"start", "progress", "wait", "verify", "complete", "fail", "visible-update", "wait-reminder-sent", "report-sent", "abandon"}:
        event_type = {
            "visible-update": "visible_update_sent",
            "wait-reminder-sent": "wait_reminder_sent",
        }.get(args.command, args.command.replace("-", "_"))
        event = command_event(root, args, event_type)
        print(json.dumps({"ok": True, "event": event}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "complete-reported":
        result = command_complete_reported(root, args)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "state":
        states = load_states(root)
        if args.work_id:
            states = [state for state in states if state.get("work_id") == args.work_id]
        print(json.dumps({"ok": True, "items": states}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "scan":
        packets = scan_recoveries(root, args.cooldown_seconds)
        print(json.dumps({"ok": True, "has_recoveries": bool(packets), "recoveries": packets}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "orphans":
        min_age_seconds = validate_positive_int(args.min_age_seconds, "--min-age-seconds") if args.min_age_seconds else 0
        print(json.dumps(find_orphan_active_work(root, include_cron=args.include_cron, min_age_seconds=min_age_seconds), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "watchdog-check":
        min_age_seconds = validate_positive_int(args.min_age_seconds, "--min-age-seconds") if args.min_age_seconds else 0
        cooldown_seconds = validate_positive_int(args.cooldown_seconds, "--cooldown-seconds") if args.cooldown_seconds else 0
        print(json.dumps(watchdog_check(root, include_cron=args.include_cron, min_age_seconds=min_age_seconds, cooldown_seconds=cooldown_seconds), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "prune-terminal":
        days = validate_positive_int(args.days, "--days")
        print(json.dumps(prune_terminal_work(root, days=days or DEFAULT_TERMINAL_RETENTION_DAYS, apply=args.apply), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "orphan-warning-sent":
        item = record_orphan_warning(
            root,
            args.orphan_fingerprint,
            visible_delivery=load_json_object_arg(args.visible_delivery, "--visible-delivery", required=True),
            delivery_message_id=args.delivery_message_id,
            note=args.note,
            orphan_fingerprints=load_optional_json_string_list_arg(args.orphan_fingerprints, "--orphan-fingerprints"),
        )
        print(json.dumps({"ok": True, "warning": item}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "orphan-handled":
        item = record_orphan_handled(
            root,
            args.orphan_fingerprint,
            resolution=args.resolution,
            note=args.note,
            orphan_fingerprints=load_optional_json_string_list_arg(args.orphan_fingerprints, "--orphan-fingerprints"),
        )
        print(json.dumps({"ok": True, "handled": item}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "terminal-ref-handled":
        item = record_terminal_ref_handled(
            root,
            work_id=args.work_id,
            ref=args.ref,
            terminal_status=args.terminal_status,
            resolution=args.resolution,
            note=args.note,
            terminal_ref_fingerprints=load_optional_json_string_list_arg(args.terminal_ref_fingerprints, "--terminal-ref-fingerprints"),
        )
        print(json.dumps({"ok": True, "handled": item}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "quick-start":
        print(json.dumps(command_quick_start(root, args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "hook-observe":
        print(json.dumps(command_hook_observe(root, args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "hook-guardrail":
        print(json.dumps(command_hook_guardrail(root, args), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "wake-delivered":
        validate_work_id(args.work_id)
        states = [state for state in load_states(root) if state.get("work_id") == args.work_id]
        if not states:
            raise SystemExit(f"unknown work_id: {args.work_id}")
        expected = recovery_fingerprint(states[0])
        if expected != args.recovery_fingerprint:
            raise SystemExit("recovery fingerprint does not match current work state")
        event = append_event(root, {
            "event_type": "recovery_wake_delivered",
            "work_id": args.work_id,
            "recovery_fingerprint": args.recovery_fingerprint,
            "note": args.note,
        })
        print(json.dumps({"ok": True, "event": event}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "rebuild":
        states = write_all_states(root)
        print(json.dumps({"ok": True, "items": states}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    parser.error("unknown command")
    return 64


if __name__ == "__main__":
    raise SystemExit(main())

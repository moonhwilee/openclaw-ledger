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
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def default_root() -> Path:
    configured = os.environ.get("OPENCLAW_WORKSPACE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".openclaw" / "workspace"


DEFAULT_ROOT = default_root()
ACTIVE_STATES = {"running", "waiting_subagent", "waiting_user", "verifying"}
TERMINAL_STATES = {"reported", "abandoned"}
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


def validate_visible_delivery(value: dict[str, Any] | None, name: str, *, required: bool = False) -> dict[str, Any] | None:
    if value is None:
        if required:
            raise SystemExit(f"{name} is required")
        return None
    has_session = isinstance(value.get("session_key") or value.get("sessionKey"), str)
    has_channel_target = isinstance(value.get("channel"), str) and isinstance(value.get("target") or value.get("to"), str)
    if not (has_session or has_channel_target):
        raise SystemExit(f"{name} must include session_key/sessionKey or channel plus target/to")
    return value


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
    event = dict(event)
    event.setdefault("schema_version", SCHEMA_VERSION)
    event.setdefault("event_at", now_iso())
    event["sequence"] = next_sequence(root)
    events_path(root).parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with events_path(root).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    write_all_states_unlocked(root)
    return event


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
        "no_artifact_expected": False,
        "visible_delivery": {},
        "completion_report_sent": False,
        "last_event": None,
        "last_progress_at": None,
        "last_visible_update_at": None,
        "last_wait_reminder_at": None,
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
        if event_type != "recovery_wake_delivered":
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
        if event.get("visible_delivery"):
            state["visible_delivery"] = {**state.get("visible_delivery", {}), **event["visible_delivery"]}
        for key in (
            "expected_outputs",
            "artifact_paths",
            "subagents",
            "openclaw_task_ids",
            "subagent_session_keys",
            "side_effects_performed",
            "external_actions_attempted",
        ):
            if event.get(key):
                state[key] = merge_list(state.get(key, []), event[key])

        if event_type in {"start", "progress", "wait", "verify"}:
            state["last_progress_at"] = event.get("event_at")
        if event_type == "start":
            current_status = "running"
        if event_type == "wait":
            current_status = event.get("status") or "waiting_subagent"
        elif event_type == "verify":
            current_status = "verifying"
        elif event_type == "complete":
            current_status = "completed_unreported"
        elif event_type == "fail":
            current_status = "failed_unreported"
            state["failure_reason"] = event.get("failure_reason") or event.get("note")
        elif event_type == "report_sent":
            current_status = "reported"
            state["completion_report_sent"] = True
            state["report_sent_at"] = event.get("event_at")
            if event.get("delivery_message_id"):
                state.setdefault("visible_delivery", {})["message_id"] = event["delivery_message_id"]
        elif event_type == "visible_update_sent":
            state["last_visible_update_at"] = event.get("event_at")
            if event.get("delivery_message_id"):
                state.setdefault("visible_delivery", {})["last_update_message_id"] = event["delivery_message_id"]
        elif event_type == "wait_reminder_sent":
            state["last_visible_update_at"] = event.get("event_at")
            state["last_wait_reminder_at"] = event.get("event_at")
            if event.get("delivery_message_id"):
                state.setdefault("visible_delivery", {})["last_wait_reminder_message_id"] = event["delivery_message_id"]
        elif event_type == "abandon":
            current_status = "abandoned"
            state["abandon_reason"] = event.get("abandon_reason") or event.get("note")

        if event_type == "recovery_wake_delivered":
            state["last_recovery_packet_at"] = event.get("event_at")
            state["last_recovery_fingerprint"] = event.get("recovery_fingerprint")

    activity_candidates = [
        parse_time(state.get("last_progress_at")),
        parse_time(state.get("last_wait_reminder_at")),
    ]
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


def load_states(root: Path) -> list[dict[str, Any]]:
    return write_all_states(root)


def recovery_fingerprint(state: dict[str, Any]) -> str:
    payload = {
        "work_id": state.get("work_id"),
        "status": state.get("status"),
        "last_material_event_sequence": (state.get("last_material_event") or {}).get("sequence"),
        "next_recovery_action": state.get("next_recovery_action"),
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
            "current artifacts/tasks, and conversation context available to this session; then either "
            "repair the ledger with explicit context, ask the user for the missing decision, or mark the work blocked/abandoned."
        )
    elif status == "waiting_user":
        recovery_instruction = (
            "This work is waiting for user input and may not be stuck. Reconcile current conversation "
            "state first. If the user has answered, continue the work, verify, send the visible completion "
            "report, then record report_sent. If still blocked on user input, send at most one concise "
            "waiting reminder and record a fresh wait event instead of report_sent."
        )
    else:
        recovery_instruction = (
            "You are recovering unfinished work, not merely notifying. Read this ledger state, "
            "inspect current artifacts/tasks before acting, do not repeat external/destructive "
            "side effects without user approval, execute the next safe recovery action, verify, "
            "send one visible completion report, then record report_sent."
        )
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
        "visible_delivery": state.get("visible_delivery", {}),
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


def validate_start(args: argparse.Namespace) -> None:
    validate_work_id(args.work_id)
    if args.side_effect_class not in SIDE_EFFECT_CLASSES:
        raise SystemExit(f"invalid side effect class: {args.side_effect_class}")
    if args.repeat_policy is None:
        args.repeat_policy = DEFAULT_REPEAT_POLICY[args.side_effect_class]
    if not args.request_summary:
        raise SystemExit("--request-summary is required")
    validate_owner_session_key(args.owner_session_key)
    validate_visible_delivery(load_json_object_arg(args.visible_delivery, "--visible-delivery", required=True), "--visible-delivery", required=True)
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
    if event_type == "start":
        validate_start(args)
    if event_type == "report_sent":
        validate_visible_delivery(
            load_json_object_arg(getattr(args, "visible_delivery", None), "--visible-delivery", required=True),
            "--visible-delivery",
            required=True,
        )
        if not getattr(args, "delivery_message_id", None):
            raise SystemExit("--delivery-message-id is required for report-sent")
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
        "visible_delivery": validate_visible_delivery(
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
                if existing_state.get("status") not in {"reported", "abandoned"} and not getattr(args, "resume_start", False):
                    raise SystemExit(f"active work_id already exists: {args.work_id}")
            return append_event_unlocked(root, event)
    with file_lock(root):
        if args.work_id not in grouped_events(root):
            raise SystemExit(f"unknown work_id: {args.work_id}")
        return append_event_unlocked(root, event)


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
    parser = argparse.ArgumentParser(description="Workspace-local recoverable work ledger")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Workspace root")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
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

    for name in ("progress", "wait", "verify", "complete", "fail", "visible-update", "wait-reminder-sent", "report-sent", "abandon"):
        cmd = sub.add_parser(name)
        add_common_event_args(cmd)
        if name == "wait":
            cmd.add_argument("--status", choices=["waiting_subagent", "waiting_user"], default="waiting_subagent")
        if name == "fail":
            cmd.add_argument("--failure-reason")
        if name in {"visible-update", "wait-reminder-sent", "report-sent"}:
            cmd.add_argument("--visible-delivery")
            cmd.add_argument("--delivery-message-id")

    state = sub.add_parser("state")
    state.add_argument("--work-id")

    scan = sub.add_parser("scan")
    scan.add_argument("--cooldown-seconds", type=int, default=30 * 60)
    scan.add_argument("--record-wake", action="store_true", help="Deprecated no-op; record wake only after delivery with wake-delivered")

    wake = sub.add_parser("wake-delivered")
    wake.add_argument("--work-id", required=True)
    wake.add_argument("--recovery-fingerprint", required=True)
    wake.add_argument("--note")

    sub.add_parser("rebuild")
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

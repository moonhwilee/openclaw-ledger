#!/usr/bin/env python3
"""Small hook payload contract helpers for Ledger and GoalFlow.

This module intentionally does not install or run Codex/OpenClaw hooks. It
normalizes hook-shaped JSON into conservative observations and guardrail
decisions so failure modes can be tested before runtime integration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any


CODEX_EVENTS = {
    "SessionStart",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "UserPromptSubmit",
    "Stop",
}
OPENCLAW_EVENTS = {
    "command",
    "session",
    "agent",
    "gateway",
    "message",
}
RUNTIME_ONLY_KEYS = {
    "idempotency_key",
    "blocked_idempotency_key",
    "idempotencyKey",
    "blockedIdempotencyKey",
}
PUBLIC_SUMMARY_BLOCKLIST = {
    "session_id",
    "sessionKey",
    "turn_id",
    "tool_use_id",
    "cwd",
    "model",
    "permission_mode",
    "transcript_path",
    "timestamp",
}
GH_WRITE_ACTIONS = {
    "pr": {"merge", "close", "reopen", "ready", "comment", "review", "edit", "create", "lock", "unlock", "revert", "update-branch"},
    "issue": {"create", "close", "reopen", "comment", "edit", "delete", "lock", "unlock", "pin", "unpin", "transfer"},
    "repo": {"create", "delete", "edit", "rename", "archive", "unarchive", "fork", "sync"},
    "release": {"create", "delete", "edit", "upload"},
    "workflow": {"run", "enable", "disable"},
    "run": {"rerun", "cancel", "delete"},
    "label": {"create", "delete", "edit", "clone"},
    "secret": {"set", "delete", "remove"},
    "variable": {"set", "delete", "remove"},
    "gist": {"create", "edit", "delete"},
    "ssh-key": {"add", "delete", "remove"},
}
GH_NESTED_WRITE_ACTIONS = {
    ("repo", "deploy-key"): {"add", "delete", "remove"},
}
GH_GLOBAL_OPTIONS_WITH_VALUE = {"-R", "--repo", "-H", "--hostname", "--config", "--help"}
GH_GROUP_OPTIONS_WITH_VALUE = {"-R", "--repo", "-H", "--hostname"}
GIT_GLOBAL_OPTIONS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
GH_API_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
GH_API_FIELD_FLAGS = {"-f", "--field", "-F", "--raw-field", "--input"}
SHELL_COMMANDS = {"sh", "bash", "zsh"}
INTERPRETER_COMMANDS = {"python", "python3", "node"}
GITHUB_WRITE_TOOL_RE = re.compile(
    r"\bmcp__codex_apps__github_"
    r"(add|create|delete|dismiss|enable|label|lock|mark|merge|remove|reply|request|rerun|resolve|unresolve|unlock|update)_[a-z0-9_]*\b"
    r"|\bmcp__codex_apps__github_(convert_pull_request_to_draft|mark_pull_request_ready_for_review)\b"
)
GITHUB_WRITE_CALL_RE = re.compile(
    r"\b(?:tools\s*\.\s*)?mcp__codex_apps__github_"
    r"(add|create|delete|dismiss|enable|label|lock|mark|merge|remove|reply|request|rerun|resolve|unresolve|unlock|update)_[a-z0-9_]*\s*\("
    r"|\b(?:tools\s*\.\s*)?mcp__codex_apps__github_(convert_pull_request_to_draft|mark_pull_request_ready_for_review)\s*\("
)
HOOK_ACTION_CLASSIFIER_SCHEMA_VERSION = "hook.action_classifier.v0.1"
HOOK_ACTION_CLASSES = {
    "read_only",
    "local_files",
    "repo_changes",
    "external_message",
    "public_post",
    "destructive",
    "gateway_runtime",
}
APPROVAL_SENSITIVE_HOOK_ACTION_CLASSES = {
    "repo_changes",
    "external_message",
    "public_post",
    "destructive",
    "gateway_runtime",
}
NON_IDEMPOTENT_HOOK_ACTION_CLASSES = APPROVAL_SENSITIVE_HOOK_ACTION_CLASSES | {"local_files"}
UNSAFE_COMMAND_SPECS: tuple[dict[str, Any], ...] = (
    {
        "prefixes": (("gog_helper.py", "send-email"), ("gog", "gmail", "send")),
        "action_class": "external_message",
        "summary": "email send command",
        "reason": "unsafe command sends email",
    },
    {
        "prefixes": (("openclaw", "message", "send"), ("message", "send")),
        "action_class": "external_message",
        "summary": "message send command",
        "reason": "unsafe command sends a message",
    },
    {
        "prefixes": (("browser", "open"),),
        "action_class": "external_message",
        "summary": "browser action with likely external effect",
        "reason": "unsafe browser open signal",
        "confidence": "low",
    },
    {
        "prefixes": (
            ("gateway", "restart"),
            ("launchctl", "kickstart"),
            ("openclaw", "doctor", "--fix"),
            ("openclaw", "update"),
            ("sudo", "npm"),
            ("sudo", "openclaw"),
            ("npm", "install", "-g"),
            ("pnpm", "install", "-g"),
        ),
        "action_class": "gateway_runtime",
        "summary": "runtime or package management command",
        "reason": "unsafe runtime/package command",
    },
    {
        "prefixes": (("db", "migration"), ("database", "migration"), ("alembic", "upgrade"), ("psql",)),
        "action_class": "destructive",
        "summary": "database mutation command",
        "reason": "unsafe database mutation command",
    },
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def redact_runtime_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: redact_runtime_keys(item)
            for key, item in value.items()
            if key not in RUNTIME_ONLY_KEYS
        }
    if isinstance(value, list):
        return [redact_runtime_keys(item) for item in value]
    if isinstance(value, str):
        return redact_command_preview(value)
    return value


def public_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "event": event_name(payload),
        "tool_name": payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else None,
    }
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("cmd") or tool_input.get("command")
        if isinstance(command, str) and command.strip():
            summary["command_preview"] = redact_command_preview(command.strip())[:160]
        elif isinstance(command, list):
            summary["command_preview"] = redact_command_preview(" ".join(str(item) for item in command))[:160]
    tool_response = payload.get("tool_response")
    if isinstance(tool_response, dict):
        for key in ("exit_code", "status", "success"):
            if key in tool_response and isinstance(tool_response[key], (str, int, bool, type(None))):
                summary[key] = tool_response[key]
    return {key: value for key, value in summary.items() if value not in (None, "", [], {}) and key not in PUBLIC_SUMMARY_BLOCKLIST}


def redact_command_preview(command: str) -> str:
    text = re.sub(r"(--(?:idempotency-key|blocked-idempotency-key))(?:=|\s+)\S+", r"\1 [redacted]", command)
    text = re.sub(r"((?:idempotency_key|blocked_idempotency_key)=)\S+", r"\1[redacted]", text)
    return text


def without_volatile_delivery_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: without_volatile_delivery_fields(item)
            for key, item in value.items()
            if key not in {"timestamp"}
        }
    if isinstance(value, list):
        return [without_volatile_delivery_fields(item) for item in value]
    return value


def hook_family(payload: dict[str, Any]) -> str:
    event_name = payload.get("hook_event_name")
    if event_name in CODEX_EVENTS:
        return "codex"
    event_type = payload.get("type")
    if event_type in OPENCLAW_EVENTS:
        return "openclaw"
    return "unknown"


def event_name(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("hook_event_name"), str):
        return payload["hook_event_name"]
    event_type = payload.get("type")
    action = payload.get("action")
    if isinstance(event_type, str) and isinstance(action, str):
        return f"{event_type}:{action}"
    if isinstance(event_type, str):
        return event_type
    return "unknown"


def is_pre_action_event(name: str) -> bool:
    return name in {"PreToolUse", "PermissionRequest", "command:PreToolUse", "message:send"}


def permission_request_has_action_details(payload: dict[str, Any]) -> bool:
    if payload.get("hook_event_name") != "PermissionRequest":
        return True
    return (
        permission_request_input_has_action_details(payload.get("tool_input"))
        or permission_request_input_has_action_details(hook_action_probe_input(payload))
    )


def permission_request_input_has_action_details(value: Any) -> bool:
    if isinstance(value, str) and value.strip():
        return True
    if isinstance(value, list) and value:
        return True
    if not isinstance(value, dict):
        return False
    for key in ("cmd", "command", "source", "argv", "args", "executable"):
        detail = value.get(key)
        if isinstance(detail, str) and detail.strip():
            return True
        if isinstance(detail, list) and detail:
            return True
        if isinstance(detail, dict) and detail:
            return True
    return False


def event_fingerprint(payload: dict[str, Any]) -> str:
    payload_hash = sha256(without_volatile_delivery_fields(redact_runtime_keys(payload)))
    basis = {
        "family": hook_family(payload),
        "event": event_name(payload),
        "session_id": payload.get("session_id") or payload.get("sessionKey"),
        "turn_id": payload.get("turn_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "tool_name": payload.get("tool_name"),
        "action": payload.get("action"),
        "payload_hash": payload_hash,
    }
    return sha256(basis)


def delivery_fingerprint(payload: dict[str, Any]) -> str:
    return sha256({
        "event_fingerprint": event_fingerprint(payload),
        "timestamp": payload.get("timestamp"),
    })


def is_unsafe_tool_attempt(payload: dict[str, Any]) -> bool:
    tool_name = str(payload.get("tool_name") or "")
    tool_input = hook_action_probe_input(payload)
    if GITHUB_WRITE_TOOL_RE.search(tool_name):
        return True
    if "message" in tool_name.lower() or "send" in tool_name.lower():
        return True
    if has_git_push_command(tool_input):
        return True
    if has_gh_write_command(tool_input):
        return True
    if has_github_write_call_source(tool_input):
        return True
    return has_generic_unsafe_command(tool_input)


def hook_action_probe_input(payload: dict[str, Any]) -> Any:
    if "tool_input" in payload:
        return payload.get("tool_input")
    probe = {
        key: payload[key]
        for key in ("cmd", "command", "source", "argv", "args", "executable")
        if key in payload
    }
    return probe if probe else None


def command_has_prefix(tool_input: Any, prefix: tuple[str, ...]) -> bool:
    for tokens in command_token_sequences(tool_input):
        index = skip_env_prefix(tokens)
        if index >= len(tokens):
            continue
        relevant = [
            Path(token).name.lower() if offset == 0 else str(token).lower()
            for offset, token in enumerate(tokens[index:])
        ]
        starts = [0]
        if len(relevant) > 1 and Path(relevant[0]).name in INTERPRETER_COMMANDS:
            relevant[1] = Path(relevant[1]).name.lower()
            starts.append(1)
        for start in starts:
            if len(relevant) >= start + len(prefix) and tuple(relevant[start:start + len(prefix)]) == prefix:
                return True
    return False


def unsafe_command_spec(tool_input: Any) -> dict[str, Any] | None:
    for spec in UNSAFE_COMMAND_SPECS:
        if any(command_has_prefix(tool_input, prefix) for prefix in spec["prefixes"]):
            return spec
    return None


def classify_unsafe_command_action(tool_input: Any) -> tuple[str, str, str] | None:
    spec = unsafe_command_spec(tool_input)
    if spec:
        return (spec["action_class"], spec["summary"], spec["reason"])
    if any(rm_recursive_force_from_tokens(tokens) for tokens in command_token_sequences(tool_input)):
        return ("destructive", "destructive filesystem command", "unsafe recursive force removal")
    return None


def classify_hook_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Return advisory action-class signals for a hook-shaped payload.

    This classifier intentionally does not allow, block, approve, retry, or
    recover anything. Ledger and GoalFlow own those policy decisions.
    """
    tool_name = str(payload.get("tool_name") or "")
    tool_name_lower = tool_name.lower()
    tool_input = payload.get("tool_input")
    probe_input = hook_action_probe_input(payload)
    command_spec = unsafe_command_spec(probe_input)
    action_class = "read_only"
    confidence = "high"
    summary = "read-only or telemetry-like hook payload"
    reasons: list[str] = []

    if payload.get("hook_event_name") == "PermissionRequest" and not permission_request_has_action_details(payload):
        action_class = "gateway_runtime"
        confidence = "low"
        summary = "permission request without action details"
        reasons.append("PermissionRequest lacks tool or command details")
    elif payload.get("type") == "message":
        action_class = "external_message"
        summary = "message delivery telemetry"
        reasons.append("payload type is message")
    elif payload.get("hook_event_name") == "Stop":
        confidence = "medium"
        summary = "turn stop telemetry"
        reasons.append("Stop event is telemetry; consumer state decides whether to nudge")
    elif tool_name_lower == "apply_patch":
        action_class = "local_files"
        summary = "local file patch"
        reasons.append("tool_name is apply_patch")
    elif GITHUB_WRITE_TOOL_RE.search(tool_name):
        action_class = "repo_changes"
        summary = "GitHub write connector tool"
        reasons.append("tool_name matches GitHub write connector pattern")
    elif tool_name_lower == "xurl" and isinstance(tool_input, dict) and tool_input.get("action") in {"post", "reply"}:
        action_class = "public_post"
        summary = "public X post or reply"
        reasons.append("xurl action is post/reply")
    elif command_has_prefix(probe_input, ("xurl", "post")) or command_has_prefix(probe_input, ("xurl", "reply")):
        action_class = "public_post"
        summary = "public X post or reply command"
        reasons.append("xurl command is post/reply")
    elif "message" in tool_name_lower or "send" in tool_name_lower:
        action_class = "external_message"
        summary = "message/send tool"
        reasons.append("tool_name contains message/send")
    elif any(rm_recursive_force_from_tokens(tokens) for tokens in command_token_sequences(probe_input)):
        action_class = "destructive"
        summary = "destructive filesystem command"
        reasons.append("command looks like recursive force removal")
    elif command_spec:
        action_class = command_spec["action_class"]
        summary = command_spec["summary"]
        confidence = command_spec.get("confidence", confidence)
        reasons.append(command_spec["reason"])
    elif (
        has_git_push_command(probe_input)
        or has_gh_write_command(probe_input)
        or has_github_write_call_source(probe_input)
    ):
        action_class = "repo_changes"
        summary = "repository write command or GitHub write call"
        reasons.append("command/source matches repository write signal")
    else:
        unsafe_command_classification = classify_unsafe_command_action(probe_input)
        if unsafe_command_classification:
            action_class, summary, reason = unsafe_command_classification
            reasons.append(reason)

    if not reasons:
        reasons.append("no side-effect classifier signal matched")
    is_delivery_telemetry = payload.get("type") == "message" and payload.get("action") == "sent"
    is_post_tool_observation = payload.get("hook_event_name") == "PostToolUse"
    approval_sensitive = action_class in APPROVAL_SENSITIVE_HOOK_ACTION_CLASSES and not is_delivery_telemetry and not is_post_tool_observation
    is_non_idempotent = action_class in NON_IDEMPOTENT_HOOK_ACTION_CLASSES and not is_delivery_telemetry and not is_post_tool_observation
    return {
        "schema_version": HOOK_ACTION_CLASSIFIER_SCHEMA_VERSION,
        "hook_action_class": action_class,
        "confidence": confidence,
        "is_non_idempotent": is_non_idempotent,
        "approval_sensitive_candidate": approval_sensitive,
        "current_guardrail_unsafe_candidate": is_unsafe_tool_attempt(payload),
        "summary": summary,
        "reasons": reasons[:3],
    }


def tokenize_command(value: Any) -> list[str]:
    if isinstance(value, list):
        tokens: list[str] = []
        for item in value:
            tokens.extend(tokenize_command(item))
        return tokens
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    if value is None:
        return []
    return [str(value)]


def tokenize_arg_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return tokenize_command(value)


def command_tokens_from_input(tool_input: Any) -> list[str]:
    if not isinstance(tool_input, dict):
        return tokenize_command(tool_input)
    tokens: list[str] = []
    for key in ("cmd", "command", "argv", "executable"):
        if key in tool_input:
            tokens.extend(tokenize_arg_values(tool_input[key]) if key == "argv" else tokenize_command(tool_input[key]))
            break
    if "args" in tool_input:
        tokens.extend(tokenize_arg_values(tool_input["args"]))
    return tokens


def skip_env_prefix(tokens: list[str]) -> int:
    if not tokens or Path(tokens[0]).name != "env":
        return 0
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("-"):
            index += 1
        elif token in {"-i", "--ignore-environment", "-0", "--null"}:
            index += 1
        elif token in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}:
            index += 2
        elif token.startswith("--unset=") or token.startswith("--chdir=") or token.startswith("--split-string="):
            index += 1
        elif token.startswith("-u") and len(token) > 2:
            index += 1
        else:
            break
    return index


def env_split_inner_commands(tokens: list[str]) -> list[str]:
    if not tokens or Path(tokens[0]).name != "env":
        return []
    commands: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-S", "--split-string"} and index + 1 < len(tokens):
            commands.append(tokens[index + 1])
            index += 2
        elif token.startswith("-S") and len(token) > 2:
            commands.append(token[2:])
            index += 1
        elif token.startswith("--split-string="):
            commands.append(token.split("=", 1)[1])
            index += 1
        elif token in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
        elif token.startswith("--unset=") or token.startswith("--chdir="):
            index += 1
        elif token.startswith("-u") and len(token) > 2:
            index += 1
        elif token in {"-i", "--ignore-environment", "-0", "--null"} or ("=" in token and not token.startswith("-")):
            index += 1
        else:
            break
    return commands


def command_segments(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            continue
        if char in {";", "&", "|"}:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            continue
        current.append(char)
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def has_shell_separators(command: str) -> bool:
    return len(command_segments(command)) > 1


def git_push_from_tokens(tokens: list[str]) -> bool:
    if not tokens:
        return False
    index = skip_env_prefix(tokens)
    if index >= len(tokens):
        return False
    command = Path(tokens[index]).name
    if command != "git":
        return False
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token == "push":
            return True
        if token == "--":
            index += 1
            continue
        if not token.startswith("-"):
            return False
        if token in GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
        elif any(token.startswith(option + "=") for option in GIT_GLOBAL_OPTIONS_WITH_VALUE):
            index += 1
        else:
            index += 1
    return False


def shell_inner_commands_from_tokens(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    index = skip_env_prefix(tokens)
    if index >= len(tokens):
        return []
    command = Path(tokens[index]).name
    if command not in SHELL_COMMANDS:
        return []
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-c", "-lc", "-ic", "-lic"}:
            return [tokens[index + 1]] if index + 1 < len(tokens) else []
        if token.startswith("-") and "c" in token[1:]:
            return [tokens[index + 1]] if index + 1 < len(tokens) else []
        index += 1
    return []


def command_token_sequences(tool_input: Any, *, depth: int = 0, max_depth: int = 2) -> list[list[str]]:
    if depth > max_depth:
        return []
    sequences: list[list[str]] = []
    if isinstance(tool_input, dict) and ("argv" in tool_input or "executable" in tool_input):
        sequences.append(command_tokens_from_input(tool_input))
    elif (
        isinstance(tool_input, dict)
        and "args" in tool_input
        and tokenize_arg_values(tool_input.get("args"))
        and isinstance(tool_input.get("cmd") or tool_input.get("command"), str)
        and not has_shell_separators(tool_input.get("cmd") or tool_input.get("command"))
    ):
        sequences.append(command_tokens_from_input(tool_input))
    else:
        for command in command_strings_from_input(tool_input):
            sequences.extend(tokenize_command(segment) for segment in command_segments(command))
        for command in source_exec_command_strings(tool_input):
            sequences.extend(tokenize_command(segment) for segment in command_segments(command))
        if not sequences and not isinstance(tool_input, dict):
            sequences.append(tokenize_command(tool_input))

    normalized: list[list[str]] = []
    for tokens in sequences:
        if not tokens:
            continue
        normalized.append(tokens)
        for inner_command in env_split_inner_commands(tokens):
            normalized.extend(command_token_sequences(inner_command, depth=depth + 1, max_depth=max_depth))
        for inner_command in shell_inner_commands_from_tokens(tokens):
            normalized.extend(command_token_sequences(inner_command, depth=depth + 1, max_depth=max_depth))
    return normalized


def has_git_push_command(tool_input: Any) -> bool:
    return any(git_push_from_tokens(tokens) for tokens in command_token_sequences(tool_input))


def command_strings_from_input(tool_input: Any) -> list[str]:
    if isinstance(tool_input, dict):
        values = []
        for key in ("cmd", "command", "source"):
            if isinstance(tool_input.get(key), str):
                values.append(tool_input[key])
        return values
    if isinstance(tool_input, str):
        return [tool_input]
    return []


def source_exec_command_strings(tool_input: Any) -> list[str]:
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("source"), str):
        return []
    pattern = re.compile(
        r"\b(?:tools\s*\.\s*)?exec_command\s*\([^)]*?\b['\"]?cmd['\"]?\s*:\s*",
        re.DOTALL,
    )
    commands = []
    source = tool_input["source"]
    for match in pattern.finditer(source):
        parsed = parse_source_string_literal(source, match.end())
        if parsed is not None:
            commands.append(parsed)
    return commands


def parse_source_string_literal(source: str, start: int) -> str | None:
    index = start
    while index < len(source) and source[index].isspace():
        index += 1
    if index >= len(source) or source[index] not in {"'", '"', "`"}:
        return None
    quote = source[index]
    index += 1
    chars: list[str] = []
    escaped = False
    while index < len(source):
        char = source[index]
        index += 1
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            return "".join(chars)
        chars.append(char)
    return None


def has_github_write_call_source(tool_input: Any) -> bool:
    return isinstance(tool_input, dict) and isinstance(tool_input.get("source"), str) and GITHUB_WRITE_CALL_RE.search(tool_input["source"]) is not None


def gh_write_from_tokens(tokens: list[str]) -> bool:
    if not tokens:
        return False
    index = skip_env_prefix(tokens)
    if index >= len(tokens):
        return False
    command = Path(tokens[index]).name
    if command != "gh":
        return False
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        if token in GH_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
        elif any(token.startswith(option + "=") for option in GH_GLOBAL_OPTIONS_WITH_VALUE):
            index += 1
        else:
            index += 1
    if index >= len(tokens):
        return False
    group = tokens[index]
    args_index = skip_gh_options(tokens, index + 1)
    args = tokens[args_index:]
    if group == "api":
        method = "GET"
        explicit_method = False
        has_field_or_input = False
        for arg_index, arg in enumerate(args):
            if arg in {"-X", "--method"} and arg_index + 1 < len(args):
                method = args[arg_index + 1].upper()
                explicit_method = True
            elif arg.startswith("-X") and len(arg) > 2:
                method = arg[2:].upper()
                explicit_method = True
            elif arg.startswith("--method="):
                method = arg.split("=", 1)[1].upper()
                explicit_method = True
            elif arg in GH_API_FIELD_FLAGS or any(arg.startswith(flag + "=") for flag in GH_API_FIELD_FLAGS if flag.startswith("--")):
                has_field_or_input = True
        if method in GH_API_WRITE_METHODS:
            return True
        return has_field_or_input and not (explicit_method and method == "GET")
    if not args:
        return False
    nested = (group, args[0])
    if nested in GH_NESTED_WRITE_ACTIONS and len(args) > 1:
        return args[1] in GH_NESTED_WRITE_ACTIONS[nested]
    action = args[0]
    return action in GH_WRITE_ACTIONS.get(group, set())


def skip_gh_options(tokens: list[str], index: int) -> int:
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if not token.startswith("-"):
            return index
        if token in GH_GROUP_OPTIONS_WITH_VALUE:
            index += 2
        elif any(token.startswith(option + "=") for option in GH_GROUP_OPTIONS_WITH_VALUE):
            index += 1
        else:
            index += 1
    return index


def has_gh_write_command(tool_input: Any) -> bool:
    return any(gh_write_from_tokens(tokens) for tokens in command_token_sequences(tool_input))


def rm_recursive_force_from_tokens(tokens: list[str]) -> bool:
    if not tokens:
        return False
    index = skip_env_prefix(tokens)
    if index >= len(tokens) or Path(tokens[index]).name != "rm":
        return False
    recursive = False
    force = False
    for token in tokens[index + 1:]:
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            continue
        if token in {"--recursive", "--dir"}:
            recursive = True
        elif token == "--force":
            force = True
        elif token.startswith("--"):
            continue
        else:
            flags = token[1:]
            recursive = recursive or "r" in flags or "R" in flags
            force = force or "f" in flags
    return recursive and force


def has_generic_unsafe_command(tool_input: Any) -> bool:
    return unsafe_command_spec(tool_input) is not None or any(
        rm_recursive_force_from_tokens(tokens)
        for tokens in command_token_sequences(tool_input)
    )


def ledger_required_visible_report_attempt(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    if state.get("status") not in {"completed_unreported", "failed_unreported"}:
        return False
    if state.get("completion_report_sent"):
        return False
    report_input = visible_report_send_input(payload)
    if not report_input:
        return False
    expected = state.get("completion_visible_delivery") or state.get("visible_delivery") or {}
    expected_channel = expected.get("channel")
    expected_target = expected.get("target") or expected.get("to")
    supplied_channel = report_input.get("channel")
    supplied_target = report_input.get("target") or report_input.get("to")
    if not expected_channel or not expected_target or not supplied_channel or not supplied_target:
        return False
    if supplied_channel != expected_channel:
        return False
    owner_session = state.get("owner_session_key")
    if not owner_session:
        return False
    payload_session = payload.get("session_key") or payload.get("sessionKey") or payload.get("session_id")
    supplied_session = report_input.get("session_key") or report_input.get("sessionKey") or report_input.get("session_id")
    session_matches_owner = payload_session == owner_session or supplied_session == owner_session
    if not session_matches_owner:
        return False
    if payload_session and payload_session != owner_session:
        return False
    if not owner_session_matches_delivery(owner_session, expected_channel, expected_target, session_matches_owner):
        return False
    if supplied_session and supplied_session != owner_session:
        return False
    if expected_channel == "telegram":
        if normalize_telegram_target(str(supplied_target or "")) != normalize_telegram_target(str(expected_target)):
            return False
    elif supplied_target != expected_target:
        return False
    for key in ("accountId", "threadId"):
        if (expected.get(key) in (None, "")) != (report_input.get(key) in (None, "")):
            return False
        if expected.get(key) not in (None, "") and report_input.get(key) != expected[key]:
            return False
    return True


def visible_report_send_input(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("type") == "message" and payload.get("action") == "send":
        return payload
    tool_name = str(payload.get("tool_name") or "").lower()
    if "message" not in tool_name and "send" not in tool_name:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("action") not in {None, "send"}:
        return None
    return tool_input


def normalize_telegram_target(target: str) -> str:
    text = str(target)
    if text.startswith("telegram:"):
        return text.split(":", 1)[1]
    if text.startswith("telegram-"):
        return text.split("-", 1)[1]
    return text


def owner_session_matches_delivery(owner_session: Any, channel: Any, target: Any, session_matches_owner: bool = False) -> bool:
    if not owner_session:
        return False
    if channel != "telegram":
        return True
    owner_text = str(owner_session)
    match = re.search(r"(?:^|:)telegram:direct:([^:]+)$", owner_text)
    if not match:
        return session_matches_owner
    return normalize_telegram_target(match.group(1)) == normalize_telegram_target(str(target))


def ledger_observation(payload: dict[str, Any]) -> dict[str, Any]:
    family = hook_family(payload)
    name = event_name(payload)
    observation: dict[str, Any] = {
        "schema_version": "hook.ledger_observation.v0.1",
        "family": family,
        "event": name,
        "fingerprint": event_fingerprint(payload),
        "delivery_fingerprint": delivery_fingerprint(payload),
        "payload_hash": sha256(without_volatile_delivery_fields(redact_runtime_keys(payload))),
        "redacted_payload": redact_runtime_keys(payload),
        "public_summary": public_summary(payload),
        "candidate_event_type": "progress",
        "authority": "candidate",
    }
    if name == "PostToolUse":
        observation["candidate_event_type"] = "tool_observation"
        observation["tool_name"] = payload.get("tool_name")
    elif name in {"message:sent", "message"} and payload.get("action") == "sent":
        observation["candidate_event_type"] = "visible_update_sent"
    elif name == "Stop":
        observation["candidate_event_type"] = "progress"
        observation["stop_seen"] = True
    return observation


def ledger_guardrail(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    status = state.get("status")
    report_sent = bool(state.get("completion_report_sent"))
    name = event_name(payload)
    decision = {
        "schema_version": "hook.guardrail_decision.v0.1",
        "system": "ledger",
        "event": name,
        "decision": "allow",
        "reason": "no_guardrail_triggered",
    }
    if name == "Stop" and status in {"completed_unreported", "failed_unreported"} and not report_sent:
        decision.update({
            "decision": "nudge",
            "reason": "ledger_terminal_unreported",
            "required_action": "send_visible_report_and_record_report_sent",
        })
    if is_pre_action_event(name):
        classification = classify_hook_action(payload)
        unsafe_attempt = classification["current_guardrail_unsafe_candidate"] or classification["approval_sensitive_candidate"]
        if unsafe_attempt and ledger_required_visible_report_attempt(payload, state):
            decision.update({
                "decision": "allow",
                "reason": "ledger_required_visible_report",
                "required_action": "send_visible_report_then_record_report_sent",
            })
        elif unsafe_attempt and not state:
            decision.update({
                "decision": "block",
                "reason": "unsafe_attempt_without_durable_state",
                "required_action": "create_or_reconcile_ledger_state_before_side_effect",
            })
        elif unsafe_attempt:
            decision.update({
                "decision": "block",
                "reason": "unsafe_attempt_requires_durable_clearance",
                "required_action": "record_owner_approval_or_reconciled_idempotent_boundary",
            })
    return decision


def goalflow_observation(payload: dict[str, Any]) -> dict[str, Any]:
    name = event_name(payload)
    observation = {
        "schema_version": "hook.goalflow_observation.v0.1",
        "family": hook_family(payload),
        "event": name,
        "fingerprint": event_fingerprint(payload),
        "delivery_fingerprint": delivery_fingerprint(payload),
        "payload_hash": sha256(without_volatile_delivery_fields(redact_runtime_keys(payload))),
        "redacted_payload": redact_runtime_keys(payload),
        "public_summary": public_summary(payload),
        "authority": "candidate",
    }
    if name == "PostToolUse":
        observation["candidate"] = "evidence"
        observation["tool_name"] = payload.get("tool_name")
    elif name == "Stop":
        observation["candidate"] = "finish_review"
    else:
        observation["candidate"] = "telemetry"
    return observation


def goalflow_guardrail(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    name = event_name(payload)
    required_gap = state.get("required_evidence_gap") or state.get("evidence_gap") or []
    blocker = state.get("blocked_reason") or state.get("blocker")
    pending_approvals = state.get("pending_approval_count") or len(state.get("pending_approval_ids") or [])
    decision = {
        "schema_version": "hook.guardrail_decision.v0.1",
        "system": "goalflow",
        "event": name,
        "decision": "allow",
        "reason": "no_guardrail_triggered",
    }
    if name == "Stop" and (required_gap or blocker or pending_approvals):
        decision.update({
            "decision": "nudge",
            "reason": "goalflow_finish_needs_reconcile",
            "required_action": "reconcile_required_evidence_blockers_and_approvals",
        })
    if is_pre_action_event(name):
        classification = classify_hook_action(payload)
        unsafe_attempt = classification["current_guardrail_unsafe_candidate"] or classification["approval_sensitive_candidate"]
        if unsafe_attempt and not ledger_required_visible_report_attempt(payload, state):
            decision.update({
                "decision": "block",
                "reason": "risk_action_requires_goalflow_approval",
                "required_action": "request_or_consume_owner_approval",
            })
    return decision


def load_json_file(path: str | None, default: Any) -> Any:
    if not path:
        return default
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize hook payloads into Ledger/GoalFlow observations and guardrail decisions")
    parser.add_argument("--payload", required=True, help="Hook payload JSON file")
    parser.add_argument("--state", help="Optional Ledger/GoalFlow state JSON file")
    parser.add_argument("--system", choices=["ledger", "goalflow"], required=True)
    parser.add_argument("--mode", choices=["observe", "guardrail"], required=True)
    args = parser.parse_args()

    payload = load_json_file(args.payload, {})
    state = load_json_file(args.state, {})
    if not isinstance(payload, dict):
        raise SystemExit("--payload must be a JSON object")
    if not isinstance(state, dict):
        raise SystemExit("--state must be a JSON object")
    if args.system == "ledger":
        result = ledger_observation(payload) if args.mode == "observe" else ledger_guardrail(payload, state)
    else:
        result = goalflow_observation(payload) if args.mode == "observe" else goalflow_guardrail(payload, state)
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

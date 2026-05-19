#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "hook_event_contract.py"


def run_contract(payload: dict, state: dict | None, system: str, mode: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        payload_path = Path(tmp) / "payload.json"
        state_path = Path(tmp) / "state.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        args = ["python3", str(SCRIPT), "--payload", str(payload_path), "--system", system, "--mode", mode]
        if state is not None:
            state_path.write_text(json.dumps(state), encoding="utf-8")
            args.extend(["--state", str(state_path)])
        proc = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=True)
        return json.loads(proc.stdout)["result"]


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    post_tool = {
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
    duplicate = dict(post_tool)
    first = run_contract(post_tool, None, "ledger", "observe")
    second = run_contract(duplicate, None, "ledger", "observe")
    assert_true(first["fingerprint"] == second["fingerprint"], "duplicate hook should produce stable fingerprint")
    assert_true("idempotency_key" not in json.dumps(first), "runtime idempotency_key must be redacted")
    assert_true(first["candidate_event_type"] == "tool_observation", "PostToolUse should default to tool observation")
    assert_true("session_id" not in json.dumps(first["public_summary"]), "public summary must not expose session id")
    changed_timestamp = dict(post_tool, timestamp="later")
    changed_response = {**post_tool, "tool_response": {"exit_code": 1}}
    assert_true(run_contract(changed_timestamp, None, "ledger", "observe")["fingerprint"] == first["fingerprint"], "timestamp changes should not affect dedupe fingerprint")
    assert_true(run_contract(changed_response, None, "ledger", "observe")["fingerprint"] != first["fingerprint"], "material payload changes should affect fingerprint")

    stop = {
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
    ledger_decision = run_contract(stop, {"status": "completed_unreported", "completion_report_sent": False}, "ledger", "guardrail")
    assert_true(ledger_decision["decision"] == "nudge", "completed_unreported Stop should nudge report delivery")

    unsafe_pre = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_use_id": "tool-2",
        "tool_name": "Bash",
        "tool_input": {"cmd": "launchctl kickstart -k gui/$UID/ai.openclaw.gateway"},
        "cwd": str(ROOT),
        "model": "gpt-5.5",
        "permission_mode": "dontAsk",
        "transcript_path": None,
    }
    blocked = run_contract(unsafe_pre, {"side_effect_class": "gateway_runtime", "repeat_policy": "never_repeat_without_user_approval"}, "ledger", "guardrail")
    assert_true(blocked["decision"] == "block", "unsafe gateway repeat should block")
    local_work_block = run_contract(unsafe_pre, {"status": "running", "side_effect_class": "local_files", "repeat_policy": "reconcile_first"}, "ledger", "guardrail")
    assert_true(local_work_block["decision"] == "block", "unsafe side effect inside local work should require durable clearance")
    missing_state_block = run_contract(unsafe_pre, {}, "ledger", "guardrail")
    assert_true(missing_state_block["decision"] == "block", "unsafe attempt without durable state should block")
    assert_true(missing_state_block["reason"] == "unsafe_attempt_without_durable_state", "empty Ledger state should report the missing-state reason")
    email_payload = {
        **unsafe_pre,
        "tool_use_id": "tool-email",
        "tool_input": {"cmd": "python3 scripts/gog_helper.py send-email --to user@example.com --subject hi --text hi"},
    }
    email_block = run_contract(email_payload, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(email_block["decision"] == "block", "unknown email side effect should block")
    ledger_report_payload = {
        **unsafe_pre,
        "session_id": "agent:main:telegram:direct:test-user",
        "tool_use_id": "tool-visible-report",
        "tool_name": "message",
        "tool_input": {"action": "send", "channel": "telegram", "target": "test-user", "message": "Status: 완료"},
    }
    ledger_report_allow = run_contract(
        ledger_report_payload,
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_report_allow["decision"] == "allow", "required Ledger visible report should not deadlock behind message block")
    native_message_report_allow = run_contract(
        {
            "type": "message",
            "action": "send",
            "channel": "telegram",
            "target": "test-user",
            "message": "Status: 완료",
            "sessionKey": "agent:main:telegram:direct:test-user",
        },
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(native_message_report_allow["decision"] == "allow", "native OpenClaw message:send should allow the required visible report when route/session match")
    ledger_omitted_route_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_omitted_route_block["decision"] == "block", "Ledger visible report must require an explicit route")
    ledger_omitted_prefixed_target_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "telegram:test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_omitted_prefixed_target_block["decision"] == "block", "Ledger visible report must not infer prefixed Telegram default routes")
    ledger_omitted_route_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:other-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_omitted_route_block["decision"] == "block", "Ledger visible report default route should stay owner-scoped")
    ledger_omitted_route_substring_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "session:telegram-test-user-other",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_omitted_route_substring_block["decision"] == "block", "Ledger default route should reject substring target matches")
    ledger_partial_route_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "channel": "telegram", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_partial_route_block["decision"] == "block", "Ledger visible report allow should reject partial route matches")
    ledger_session_mismatch_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "session_key": "session:other", "channel": "telegram", "target": "test-user", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_session_mismatch_block["decision"] == "block", "Ledger visible report allow should not accept unrelated route classes")
    ledger_top_level_session_mismatch_block = run_contract(
        {**ledger_report_payload, "session_id": "agent:main:telegram:direct:other-user"},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_top_level_session_mismatch_block["decision"] == "block", "Ledger visible report allow should reject top-level session owner mismatch")
    ledger_wrong_route_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "channel": "telegram", "target": "other-user", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_wrong_route_block["decision"] == "block", "required Ledger visible report allow should stay route-scoped")
    ledger_unexpected_route_extra_block = run_contract(
        {**ledger_report_payload, "tool_input": {"action": "send", "channel": "telegram", "target": "test-user", "threadId": "thread-1", "message": "Status: 완료"}},
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_unexpected_route_extra_block["decision"] == "block", "Ledger visible report allow should reject unexpected route extras")
    ledger_session_owner_allow = run_contract(
        {
            **unsafe_pre,
            "session_id": "session:phase2-review",
            "tool_use_id": "tool-ledger-session-owner",
            "tool_name": "message",
            "tool_input": {"action": "send", "channel": "telegram", "target": "test-user", "message": "Status: 완료"},
        },
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "session:phase2-review",
            "visible_delivery": {"channel": "telegram", "target": "test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_session_owner_allow["decision"] == "allow", "explicit session: owner binding plus explicit route should allow the required report")
    ledger_explicit_telegram_prefixed = {
        **unsafe_pre,
        "session_id": "agent:main:telegram:direct:test-user",
        "tool_use_id": "tool-ledger-explicit-telegram-prefix",
        "tool_name": "message",
        "tool_input": {"action": "send", "channel": "telegram", "target": "test-user", "message": "Status: 완료"},
    }
    ledger_explicit_telegram_prefixed_allow = run_contract(
        ledger_explicit_telegram_prefixed,
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "owner_session_key": "agent:main:telegram:direct:test-user",
            "visible_delivery": {"channel": "telegram", "target": "telegram:test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_explicit_telegram_prefixed_allow["decision"] == "allow", "explicit Telegram report target should normalize telegram: prefix")
    ledger_ownerless_prefixed_block = run_contract(
        ledger_explicit_telegram_prefixed,
        {
            "status": "completed_unreported",
            "completion_report_sent": False,
            "visible_delivery": {"channel": "telegram", "target": "telegram:test-user"},
        },
        "ledger",
        "guardrail",
    )
    assert_true(ledger_ownerless_prefixed_block["decision"] == "block", "Ledger visible report allow should require owner/session binding")
    message_send_block = run_contract(
        {"type": "message", "action": "send", "channel": "telegram", "target": "test-user", "message": "hi"},
        {"side_effect_status": "unknown"},
        "ledger",
        "guardrail",
    )
    assert_true(message_send_block["decision"] == "block", "OpenClaw message:send pre-action should require durable clearance")
    gh_merge_payload = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-merge",
        "tool_input": {"cmd": "gh pr merge 7 --squash"},
    }
    gh_merge_block = run_contract(gh_merge_payload, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_merge_block["decision"] == "block", "gh PR write command should block")
    gh_merge_repo_flag = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-merge-repo-flag",
        "tool_input": {"cmd": "gh pr -R owner/repo merge 7 --squash"},
    }
    gh_merge_repo_flag_block = run_contract(gh_merge_repo_flag, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_merge_repo_flag_block["decision"] == "block", "gh PR write with group repo flag should block")
    gh_issue_repo_flag_comment = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-issue-repo-flag-comment",
        "tool_input": {"cmd": "gh issue --repo owner/repo comment 3 --body hi"},
    }
    gh_issue_repo_flag_comment_block = run_contract(gh_issue_repo_flag_comment, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_issue_repo_flag_comment_block["decision"] == "block", "gh issue write with group repo flag should block")
    gh_pr_update_branch = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-pr-update-branch",
        "tool_input": {"cmd": "gh pr update-branch 7"},
    }
    gh_pr_update_branch_block = run_contract(gh_pr_update_branch, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_pr_update_branch_block["decision"] == "block", "gh pr update-branch should block")
    gh_shell_merge = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-shell-merge",
        "tool_input": {"cmd": "zsh -lc 'gh pr merge 7 --squash'"},
    }
    gh_shell_merge_block = run_contract(gh_shell_merge, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_shell_merge_block["decision"] == "block", "shell-wrapped gh PR write command should block")
    gh_env_shell_merge = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-env-shell-merge",
        "tool_input": {"cmd": "env -u TOKEN zsh -lc 'gh pr merge 7 --squash'"},
    }
    gh_env_shell_merge_block = run_contract(gh_env_shell_merge, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_env_shell_merge_block["decision"] == "block", "env-wrapped shell gh PR write command should block")
    gh_structured_shell_merge = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-structured-shell-merge",
        "tool_input": {"executable": "zsh", "args": ["-lc", "gh pr merge 7 --squash"]},
    }
    gh_structured_shell_merge_block = run_contract(gh_structured_shell_merge, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_structured_shell_merge_block["decision"] == "block", "structured shell gh PR write command should block")
    gh_repo_fork = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-repo-fork",
        "tool_input": {"cmd": "gh repo fork owner/repo --clone=false"},
    }
    gh_repo_fork_block = run_contract(gh_repo_fork, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_repo_fork_block["decision"] == "block", "gh repo fork should block")
    gh_repo_sync = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-repo-sync",
        "tool_input": {"cmd": "gh repo sync owner/repo"},
    }
    gh_repo_sync_block = run_contract(gh_repo_sync, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_repo_sync_block["decision"] == "block", "gh repo sync should block")
    gh_deploy_key_add = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-deploy-key-add",
        "tool_input": {"cmd": "gh repo deploy-key add ~/.ssh/id_rsa.pub --repo owner/repo"},
    }
    gh_deploy_key_add_block = run_contract(gh_deploy_key_add, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_deploy_key_add_block["decision"] == "block", "gh repo deploy-key add should block")
    gh_api_implicit_write = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-api-field",
        "tool_input": {"cmd": "gh api repos/owner/repo/issues/3 -f title=updated"},
    }
    gh_api_implicit_block = run_contract(gh_api_implicit_write, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_api_implicit_block["decision"] == "block", "gh api field flags should be treated as write by default")
    gh_graphql_mutation = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-api-graphql",
        "tool_input": {"cmd": "gh api graphql -f query='mutation { addStar(input:{starrableId:\"x\"}) { starrable { id } } }'"},
    }
    gh_graphql_block = run_contract(gh_graphql_mutation, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_graphql_block["decision"] == "block", "gh api GraphQL mutation via field flag should block")
    gh_secret_set = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-secret",
        "tool_input": {"cmd": "gh secret set TOKEN --body value"},
    }
    gh_secret_block = run_contract(gh_secret_set, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_secret_block["decision"] == "block", "gh secret set should block")
    gh_search_payload = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-search",
        "tool_input": {"cmd": "rg gh pr merge plugins/goalflow/index.js"},
    }
    gh_search_allow = run_contract(gh_search_payload, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_search_allow["decision"] == "allow", "searching text that mentions gh writes should not block")
    git_push_search = {
        **unsafe_pre,
        "tool_use_id": "tool-git-push-search",
        "tool_input": {"cmd": "rg git push plugins/goalflow/index.js"},
    }
    git_push_search_allow = run_contract(git_push_search, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_push_search_allow["decision"] == "allow", "searching text that mentions git push should not block")
    git_shell_push = {
        **unsafe_pre,
        "tool_use_id": "tool-git-shell-push",
        "tool_input": {"cmd": "bash -lc 'git push origin main'"},
    }
    git_shell_push_block = run_contract(git_shell_push, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_shell_push_block["decision"] == "block", "shell-wrapped git push should block")
    git_env_push = {
        **unsafe_pre,
        "tool_use_id": "tool-git-env-push",
        "tool_input": {"cmd": "env GIT_SSH_COMMAND='ssh -i k' git push origin main"},
    }
    git_env_push_block = run_contract(git_env_push, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_env_push_block["decision"] == "block", "env assignment git push should block")
    git_env_shell_push = {
        **unsafe_pre,
        "tool_use_id": "tool-git-env-shell-push",
        "tool_input": {"cmd": "env -i bash -lc 'git push origin main'"},
    }
    git_env_shell_push_block = run_contract(git_env_shell_push, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_env_shell_push_block["decision"] == "block", "env-wrapped shell git push should block")
    git_env_split_push = {
        **unsafe_pre,
        "tool_use_id": "tool-git-env-split-push",
        "tool_input": {"cmd": "env -S 'git push origin main'"},
    }
    git_env_split_push_block = run_contract(git_env_split_push, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_env_split_push_block["decision"] == "block", "env -S git push should block")
    git_exec_path_push = {
        **unsafe_pre,
        "tool_use_id": "tool-git-exec-path-push",
        "tool_input": {"cmd": "git --exec-path=/tmp/git-core push origin main"},
    }
    git_exec_path_push_block = run_contract(git_exec_path_push, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_exec_path_push_block["decision"] == "block", "git --exec-path push should block")
    source_exec_git_push = {
        **unsafe_pre,
        "tool_use_id": "tool-source-exec-git-push",
        "tool_input": {"source": "await tools.exec_command({cmd: 'git push origin main'})"},
    }
    source_exec_git_push_block = run_contract(source_exec_git_push, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(source_exec_git_push_block["decision"] == "block", "source exec_command git push should block")
    source_exec_git_push_quoted_key = {
        **unsafe_pre,
        "tool_use_id": "tool-source-exec-git-push-quoted-key",
        "tool_input": {"source": "await tools.exec_command({\"cmd\": \"git push origin main\"})"},
    }
    source_exec_git_push_quoted_key_block = run_contract(source_exec_git_push_quoted_key, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(source_exec_git_push_quoted_key_block["decision"] == "block", "source exec_command quoted cmd key should block")
    source_exec_git_push_template = {
        **unsafe_pre,
        "tool_use_id": "tool-source-exec-git-push-template",
        "tool_input": {"source": "await tools.exec_command({cmd: `git push origin main`})"},
    }
    source_exec_git_push_template_block = run_contract(source_exec_git_push_template, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(source_exec_git_push_template_block["decision"] == "block", "source exec_command template cmd should block")
    source_exec_git_push_escaped = {
        **unsafe_pre,
        "tool_use_id": "tool-source-exec-git-push-escaped",
        "tool_input": {"source": "await tools.exec_command({cmd: \\\"echo \\\\\\\"x\\\\\\\"; git push origin main\\\"})"},
    }
    source_exec_git_push_escaped_block = run_contract(source_exec_git_push_escaped, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(source_exec_git_push_escaped_block["decision"] == "block", "source exec_command escaped quotes should block")
    source_exec_gh_merge = {
        **unsafe_pre,
        "tool_use_id": "tool-source-exec-gh-merge",
        "tool_input": {"source": "await tools.exec_command({cmd: \"gh pr merge 7 --squash\"})"},
    }
    source_exec_gh_merge_block = run_contract(source_exec_gh_merge, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(source_exec_gh_merge_block["decision"] == "block", "source exec_command gh write should block")
    gh_env_split_merge = {
        **unsafe_pre,
        "tool_use_id": "tool-gh-env-split-merge",
        "tool_input": {"cmd": "env --split-string='gh pr merge 7 --squash'"},
    }
    gh_env_split_merge_block = run_contract(gh_env_split_merge, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(gh_env_split_merge_block["decision"] == "block", "env --split-string gh write should block")
    git_env_attached_split = {
        **unsafe_pre,
        "tool_use_id": "tool-git-env-attached-split",
        "tool_input": {"cmd": "env -S'git push origin main'"},
    }
    git_env_attached_split_block = run_contract(git_env_attached_split, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_env_attached_split_block["decision"] == "block", "env attached -S git push should block")
    git_structured_shell_push = {
        **unsafe_pre,
        "tool_use_id": "tool-git-structured-shell-push",
        "tool_input": {"cmd": "bash", "args": ["-lc", "git push origin main"]},
    }
    git_structured_shell_push_block = run_contract(git_structured_shell_push, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_structured_shell_push_block["decision"] == "block", "structured shell git push should block")
    git_empty_args_multi_command = {
        **unsafe_pre,
        "tool_use_id": "tool-git-empty-args-multi",
        "tool_input": {"cmd": "git status && git push origin main", "args": []},
    }
    git_empty_args_multi_block = run_contract(git_empty_args_multi_command, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_empty_args_multi_block["decision"] == "block", "cmd strings with empty args should still be segmented")
    git_quoted_search = {
        **unsafe_pre,
        "tool_use_id": "tool-git-quoted-search",
        "tool_input": {"cmd": "rg 'git push && gh pr merge' plugins/goalflow/index.js"},
    }
    git_quoted_search_allow = run_contract(git_quoted_search, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(git_quoted_search_allow["decision"] == "allow", "quoted search strings with separators should not become fake commands")
    rm_fr_payload = {
        **unsafe_pre,
        "tool_use_id": "tool-rm-fr",
        "tool_input": {"cmd": "rm -fr tmp/x"},
    }
    rm_fr_block = run_contract(rm_fr_payload, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(rm_fr_block["decision"] == "block", "rm -fr should block")
    rm_split_flags_payload = {
        **unsafe_pre,
        "tool_use_id": "tool-rm-split-flags",
        "tool_input": {"cmd": "rm -r -f tmp/x"},
    }
    rm_split_flags_block = run_contract(rm_split_flags_payload, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(rm_split_flags_block["decision"] == "block", "rm -r -f should block")
    unsafe_literal_search = {
        **unsafe_pre,
        "tool_use_id": "tool-unsafe-literal-search",
        "tool_input": {"cmd": "rg 'browser open' AGENTS.md && rg 'rm -rf' AGENTS.md"},
    }
    unsafe_literal_search_allow = run_contract(unsafe_literal_search, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(unsafe_literal_search_allow["decision"] == "allow", "searching unsafe literal text should not block")
    github_write_call_search = {
        **unsafe_pre,
        "tool_use_id": "tool-github-write-call-search",
        "tool_input": {"cmd": "rg 'tools.mcp__codex_apps__github_update_file(' scripts tests"},
    }
    github_write_call_search_allow = run_contract(github_write_call_search, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(github_write_call_search_allow["decision"] == "allow", "searching GitHub write call text should not block")
    argv_payload = {
        **unsafe_pre,
        "tool_use_id": "tool-argv",
        "tool_input": {"cmd": "launchctl", "args": ["kickstart", "-k", "gui/$UID/ai.openclaw.gateway"]},
    }
    argv_block = run_contract(argv_payload, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(argv_block["decision"] == "block", "structured argv unsafe side effect should block")
    spaced_payload = {
        **unsafe_pre,
        "tool_use_id": "tool-spaced",
        "tool_input": {"cmd": "launchctl    kickstart    -k gui/$UID/ai.openclaw.gateway"},
    }
    spaced_block = run_contract(spaced_payload, {"side_effect_status": "unknown"}, "ledger", "guardrail")
    assert_true(spaced_block["decision"] == "block", "whitespace-variant unsafe side effect should block")
    secret_payload = {
        **post_tool,
        "tool_input": {"cmd": "notify --idempotency-key runtime-secret --blocked-idempotency-key=hidden", "idempotencyKey": "camel-secret"},
    }
    secret_observation = run_contract(secret_payload, None, "ledger", "observe")
    secret_summary = secret_observation["public_summary"]
    assert_true("runtime-secret" not in json.dumps(secret_summary) and "hidden" not in json.dumps(secret_summary), "public command preview should redact runtime keys")
    assert_true(
        "runtime-secret" not in json.dumps(secret_observation)
        and "hidden" not in json.dumps(secret_observation)
        and "camel-secret" not in json.dumps(secret_observation),
        "redacted hook observation should not retain runtime key values",
    )

    goal_stop = run_contract(stop, {"evidence_gap": ["tests-pass"], "pending_approval_count": 0}, "goalflow", "guardrail")
    assert_true(goal_stop["decision"] == "nudge", "GoalFlow Stop with evidence gap should nudge")
    goal_block = run_contract(unsafe_pre, {"approval_clearance": False}, "goalflow", "guardrail")
    assert_true(goal_block["decision"] == "block", "GoalFlow risky action without consumed approval should block")
    out_of_order_goal = run_contract(stop, {"evidence_gap": ["tests-pass"]}, "goalflow", "guardrail")
    late_tool = run_contract(post_tool, None, "goalflow", "observe")
    assert_true(out_of_order_goal["decision"] == "nudge" and late_tool["authority"] == "candidate", "out-of-order Stop/PostToolUse should stay non-authoritative")

    print(json.dumps({
        "ok": True,
        "smokes": {
            "duplicate_fingerprint_stable": first["fingerprint"],
            "ledger_stop_nudge": ledger_decision["reason"],
            "ledger_unsafe_block": blocked["reason"],
            "ledger_missing_state_block": missing_state_block["reason"],
            "ledger_visible_report_allow": ledger_report_allow["reason"],
            "ledger_gh_write_block": gh_merge_block["reason"],
            "ledger_gh_api_implicit_write_block": gh_api_implicit_block["reason"],
            "ledger_structured_argv_block": argv_block["reason"],
            "goalflow_stop_nudge": goal_stop["reason"],
            "goalflow_risk_block": goal_block["reason"],
        },
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

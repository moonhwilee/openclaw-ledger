#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fake_openclaw="$TMP/openclaw"
fake_launchctl="$TMP/launchctl"
cat >"$fake_openclaw" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-} ${2:-} ${3:-}" == "channels status --json" ]]; then
  echo '{"channelAccounts":{"telegram":[{"connected":true}]}}'
  exit 0
fi
if [[ "${1:-}" == "sessions" ]]; then
  echo '{"sessions":[{"key":"agent:main:telegram:direct:test-user","updatedAt":1,"abortedLastRun":false}]}'
  exit 0
fi
if [[ "${1:-} ${2:-}" == "cron list" ]]; then
  echo '[]'
  exit 0
fi
if [[ "${1:-} ${2:-}" == "tasks list" ]]; then
  echo '{"tasks":[]}'
  exit 0
fi
if [[ "${1:-} ${2:-}" == "system event" ]]; then
  echo '{"ok":true}'
  exit 0
fi
echo "unexpected fake openclaw command: $*" >&2
exit 1
SH
chmod 700 "$fake_openclaw"
cat >"$fake_launchctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${OPENCLAW_LEDGER_LAUNCHCTL_LOG:?}"
exit 0
SH
chmod 700 "$fake_launchctl"

run_install() {
  local install_home="$1"
  shift
  mkdir -p "$install_home/home"
  OPENCLAW_LEDGER_RAW_BASE_URL="file://$ROOT" \
  OPENCLAW_LEDGER_INSTALL_DIR="$install_home/bin" \
  OPENCLAW_LEDGER_HOME="$install_home/ledger" \
  OPENCLAW_LEDGER_LOG_DIR="$install_home/logs" \
  OPENCLAW_LEDGER_LAUNCH_AGENT_LABEL="com.openclaw.ledger.watchdog.smoke" \
  OPENCLAW_BIN="$fake_openclaw" \
  OPENCLAW_WORKSPACE="$install_home/workspace" \
  OPENCLAW_LEDGER_LAUNCHCTL_LOG="${OPENCLAW_LEDGER_LAUNCHCTL_LOG:-$install_home/launchctl.log}" \
  HOME="$install_home/home" \
  PATH="$TMP:$PATH" \
  bash "$ROOT/install.sh" "$@"
}

cli_home="$TMP/cli"
run_install "$cli_home" --cli-only >"$TMP/openclaw-ledger-install-cli.out"
test -x "$cli_home/bin/openclaw-ledger"
test -x "$cli_home/bin/hook_event_contract.py"
test ! -e "$cli_home/ledger/config.json"

full_home="$TMP/full"
mkdir -p "$full_home/logs" "$full_home/workspace"
chmod 755 "$full_home/logs" "$full_home/workspace"
run_install "$full_home" --no-launch-agent >"$TMP/openclaw-ledger-install-full.out"
test -x "$full_home/bin/openclaw-ledger"
test -x "$full_home/ledger/work_ledger_watchdog_runner.py"
test -s "$full_home/ledger/prompts/work-ledger-watchdog.md"
cmp "$full_home/ledger/prompts/work-ledger-watchdog.md" "$ROOT/prompts/work-ledger-watchdog.md"
grep -q "LaunchAgent: skipped" "$TMP/openclaw-ledger-install-full.out"
python3 - "$full_home/ledger/config.json" "$full_home" <<'PY'
import json
import os
import subprocess
import stat
import sys

config_path = sys.argv[1]
full_home = sys.argv[2]
with open(config_path, encoding="utf-8") as fh:
    config = json.load(fh)
assert config["fallback_session_key"] == "agent:main:telegram:direct:test-user"
assert config["visible_delivery"]["target"] == "test-user"
assert "accountId" not in config["visible_delivery"]
assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o600
assert stat.S_IMODE(os.stat(os.path.join(full_home, "ledger")).st_mode) == 0o700
assert stat.S_IMODE(os.stat(os.path.join(full_home, "logs")).st_mode) == 0o755
assert stat.S_IMODE(os.stat(os.path.join(full_home, "workspace")).st_mode) == 0o755
ledger = os.path.join(full_home, "bin", "openclaw-ledger")
root = os.path.join(full_home, "workspace")
work_id = "install-visible-report-route"

def run(*args):
    proc = subprocess.run([ledger, "--root", root, *args], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"{args} failed\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return json.loads(proc.stdout)

run(
    "start",
    "--work-id", work_id,
    "--owner-session-key", config["fallback_session_key"],
    "--visible-delivery", json.dumps(config["visible_delivery"]),
    "--request-summary", "install visible route proof",
    "--checklist", json.dumps(["complete", "report"]),
    "--success-criteria", json.dumps(["installed route closes after sent proof"]),
)
run("complete", "--work-id", work_id, "--note", "done")
send = run(
    "hook-observe",
    "--work-id", work_id,
    "--payload", json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": config["fallback_session_key"],
        "tool_use_id": "install-visible-send",
        "tool_name": "message",
        "tool_input": {
            "action": "send",
            "channel": "telegram",
            "target": config["visible_delivery"]["target"],
            "message": "Status: 완료",
        },
    }),
)
assert send.get("recorded_completion_report_send") is True
sent = run(
    "hook-observe",
    "--work-id", work_id,
    "--payload", json.dumps({
        "type": "message",
        "action": "sent",
        "channel": "telegram",
        "target": config["visible_delivery"]["target"],
        "sessionKey": config["fallback_session_key"],
        "tool_use_id": "install-visible-send",
        "messageId": "install-visible-message",
    }),
)
assert sent.get("recorded_report_sent") is True
state = run("state", "--work-id", work_id)["items"][0]
assert state["status"] == "reported"

complete_reported_id = "install-complete-reported"
run(
    "start",
    "--work-id", complete_reported_id,
    "--owner-session-key", config["fallback_session_key"],
    "--visible-delivery", json.dumps(config["visible_delivery"]),
    "--request-summary", "install complete-reported proof",
    "--checklist", json.dumps(["complete", "report"]),
    "--success-criteria", json.dumps(["installed complete-reported closes cleanly"]),
)
run(
    "complete-reported",
    "--work-id", complete_reported_id,
    "--visible-delivery", json.dumps(config["visible_delivery"]),
    "--delivery-message-id", "install-complete-reported-message",
    "--note", "done",
)
complete_reported_state = run("state", "--work-id", complete_reported_id)["items"][0]
assert complete_reported_state["status"] == "reported"
assert complete_reported_state["visible_delivery_proof"]["message_id"] == "install-complete-reported-message"
PY

bad_home="$TMP/bad"
if run_install "$bad_home" --session-key $'agent:main:telegram:direct:test user\nsecond' --no-launch-agent >"$TMP/openclaw-ledger-install-bad.out" 2>"$TMP/openclaw-ledger-install-bad.err"; then
  echo "malformed session key unexpectedly succeeded" >&2
  exit 1
fi
test ! -e "$bad_home/bin/openclaw-ledger"

default_home="$TMP/default"
run_install "$default_home" >"$TMP/openclaw-ledger-install-default.out"
plist="$default_home/home/Library/LaunchAgents/com.openclaw.ledger.watchdog.smoke.plist"
test -s "$plist"
python3 - "$plist" "$default_home" <<'PY'
import plistlib
import sys

plist = plistlib.load(open(sys.argv[1], "rb"))
home = sys.argv[2]
assert plist["ProgramArguments"][0].startswith("/")
assert plist["ProgramArguments"][1] == f"{home}/ledger/work_ledger_watchdog_runner.py"
assert plist["EnvironmentVariables"]["OPENCLAW_LEDGER_CONFIG"] == f"{home}/ledger/config.json"
assert plist["StartInterval"] == 600
PY
grep -Eq "bootout gui/.*/com\.openclaw\.ledger\.watchdog\.smoke\.plist" "$default_home/launchctl.log"
grep -Eq "bootstrap gui/.*/com\.openclaw\.ledger\.watchdog\.smoke\.plist" "$default_home/launchctl.log"
grep -Eq "kickstart -k gui/.*/com\.openclaw\.ledger\.watchdog\.smoke" "$default_home/launchctl.log"

cat >"$fake_launchctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${OPENCLAW_LEDGER_LAUNCHCTL_LOG:?}"
if [[ "${1:-}" == "bootstrap" ]]; then
  echo "simulated bootstrap failure" >&2
  exit 1
fi
exit 0
SH
chmod 700 "$fake_launchctl"

rollback_home="$TMP/rollback"
mkdir -p "$rollback_home/bin" "$rollback_home/ledger/prompts" "$rollback_home/home/Library/LaunchAgents"
printf 'old-ledger\n' >"$rollback_home/bin/openclaw-ledger"
printf 'old-hook\n' >"$rollback_home/bin/hook_event_contract.py"
printf 'old-runner\n' >"$rollback_home/ledger/work_ledger_watchdog_runner.py"
printf 'old-prompt\n' >"$rollback_home/ledger/prompts/work-ledger-watchdog.md"
printf '{"old":true}\n' >"$rollback_home/ledger/config.json"
printf 'old-plist\n' >"$rollback_home/home/Library/LaunchAgents/com.openclaw.ledger.watchdog.smoke.plist"
if run_install "$rollback_home" --session-key agent:main:telegram:direct:test-user >"$TMP/openclaw-ledger-install-rollback.out" 2>"$TMP/openclaw-ledger-install-rollback.err"; then
  echo "launchctl bootstrap failure unexpectedly succeeded" >&2
  exit 1
fi
grep -q '^old-ledger$' "$rollback_home/bin/openclaw-ledger"
grep -q '^old-hook$' "$rollback_home/bin/hook_event_contract.py"
grep -q '^old-runner$' "$rollback_home/ledger/work_ledger_watchdog_runner.py"
grep -q '^old-prompt$' "$rollback_home/ledger/prompts/work-ledger-watchdog.md"
grep -q '"old":true' "$rollback_home/ledger/config.json"
grep -q '^old-plist$' "$rollback_home/home/Library/LaunchAgents/com.openclaw.ledger.watchdog.smoke.plist"

python3 -m json.tool "$full_home/ledger/config.json" >/dev/null
"$full_home/bin/openclaw-ledger" --help >/dev/null
"$full_home/bin/openclaw-ledger" watchdog-check --include-cron >/dev/null

deploy_home="$TMP/deploy-local"
mkdir -p "$deploy_home/bin" "$deploy_home/ledger"
OPENCLAW_LEDGER_INSTALL_DIR="$deploy_home/bin" \
OPENCLAW_LEDGER_HOME="$deploy_home/ledger" \
OPENCLAW_LEDGER_RUNNER_PATH="$deploy_home/ledger/work_ledger_watchdog_runner.py" \
OPENCLAW_LEDGER_PROMPT_PATH="$deploy_home/ledger/prompts/work-ledger-watchdog.md" \
  bash "$ROOT/scripts/deploy-local.sh" >"$TMP/openclaw-ledger-deploy-local.out"
test -x "$deploy_home/bin/openclaw-ledger"
test -x "$deploy_home/bin/hook_event_contract.py"
test -x "$deploy_home/ledger/work_ledger_watchdog_runner.py"
cmp "$deploy_home/bin/openclaw-ledger" "$ROOT/src/work_ledger.py"
cmp "$deploy_home/bin/hook_event_contract.py" "$ROOT/src/hook_event_contract.py"
cmp "$deploy_home/ledger/work_ledger_watchdog_runner.py" "$ROOT/scripts/work_ledger_watchdog_runner.py"
cmp "$deploy_home/ledger/prompts/work-ledger-watchdog.md" "$ROOT/prompts/work-ledger-watchdog.md"
python3 - "$deploy_home/bin/openclaw-ledger.deploy.json" "$ROOT" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

stamp = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(sys.argv[2])
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())
assert stamp["component"] == "openclaw-ledger"
assert stamp["source_repo"] == str(root)
assert stamp["commit"] == head
assert stamp["dirty"] is dirty
assert stamp["deployed_at"].endswith("Z")
PY

echo '{"ok":true,"checked":["cli-only-install","full-no-launch-agent-install","prompt-source-parity","deploy-local-stamp-and-parity","default-launchagent-install","permissions","bad-session-key-no-partial-install","launchagent-failure-rollback"]}'

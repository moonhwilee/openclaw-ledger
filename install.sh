#!/usr/bin/env bash
set -euo pipefail
umask 077

RAW_BASE_URL="${OPENCLAW_LEDGER_RAW_BASE_URL:-https://raw.githubusercontent.com/moonhwilee/openclaw-ledger/main}"
INSTALL_DIR="${OPENCLAW_LEDGER_INSTALL_DIR:-$HOME/.openclaw/bin}"
LEDGER_HOME="${OPENCLAW_LEDGER_HOME:-$HOME/.openclaw/ledger}"
INSTALL_PATH="$INSTALL_DIR/openclaw-ledger"
HOOK_PATH="$INSTALL_DIR/hook_event_contract.py"
RUNNER_PATH="$LEDGER_HOME/work_ledger_watchdog_runner.py"
PROMPT_PATH="$LEDGER_HOME/prompts/work-ledger-watchdog.md"
CONFIG_PATH="$LEDGER_HOME/config.json"
STATE_PATH="$LEDGER_HOME/state/watchdog-runner-state.json"
LOG_DIR="${OPENCLAW_LEDGER_LOG_DIR:-$HOME/.openclaw/logs}"
LAUNCH_AGENT_LABEL="${OPENCLAW_LEDGER_LAUNCH_AGENT_LABEL:-com.openclaw.ledger.watchdog}"
LAUNCH_AGENT_PATH="$HOME/Library/LaunchAgents/$LAUNCH_AGENT_LABEL.plist"
INTERVAL_SECONDS="${OPENCLAW_LEDGER_INTERVAL_SECONDS:-600}"
CLI_ONLY="${OPENCLAW_LEDGER_CLI_ONLY:-0}"
SESSION_KEY="${OPENCLAW_LEDGER_SESSION_KEY:-${OPENCLAW_LEDGER_OWNER_SESSION_KEY:-}}"
OPENCLAW_BIN="${OPENCLAW_BIN:-}"
PYTHON_BIN="${PYTHON_BIN:-}"
WORKSPACE="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
TMP_DIR=""

usage() {
  cat <<'MSG'
Usage:
  install.sh [--session-key SESSION_KEY] [--cli-only] [--no-launch-agent]

Default install is the full OpenClaw recovery setup:
  - openclaw-ledger CLI
  - hook_event_contract.py
  - watchdog runner
  - packaged recovery prompt
  - ~/.openclaw/ledger/config.json
  - macOS LaunchAgent running every 10 minutes

Requirements for full setup:
  - OpenClaw CLI installed
  - OpenClaw Telegram channel connected
  - exactly one recent main Telegram direct session, or --session-key provided

Use --cli-only only when you want the CLI without automatic recovery wakeups.
MSG
}

NO_LAUNCH_AGENT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --session-key)
      SESSION_KEY="${2:-}"
      shift 2
      ;;
    --cli-only)
      CLI_ONLY=1
      shift
      ;;
    --no-launch-agent)
      NO_LAUNCH_AGENT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}
PYTHON_BIN="$(command -v python3)"

if [[ -z "$OPENCLAW_BIN" ]]; then
  OPENCLAW_BIN="$(command -v openclaw || true)"
fi

if ! python3 - "$LAUNCH_AGENT_LABEL" "$INTERVAL_SECONDS" <<'PY'
import re
import sys

label = sys.argv[1]
interval = sys.argv[2]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}", label):
    raise SystemExit("invalid LaunchAgent label")
try:
    interval_int = int(interval)
except ValueError:
    raise SystemExit("interval must be an integer")
if interval_int < 60:
    raise SystemExit("interval must be at least 60 seconds")
PY
then
  echo "Invalid LaunchAgent configuration. Label must be plist-safe and interval must be an integer >= 60." >&2
  exit 1
fi

WORKSPACE_EXISTED=0
LOG_DIR_EXISTED=0
if [ -d "$WORKSPACE" ]; then
  WORKSPACE_EXISTED=1
fi
if [ -d "$LOG_DIR" ]; then
  LOG_DIR_EXISTED=1
fi
mkdir -p "$INSTALL_DIR" "$LEDGER_HOME/prompts" "$LEDGER_HOME/state" "$LOG_DIR" "$WORKSPACE"
chmod 700 "$LEDGER_HOME" "$LEDGER_HOME/prompts" "$LEDGER_HOME/state"
if [ "$LOG_DIR_EXISTED" = "0" ]; then
  chmod 700 "$LOG_DIR"
fi
if [ "$WORKSPACE_EXISTED" = "0" ]; then
  chmod 700 "$WORKSPACE"
fi
TMP_DIR="$(mktemp -d "$INSTALL_DIR/openclaw-ledger-install.XXXXXX")"
ROLLBACK_ACTIVE=0
ROLLBACK_LAUNCH_AGENT=0
BACKUP_DIR="$TMP_DIR/backup"
mkdir -p "$BACKUP_DIR"
cleanup_exit() {
  local status=$?
  if [[ "$status" -ne 0 && "${ROLLBACK_ACTIVE:-0}" == "1" ]]; then
    rollback_install || true
  fi
  rm -rf "$TMP_DIR"
  exit "$status"
}
trap cleanup_exit EXIT
TMP_LEDGER="$TMP_DIR/openclaw-ledger"
TMP_HOOK="$TMP_DIR/hook_event_contract.py"
TMP_RUNNER="$TMP_DIR/work_ledger_watchdog_runner.py"
TMP_PROMPT="$TMP_DIR/work-ledger-watchdog.md"
TMP_CONFIG="$TMP_DIR/config.json"
TMP_PLIST="$TMP_DIR/$LAUNCH_AGENT_LABEL.plist"

backup_path() {
  local path="$1"
  local key="$2"
  if [[ -e "$path" ]]; then
    cp -p "$path" "$BACKUP_DIR/$key"
    echo present >"$BACKUP_DIR/$key.state"
  else
    echo absent >"$BACKUP_DIR/$key.state"
  fi
}

restore_path() {
  local path="$1"
  local key="$2"
  if [[ "$(cat "$BACKUP_DIR/$key.state" 2>/dev/null || echo absent)" == "present" ]]; then
    mkdir -p "$(dirname "$path")"
    mv "$BACKUP_DIR/$key" "$path"
  else
    rm -f "$path"
  fi
}

rollback_install() {
  if [[ "$ROLLBACK_LAUNCH_AGENT" == "1" ]]; then
    launchctl bootout "gui/$UID" "$LAUNCH_AGENT_PATH" >/dev/null 2>&1 || true
  fi
  restore_path "$INSTALL_PATH" ledger
  restore_path "$HOOK_PATH" hook
  restore_path "$RUNNER_PATH" runner
  restore_path "$PROMPT_PATH" prompt
  restore_path "$CONFIG_PATH" config
  if [[ "$ROLLBACK_LAUNCH_AGENT" == "1" ]]; then
    restore_path "$LAUNCH_AGENT_PATH" launchagent
    if [[ -e "$LAUNCH_AGENT_PATH" ]]; then
      launchctl bootstrap "gui/$UID" "$LAUNCH_AGENT_PATH" >/dev/null 2>&1 || true
    fi
  fi
}

fetch() {
  local url="$1"
  local dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
  else
    python3 - "$url" "$dest" <<'PY'
import sys
from urllib.request import urlopen

with urlopen(sys.argv[1], timeout=30) as response:
    data = response.read()
with open(sys.argv[2], "wb") as handle:
    handle.write(data)
PY
  fi
}

fetch "$RAW_BASE_URL/src/work_ledger.py" "$TMP_LEDGER"
fetch "$RAW_BASE_URL/src/hook_event_contract.py" "$TMP_HOOK"
fetch "$RAW_BASE_URL/scripts/work_ledger_watchdog_runner.py" "$TMP_RUNNER"
fetch "$RAW_BASE_URL/prompts/work-ledger-watchdog.md" "$TMP_PROMPT"

python3 -m py_compile "$TMP_LEDGER" "$TMP_HOOK" "$TMP_RUNNER"
chmod 755 "$TMP_LEDGER" "$TMP_HOOK" "$TMP_RUNNER"
"$TMP_LEDGER" --help >/dev/null

if [[ "$CLI_ONLY" == "1" ]]; then
  mv "$TMP_LEDGER" "$INSTALL_PATH"
  mv "$TMP_HOOK" "$HOOK_PATH"
  chmod 755 "$INSTALL_PATH" "$HOOK_PATH"
  cat <<MSG

OpenClaw Ledger CLI-only install complete:
  $INSTALL_PATH
  $HOOK_PATH

Automatic recovery wakeups were not installed. Re-run without --cli-only for the
full recovery package after OpenClaw Telegram is connected.
MSG
  exit 0
fi

if [[ -z "$OPENCLAW_BIN" || ! -x "$OPENCLAW_BIN" ]]; then
  echo "OpenClaw CLI is required for full recovery setup. Use --cli-only or install OpenClaw first." >&2
  exit 1
fi

CHANNEL_STATUS="$("$OPENCLAW_BIN" channels status --json)"
SESSION_KEY="$(
  python3 - "$SESSION_KEY" "$OPENCLAW_BIN" "$CHANNEL_STATUS" <<'PY'
import json
import subprocess
import sys

explicit = sys.argv[1].strip()
openclaw = sys.argv[2]
status = json.loads(sys.argv[3])
accounts = status.get("channelAccounts", {}).get("telegram") or []
if not any(account.get("connected") for account in accounts if isinstance(account, dict)):
    raise SystemExit("OpenClaw Telegram must be connected before installing full Ledger recovery.")
if explicit:
    print(explicit)
    raise SystemExit(0)

sessions_raw = subprocess.check_output(
    [openclaw, "sessions", "--json", "--agent", "main", "--limit", "50"],
    text=True,
)
sessions = json.loads(sessions_raw).get("sessions") or []
candidates = []
for item in sessions:
    key = str(item.get("key") or "")
    if not key.startswith("agent:main:telegram:direct:"):
        continue
    if key.endswith(":heartbeat") or ":subagent:" in key:
        continue
    if item.get("abortedLastRun"):
        continue
    candidates.append((int(item.get("updatedAt") or 0), key))
candidates.sort(reverse=True)
keys = [key for _, key in candidates]
if len(keys) == 1:
    print(keys[0])
    raise SystemExit(0)
if not keys:
    raise SystemExit("Could not find a main Telegram direct session. Re-run with --session-key agent:main:telegram:direct:<id>.")
raise SystemExit(
    "Multiple Telegram direct sessions found. Re-run with --session-key and one of:\n  "
    + "\n  ".join(keys[:10])
)
PY
)"

TELEGRAM_TARGET="${SESSION_KEY##agent:main:telegram:direct:}"
if [[ -z "$TELEGRAM_TARGET" || "$TELEGRAM_TARGET" == "$SESSION_KEY" || "$TELEGRAM_TARGET" == *:* ]]; then
  echo "Session key must look like agent:main:telegram:direct:<target>." >&2
  exit 1
fi
if ! python3 - "$SESSION_KEY" <<'PY'
import re
import sys

key = sys.argv[1]
if not re.fullmatch(r"agent:main:telegram:direct:[A-Za-z0-9_.@+-]{1,128}", key):
    raise SystemExit(1)
PY
then
  echo "Session key contains unsupported characters. Expected agent:main:telegram:direct:<target>." >&2
  exit 1
fi

python3 - "$TMP_CONFIG" "$WORKSPACE" "$INSTALL_PATH" "$OPENCLAW_BIN" "$PROMPT_PATH" "$STATE_PATH" "$SESSION_KEY" "$TELEGRAM_TARGET" "$LAUNCH_AGENT_LABEL" "$INTERVAL_SECONDS" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
config = {
    "workspace": sys.argv[2],
    "ledger_path": sys.argv[3],
    "openclaw_path": sys.argv[4],
    "prompt_path": sys.argv[5],
    "state_path": sys.argv[6],
    "fallback_session_key": sys.argv[7],
    "visible_delivery": {
        "channel": "telegram",
        "target": sys.argv[8],
    },
    "launch_agent_label": sys.argv[9],
    "interval_seconds": int(sys.argv[10]),
    "wake_suppression_seconds": 1800,
}
config_path.parent.mkdir(parents=True, exist_ok=True)
tmp = config_path.with_suffix(config_path.suffix + ".tmp")
tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.chmod(0o600)
tmp.replace(config_path)
config_path.chmod(0o600)
PY

if [[ "$NO_LAUNCH_AGENT" != "1" ]]; then
  mkdir -p "$(dirname "$LAUNCH_AGENT_PATH")"
  python3 - "$TMP_PLIST" "$LAUNCH_AGENT_LABEL" "$RUNNER_PATH" "$CONFIG_PATH" "$INTERVAL_SECONDS" "$LOG_DIR" "$PYTHON_BIN" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
label = sys.argv[2]
runner = sys.argv[3]
config = sys.argv[4]
interval = int(sys.argv[5])
log_dir = Path(sys.argv[6]).expanduser()
python_bin = sys.argv[7]
plist = {
    "Label": label,
    "ProgramArguments": [python_bin, runner],
    "EnvironmentVariables": {"OPENCLAW_LEDGER_CONFIG": config},
    "RunAtLoad": True,
    "StartInterval": interval,
    "StandardOutPath": str(log_dir / "work-ledger-watchdog.log"),
    "StandardErrorPath": str(log_dir / "work-ledger-watchdog.err.log"),
}
path.write_bytes(plistlib.dumps(plist, sort_keys=True))
PY
fi

backup_path "$INSTALL_PATH" ledger
backup_path "$HOOK_PATH" hook
backup_path "$RUNNER_PATH" runner
backup_path "$PROMPT_PATH" prompt
backup_path "$CONFIG_PATH" config
backup_path "$LAUNCH_AGENT_PATH" launchagent
ROLLBACK_ACTIVE=1

mv "$TMP_LEDGER" "$INSTALL_PATH"
mv "$TMP_HOOK" "$HOOK_PATH"
mv "$TMP_RUNNER" "$RUNNER_PATH"
mv "$TMP_PROMPT" "$PROMPT_PATH"
mv "$TMP_CONFIG" "$CONFIG_PATH"
chmod 755 "$INSTALL_PATH" "$HOOK_PATH" "$RUNNER_PATH"
chmod 600 "$CONFIG_PATH"
"$INSTALL_PATH" --help >/dev/null

if [[ "$NO_LAUNCH_AGENT" != "1" ]]; then
  ROLLBACK_LAUNCH_AGENT=1
  mv "$TMP_PLIST" "$LAUNCH_AGENT_PATH"
  launchctl bootout "gui/$UID" "$LAUNCH_AGENT_PATH" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID" "$LAUNCH_AGENT_PATH"
  launchctl kickstart -k "gui/$UID/$LAUNCH_AGENT_LABEL" >/dev/null 2>&1 || true
  LAUNCH_AGENT_STATUS="$LAUNCH_AGENT_PATH"
  LAUNCH_AGENT_VERIFY="  launchctl print gui/$UID/$LAUNCH_AGENT_LABEL"
else
  LAUNCH_AGENT_STATUS="skipped (--no-launch-agent)"
  LAUNCH_AGENT_VERIFY="  # LaunchAgent install skipped"
fi

python3 -m py_compile "$RUNNER_PATH"
"$INSTALL_PATH" watchdog-check --include-cron >/dev/null
ROLLBACK_ACTIVE=0

cat <<MSG

OpenClaw Ledger recovery package installed:
  CLI: $INSTALL_PATH
  Hook: $HOOK_PATH
  Runner: $RUNNER_PATH
  Prompt: $PROMPT_PATH
  Config: $CONFIG_PATH
  LaunchAgent: $LAUNCH_AGENT_STATUS

Verify:
  $INSTALL_PATH --help
  $INSTALL_PATH watchdog-check --include-cron
$LAUNCH_AGENT_VERIFY

Note: macOS LaunchAgent checks do not run while the Mac is asleep; they resume
after login/wake.
MSG

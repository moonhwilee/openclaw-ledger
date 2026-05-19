#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${OPENCLAW_LEDGER_INSTALL_DIR:-$HOME/.openclaw/bin}"
INSTALL_PATH="$INSTALL_DIR/openclaw-ledger"
HOOK_PATH="$INSTALL_DIR/hook_event_contract.py"
STAMP_PATH="$INSTALL_DIR/openclaw-ledger.deploy.json"
RUNNER_SRC="$ROOT/scripts/work_ledger_watchdog_runner.py"
if [[ -n "${OPENCLAW_LEDGER_RUNNER_PATH:-}" ]]; then
  RUNNER_PATH="$OPENCLAW_LEDGER_RUNNER_PATH"
elif [[ -d "/Users/moon/.openclaw/workspace/scripts" ]]; then
  RUNNER_PATH="/Users/moon/.openclaw/workspace/scripts/work_ledger_watchdog_runner.py"
else
  RUNNER_PATH="$INSTALL_DIR/work_ledger_watchdog_runner.py"
fi
TMP_DIR=""

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}

mkdir -p "$INSTALL_DIR"
TMP_DIR="$(mktemp -d "$INSTALL_DIR/openclaw-ledger-deploy.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
TMP_LEDGER="$TMP_DIR/openclaw-ledger"
TMP_HOOK="$TMP_DIR/hook_event_contract.py"
TMP_RUNNER="$TMP_DIR/work_ledger_watchdog_runner.py"
TMP_STAMP="$TMP_DIR/openclaw-ledger.deploy.json"

cp "$ROOT/src/work_ledger.py" "$TMP_LEDGER"
cp "$ROOT/src/hook_event_contract.py" "$TMP_HOOK"
cp "$RUNNER_SRC" "$TMP_RUNNER"
python3 -m py_compile "$TMP_HOOK" "$TMP_LEDGER" "$TMP_RUNNER"
chmod 755 "$TMP_LEDGER" "$TMP_HOOK" "$TMP_RUNNER"

"$TMP_LEDGER" --help >/dev/null

python3 - "$ROOT" "$TMP_STAMP" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
stamp_path = Path(sys.argv[2])

def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

stamp = {
    "component": "openclaw-ledger",
    "source_repo": str(root),
    "commit": git("rev-parse", "HEAD"),
    "dirty": bool(git("status", "--porcelain")),
    "deployed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
stamp_path.write_text(json.dumps(stamp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

mv "$TMP_LEDGER" "$INSTALL_PATH"
mv "$TMP_HOOK" "$HOOK_PATH"
mkdir -p "$(dirname "$RUNNER_PATH")"
mv "$TMP_RUNNER" "$RUNNER_PATH"
mv "$TMP_STAMP" "$STAMP_PATH"

cat <<MSG
OpenClaw Ledger local deploy complete:
  $INSTALL_PATH
  $HOOK_PATH
  $RUNNER_PATH
  $STAMP_PATH
MSG

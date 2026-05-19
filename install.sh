#!/usr/bin/env bash
set -euo pipefail

RAW_BASE_URL="https://raw.githubusercontent.com/moonhwilee/openclaw-ledger/main/src"
INSTALL_DIR="${OPENCLAW_LEDGER_INSTALL_DIR:-$HOME/.openclaw/bin}"
INSTALL_PATH="$INSTALL_DIR/openclaw-ledger"
HOOK_PATH="$INSTALL_DIR/hook_event_contract.py"
TMP_DIR=""

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}

mkdir -p "$INSTALL_DIR"
TMP_DIR="$(mktemp -d "$INSTALL_DIR/openclaw-ledger-install.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
TMP_LEDGER="$TMP_DIR/openclaw-ledger"
TMP_HOOK="$TMP_DIR/hook_event_contract.py"

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$RAW_BASE_URL/work_ledger.py" -o "$TMP_LEDGER"
  curl -fsSL "$RAW_BASE_URL/hook_event_contract.py" -o "$TMP_HOOK"
else
  python3 - "$RAW_BASE_URL/work_ledger.py" "$TMP_LEDGER" "$RAW_BASE_URL/hook_event_contract.py" "$TMP_HOOK" <<'PY'
import sys
from urllib.request import urlopen

for url, path in ((sys.argv[1], sys.argv[2]), (sys.argv[3], sys.argv[4])):
    with urlopen(url, timeout=30) as response:
        data = response.read()
    with open(path, "wb") as handle:
        handle.write(data)
PY
fi

python3 -m py_compile "$TMP_LEDGER" "$TMP_HOOK"
chmod 755 "$TMP_LEDGER" "$TMP_HOOK"
mv "$TMP_LEDGER" "$INSTALL_PATH"
mv "$TMP_HOOK" "$HOOK_PATH"
chmod 755 "$INSTALL_PATH"
chmod 755 "$HOOK_PATH"
"$INSTALL_PATH" --help >/dev/null

cat <<MSG

OpenClaw Ledger is installed:
  $INSTALL_PATH
  $HOOK_PATH

Add this directory to PATH if needed:
  export PATH="$INSTALL_DIR:\$PATH"

Verify:
  $INSTALL_PATH --help
MSG

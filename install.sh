#!/usr/bin/env bash
set -euo pipefail

RAW_URL="https://raw.githubusercontent.com/moonhwilee/openclaw-ledger/main/src/work_ledger.py"
INSTALL_DIR="${OPENCLAW_LEDGER_INSTALL_DIR:-$HOME/.openclaw/bin}"
INSTALL_PATH="$INSTALL_DIR/openclaw-ledger"

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}

mkdir -p "$INSTALL_DIR"

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$RAW_URL" -o "$INSTALL_PATH"
else
  python3 - "$RAW_URL" "$INSTALL_PATH" <<'PY'
import sys
from urllib.request import urlopen

url, path = sys.argv[1], sys.argv[2]
with urlopen(url, timeout=30) as response:
    data = response.read()
with open(path, "wb") as handle:
    handle.write(data)
PY
fi

chmod +x "$INSTALL_PATH"

cat <<MSG

OpenClaw Ledger is installed:
  $INSTALL_PATH

Add this directory to PATH if needed:
  export PATH="$INSTALL_DIR:\$PATH"

Verify:
  $INSTALL_PATH --help
MSG

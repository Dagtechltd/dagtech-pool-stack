#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${BDAG_BLOCKLIST_MONITOR_LABEL:-com.blockdag.blocklist-monitor}"
RUNTIME_DIR="${BDAG_RUNTIME_DIR:-$ROOT/ops/runtime}"
ADDRESSES_FILE="${BDAG_BLOCKLIST_MONITOR_ADDRESSES:-$RUNTIME_DIR/blocklist-monitor-addresses.txt}"
RPC_URL="${BDAG_BLOCKLIST_MONITOR_RPC_URL:-https://rpc.blockdag.engineering}"
INTERVAL_SECONDS="${BDAG_BLOCKLIST_MONITOR_INTERVAL_SECONDS:-1800}"
JITTER_SECONDS="${BDAG_BLOCKLIST_MONITOR_JITTER_SECONDS:-120}"
PYTHON="${BDAG_BLOCKLIST_MONITOR_PYTHON:-$(python3 -c 'import sys; print(sys.executable)')}"
LOG_DIR="${BDAG_BLOCKLIST_MONITOR_LOG_DIR:-$HOME/Library/Logs/blockdag}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "launchd install is only available on macOS. Run ops/blocklist_activity_monitor.py from systemd/cron on this host." >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR/logs" "$LOG_DIR" "$HOME/Library/LaunchAgents"

if [[ ! -s "$ADDRESSES_FILE" ]]; then
  echo "missing address file: $ADDRESSES_FILE" >&2
  exit 1
fi

"$PYTHON" - "$PLIST" "$LABEL" "$ROOT" "$ADDRESSES_FILE" "$RPC_URL" "$INTERVAL_SECONDS" "$JITTER_SECONDS" "$LOG_DIR" "$PYTHON" <<'PY'
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
label = sys.argv[2]
root = Path(sys.argv[3])
addresses_file = Path(sys.argv[4])
rpc_url = sys.argv[5]
interval = int(sys.argv[6])
jitter = int(sys.argv[7])
log_dir = Path(sys.argv[8])
python = sys.argv[9]

payload = {
    "Label": label,
    "ProgramArguments": [
        python,
        str(root / "ops" / "blocklist_activity_monitor.py"),
        "--addresses-file",
        str(addresses_file),
        "--rpc-url",
        rpc_url,
        "--jitter-seconds",
        str(jitter),
    ],
    "RunAtLoad": True,
    "StartInterval": interval,
    "StandardOutPath": str(log_dir / "blocklist-monitor.out.log"),
    "StandardErrorPath": str(log_dir / "blocklist-monitor.err.log"),
    "WorkingDirectory": str(root),
}
plist_path.write_bytes(plistlib.dumps(payload, sort_keys=False))
PY

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

echo "installed $LABEL"
echo "plist: $PLIST"
echo "addresses: $ADDRESSES_FILE"

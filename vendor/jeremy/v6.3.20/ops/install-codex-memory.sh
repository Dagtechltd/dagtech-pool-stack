#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Install the Codex context memory service as a user-level systemd service.

Usage:
  ops/install-codex-memory.sh [options]

Options:
  --db-path PATH       SQLite database path. Default: ~/.codex/memories/context-store/context.sqlite
  --history-file PATH   History file to tail. Default: ~/.codex/history.jsonl
  --interval SECONDS    Poll interval. Default: 10
  --no-start           Write the unit but do not enable/start it
  -h, --help           Show this help
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DB_PATH="${CODEX_MEMORY_DB:-$HOME/.codex/memories/context-store/context.sqlite}"
HISTORY_FILE="${CODEX_HISTORY_FILE:-$HOME/.codex/history.jsonl}"
INTERVAL="${CODEX_MEMORY_INTERVAL:-10}"
START_SERVICE=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db-path)
      DB_PATH="${2:?--db-path requires a value}"
      shift 2
      ;;
    --history-file)
      HISTORY_FILE="${2:?--history-file requires a value}"
      shift 2
      ;;
    --interval)
      INTERVAL="${2:?--interval requires a value}"
      shift 2
      ;;
    --no-start)
      START_SERVICE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/codex-memory.service"
mkdir -p "$UNIT_DIR" "$(dirname "$DB_PATH")"

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Codex context memory service
After=default.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
Environment=CODEX_PROJECT_ROOT=$PROJECT_ROOT
Environment=CODEX_MEMORY_DB=$DB_PATH
Environment=CODEX_HISTORY_FILE=$HISTORY_FILE
Environment=CODEX_MEMORY_INTERVAL=$INTERVAL
ExecStart=/usr/bin/env python3 $PROJECT_ROOT/ops/codex_memory.py watch --interval $INTERVAL
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
if [[ "$START_SERVICE" -eq 1 ]]; then
  systemctl --user enable --now codex-memory.service
fi

cat <<EOF
Installed:
  $UNIT_FILE
  $DB_PATH

Query:
  python3 $PROJECT_ROOT/ops/codex_memory.py search "your topic"
EOF

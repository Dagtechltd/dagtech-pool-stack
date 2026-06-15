#!/usr/bin/env bash
set -euo pipefail

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUNTIME_DIR="${BDAG_RUNTIME_DIR:-$ROOT/ops/runtime}"
ENV_FILE="${BDAG_CHAIN_STATE_RESTORE_ENV_FILE:-$RUNTIME_DIR/ops.env}"
KEY_DIR="${BDAG_CHAIN_STATE_RESTORE_KEY_DIR:-$RUNTIME_DIR/chain-restore-ssh}"
KEY_PATH="${BDAG_CHAIN_STATE_RESTORE_KEY_PATH:-$KEY_DIR/id_ed25519}"
KNOWN_HOSTS="${BDAG_CHAIN_STATE_RESTORE_KNOWN_HOSTS:-$KEY_DIR/known_hosts}"
SOURCE="${BDAG_CHAIN_STATE_RESTORE_SOURCE:-}"
SSH_COMMAND="${BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND:-}"
STRICT=0
INSTALL_PUBLIC_KEY=0
PRINT_PUBLIC_KEY=0
VALIDATE_ONLY=0

usage() {
  cat <<'USAGE'
Usage:
  ops/setup-chain-restore-ssh.sh --source user@host:/path/to/mainnet [options]
  ops/setup-chain-restore-ssh.sh --source /local/path/to/mainnet [options]

Options:
  --env-file PATH            Runtime env file to update. Default: ops/runtime/ops.env.
  --key PATH                 Dedicated private key path. Default: ops/runtime/chain-restore-ssh/id_ed25519.
  --ssh-command COMMAND      Override SSH command used by rsync.
  --install-public-key       Run ssh-copy-id for the source host before BatchMode validation.
                             This is an install-time trust step and may prompt; no password is saved.
  --print-public-key         Print the generated public key and exit after key creation.
  --validate-only            Do not update env; only validate current source/SSH command.
  --strict                   Exit non-zero on validation failure.
  -h, --help                 Show this help.

The script never stores passwords. For unattended self-heal after installation,
the configured source must pass SSH BatchMode and rsync dry-run validation.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE="${2:-}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:-}"
      shift 2
      ;;
    --key)
      KEY_PATH="${2:-}"
      shift 2
      ;;
    --ssh-command)
      SSH_COMMAND="${2:-}"
      shift 2
      ;;
    --install-public-key)
      INSTALL_PUBLIC_KEY=1
      shift
      ;;
    --print-public-key)
      PRINT_PUBLIC_KEY=1
      shift
      ;;
    --validate-only)
      VALIDATE_ONLY=1
      shift
      ;;
    --strict)
      STRICT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$KEY_DIR" "$(dirname "$ENV_FILE")" "$RUNTIME_DIR/logs"
chmod 0700 "$KEY_DIR"
LOG_FILE="$RUNTIME_DIR/logs/chain-restore-ssh-setup.log"

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG_FILE" >&2
}

env_quote() {
  python3 - "$1" <<'PY'
import sys

value = sys.argv[1]
if value and all((not ch.isspace()) and ch not in '"\\$`#' for ch in value):
    print(value)
else:
    print('"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"')
PY
}

set_env_value() {
  local file="$1" key="$2" value="$3" encoded
  encoded="$(env_quote "$value")"
  python3 - "$file" "$key" "$encoded" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
encoded = sys.argv[3]
path.parent.mkdir(parents=True, exist_ok=True)
lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
prefix = key + "="
updated = []
replaced = False
for line in lines:
    if line.startswith(prefix):
        if not replaced:
            updated.append(prefix + encoded)
            replaced = True
        continue
    updated.append(line)
if not replaced:
    updated.append(prefix + encoded)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
  chmod 0600 "$file"
}

is_remote_source() {
  [[ "$1" == *:* && "$1" != /* ]]
}

default_ssh_command() {
  printf 'ssh -i %q -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=%q' "$KEY_PATH" "$KNOWN_HOSTS"
}

ensure_key() {
  if ! command -v ssh-keygen >/dev/null 2>&1; then
    log "ssh-keygen is required to create a restore key"
    return 1
  fi
  if [[ ! -f "$KEY_PATH" ]]; then
    ssh-keygen -t ed25519 -N "" -C "bdag-chain-restore@$(hostname -s 2>/dev/null || hostname)" -f "$KEY_PATH" >/dev/null
    chmod 0600 "$KEY_PATH"
    chmod 0644 "$KEY_PATH.pub"
    log "created dedicated chain-restore SSH key at $KEY_PATH"
  fi
  [[ -f "$KEY_PATH.pub" ]] || ssh-keygen -y -f "$KEY_PATH" > "$KEY_PATH.pub"
}

remote_user_host() {
  printf '%s\n' "${SOURCE%%:*}"
}

remote_path() {
  printf '%s\n' "${SOURCE#*:}"
}

run_remote_test() {
  local user_host="$1" path="$2" quoted_path remote_cmd quoted_host quoted_cmd
  quoted_path="$(printf '%q' "$path")"
  remote_cmd="test -d $quoted_path"
  quoted_host="$(printf '%q' "$user_host")"
  quoted_cmd="$(printf '%q' "$remote_cmd")"
  bash -lc "$SSH_COMMAND $quoted_host $quoted_cmd"
}

validate_source() {
  if [[ -z "$SOURCE" ]]; then
    log "no chain restore source configured"
    return 1
  fi
  if is_remote_source "$SOURCE"; then
    local user_host path probe_dir
    ensure_key
    SSH_COMMAND="${SSH_COMMAND:-$(default_ssh_command)}"
    user_host="$(remote_user_host)"
    path="$(remote_path)"
    if [[ "$INSTALL_PUBLIC_KEY" == "1" ]]; then
      if ! command -v ssh-copy-id >/dev/null 2>&1; then
        log "ssh-copy-id is required for --install-public-key"
        return 1
      fi
      log "installing public restore key on $user_host; this may prompt during setup"
      ssh-copy-id -i "$KEY_PATH.pub" "$user_host"
    fi
    run_remote_test "$user_host" "$path"
    probe_dir="$RUNTIME_DIR/chain-restore-ssh/rsync-probe"
    mkdir -p "$probe_dir"
    rsync -an --delete -e "$SSH_COMMAND" "${SOURCE%/}/" "$probe_dir/" >>"$LOG_FILE" 2>&1
  else
    [[ -d "$SOURCE" ]] || { log "local restore source is not a directory: $SOURCE"; return 1; }
    rsync -an --delete "${SOURCE%/}/" "$RUNTIME_DIR/chain-restore-ssh/rsync-probe/" >>"$LOG_FILE" 2>&1
  fi
}

if [[ -n "$SOURCE" && is_remote_source "$SOURCE" ]]; then
  ensure_key
  SSH_COMMAND="${SSH_COMMAND:-$(default_ssh_command)}"
elif [[ -z "$SSH_COMMAND" ]]; then
  SSH_COMMAND="$(default_ssh_command)"
fi

if [[ "$PRINT_PUBLIC_KEY" == "1" ]]; then
  ensure_key
  printf '%s\n' "Public key to authorize on the trusted chain source:"
  cat "$KEY_PATH.pub"
  exit 0
fi

if validate_source; then
  log "chain restore source validated: $SOURCE"
  if [[ "$VALIDATE_ONLY" != "1" ]]; then
    set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_SELF_HEAL_ENABLED 1
    set_env_value "$ENV_FILE" BDAG_MINING_IMPERATIVE_CHAIN_STATE_RESTORE_ENABLED 1
    set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_SOURCE "$SOURCE"
    if is_remote_source "$SOURCE"; then
      set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND "$SSH_COMMAND"
      set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_KEY_PATH "$KEY_PATH"
      set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_KNOWN_HOSTS "$KNOWN_HOSTS"
    fi
    set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_SETUP_STATUS validated
    set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_SETUP_AT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
  exit 0
fi

log "chain restore source validation failed"
if [[ "$VALIDATE_ONLY" != "1" ]]; then
  set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_SETUP_STATUS failed
  set_env_value "$ENV_FILE" BDAG_CHAIN_STATE_RESTORE_SETUP_AT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
if [[ "$STRICT" == "1" ]]; then
  exit 1
fi
exit 0

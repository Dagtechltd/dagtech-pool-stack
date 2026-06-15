#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
if [[ -z "${BDAG_STACK_DEFAULTS_FILE:-}" ]]; then
  if [[ -f "$ROOT/ops/config/stack-defaults.env" ]]; then
    BDAG_STACK_DEFAULTS_FILE="$ROOT/ops/config/stack-defaults.env"
  else
    BDAG_STACK_DEFAULTS_FILE="$ROOT/config/stack-defaults.env"
  fi
fi
if [[ -f "$BDAG_STACK_DEFAULTS_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$BDAG_STACK_DEFAULTS_FILE"
  set +a
fi
DOCKER=(docker)

say() { printf '\n==> %s\n' "$*"; }
warn() { printf '\nWARNING: %s\n' "$*" >&2; }

stack_default() {
  local key="$1" fallback="${2:-}"
  if [[ ${!key+x} ]]; then
    printf '%s' "${!key}"
  else
    printf '%s' "$fallback"
  fi
}

ask() {
  local prompt="$1" default="${2:-}" value
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value || true
    printf '%s\n' "${value:-$default}"
  else
    read -r -p "$prompt: " value || true
    printf '%s\n' "$value"
  fi
}

yes_no() {
  local prompt="$1" default="${2:-n}" value suffix="[y/N]"
  [[ "$default" == "y" ]] && suffix="[Y/n]"
  read -r -p "$prompt $suffix " value || true
  value="${value:-$default}"
  [[ "$value" =~ ^[Yy] ]]
}

need_sudo() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

compose_cmd() {
  if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
    "${DOCKER[@]}" compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    return 127
  fi
}

init_docker_access() {
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
    export BDAG_DOCKER_USE_SUDO=0
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
    export BDAG_DOCKER_USE_SUDO=1
    return 0
  fi
  echo "Docker is installed but this user cannot access it yet." >&2
  echo "Log out and back in, run 'newgrp docker', or rerun this installer with sudo." >&2
  exit 1
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf 'amd64\n' ;;
    aarch64|arm64) printf 'arm64\n' ;;
    *) return 1 ;;
  esac
}

detect_lan_ip() {
  local detected
  if [[ -n "${BDAG_POOL_HOST:-}" ]]; then
    printf '%s\n' "$BDAG_POOL_HOST"
    return 0
  fi
  if command -v ip >/dev/null 2>&1 && [[ -n "${BDAG_ASIC_LAN_INTERFACE:-}" ]]; then
    detected="$(ip -o -4 addr show dev "$BDAG_ASIC_LAN_INTERFACE" scope global 2>/dev/null \
      | awk '{split($4,a,"/"); if (a[1] != "") {print a[1]; exit}}' || true)"
    if [[ -n "$detected" ]]; then
      printf '%s\n' "$detected"
      return 0
    fi
  fi
  if command -v ip >/dev/null 2>&1; then
    detected="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true)"
    if [[ -n "$detected" && ! "$detected" =~ ^127\. && ! "$detected" =~ ^169\.254\. && ! "$detected" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]]; then
      printf '%s\n' "$detected"
      return 0
    fi
    detected="$(ip -o -4 addr show scope global 2>/dev/null \
      | awk '
          $2 !~ /^(docker|br-|veth|zt|wg|tun|tap|tailscale)/ {
            split($4,a,"/")
            if (a[1] !~ /^127\./ && a[1] !~ /^169\.254\./ && a[1] !~ /^172\.(1[6-9]|2[0-9]|3[0-1])\./) {
              print a[1]
              exit
            }
          }' || true)"
    if [[ -n "$detected" ]]; then
      printf '%s\n' "$detected"
      return 0
    fi
    ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true
  fi
}

wired_route_policy_script() {
  local candidate
  for candidate in \
    "$ROOT/scripts/validate-network-route-policy.py" \
    "$ROOT/../scripts/validate-network-route-policy.py" \
    "$(cd "$ROOT/.." 2>/dev/null && pwd)/scripts/validate-network-route-policy.py"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

enforce_wired_route_policy() {
  if [[ "$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')" != "linux" ]]; then
    return 0
  fi
  if [[ "${BDAG_ENFORCE_WIRED_ROUTE_POLICY:-1}" != "1" ]]; then
    warn "Skipping wired-first route policy because BDAG_ENFORCE_WIRED_ROUTE_POLICY=${BDAG_ENFORCE_WIRED_ROUTE_POLICY:-unset}."
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 is missing; cannot validate or apply wired-first route policy."
    return 0
  fi
  local script
  script="$(wired_route_policy_script || true)"
  if [[ -z "$script" ]]; then
    warn "Wired-first route policy script is missing from this package."
    return 0
  fi
  say "Applying wired-first route policy"
  if ! python3 "$script" --apply --warn-only; then
    warn "Wired-first route policy application failed; continuing so preflight can report the remaining network state."
  fi
}

default_cidr() {
  local ipaddr="$1"
  if [[ "$ipaddr" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.[0-9]+$ ]]; then
    printf '%s.%s.%s.0/24\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
  else
    printf '192.168.1.0/24\n'
  fi
}

is_default_docker_bridge_address() {
  [[ "$1" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]]
}

validate_pool_lan_config() {
  local pool_host pool_url pool_url_host scan_target asic_cidrs allow_bridge
  pool_host="$(grep -E '^BDAG_POOL_HOST=' .env | tail -n 1 | cut -d= -f2- || true)"
  pool_url="$(grep -E '^BDAG_POOL_URL=' .env | tail -n 1 | cut -d= -f2- || true)"
  scan_target="$(grep -E '^BDAG_MINER_SCAN_TARGET=' .env | tail -n 1 | cut -d= -f2- || true)"
  asic_cidrs="$(grep -E '^BDAG_ASIC_LAN_CIDRS=' .env | tail -n 1 | cut -d= -f2- || true)"
  allow_bridge="$(grep -E '^BDAG_ALLOW_DOCKER_BRIDGE_ASIC_IPS=' .env | tail -n 1 | cut -d= -f2- || true)"
  pool_host="${pool_host%\"}"; pool_host="${pool_host#\"}"
  pool_url="${pool_url%\"}"; pool_url="${pool_url#\"}"
  scan_target="${scan_target%\"}"; scan_target="${scan_target#\"}"
  asic_cidrs="${asic_cidrs%\"}"; asic_cidrs="${asic_cidrs#\"}"
  allow_bridge="${allow_bridge:-0}"
  pool_url_host="${pool_url#*://}"
  pool_url_host="${pool_url_host%%:*}"
  if [[ -z "$pool_host" || -z "$pool_url" || -z "$scan_target" || -z "$asic_cidrs" ]]; then
    echo "Pool LAN configuration is incomplete. Set BDAG_POOL_HOST, BDAG_POOL_URL, BDAG_MINER_SCAN_TARGET, and BDAG_ASIC_LAN_CIDRS." >&2
    exit 1
  fi
  if [[ "$allow_bridge" != "1" && "$allow_bridge" != "true" && "$allow_bridge" != "True" ]]; then
    if is_default_docker_bridge_address "$pool_host" || is_default_docker_bridge_address "$pool_url_host"; then
      echo "Refusing Docker bridge pool endpoint '$pool_url'. Use the host-facing ASIC LAN IP, not a 172.16.0.0/12 container address." >&2
      exit 1
    fi
    if [[ "$scan_target" =~ (^|[,[:space:]])172\.(1[6-9]|2[0-9]|3[0-1])\. || "$asic_cidrs" =~ (^|[,[:space:]])172\.(1[6-9]|2[0-9]|3[0-1])\. ]]; then
      echo "Refusing Docker bridge ASIC scan scope '$asic_cidrs'. Set BDAG_ASIC_LAN_CIDRS to the physical ASIC LAN." >&2
      exit 1
    fi
  fi
}

detect_zerotier_interface() {
  ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -m1 '^zt' || true
}

configure_miner_network() {
  # Miners may live on the host LAN (no extra routing) or on a remote LAN
  # reached through a routed gateway such as a ZeroTier peer. In the remote
  # case the miner-route compose service keeps a host route alive so the
  # watchdog can reach the miner HTTP API for health checks and restarts.
  miner_route_subnet=""
  miner_route_gateway=""
  miner_route_dev=""
  if yes_no "Are any miners on a remote LAN reached through a routed gateway (e.g. a ZeroTier peer)?" "n"; then
    miner_route_subnet="$(ask "Remote miner subnet (CIDR)" "$(env_value BDAG_MINER_ROUTE_SUBNET)")"
    miner_route_gateway="$(ask "Gateway IP that forwards to that subnet (e.g. the ZeroTier peer)" "$(env_value BDAG_MINER_ROUTE_GATEWAY)")"
    miner_route_dev="$(ask "Host interface for the route (blank lets the kernel pick)" "$(env_value BDAG_MINER_ROUTE_DEV "$(detect_zerotier_interface)")")"
    if [[ -n "$miner_route_subnet" && ",$scan_target," != *",$miner_route_subnet,"* ]]; then
      scan_target="$scan_target,$miner_route_subnet"
    fi
  fi
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
  fi
}

strip_env_quotes() {
  local value="$1"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

quote_env_assignment_value() {
  local value needs_quotes=0
  value="$(strip_env_quotes "$1")"
  case "$value" in
    *[[:space:]#]*|*\"*|*\\*|*\$*|*\`*) needs_quotes=1 ;;
  esac
  if [[ "$needs_quotes" == "0" ]]; then
    printf '%s' "$value"
    return 0
  fi
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\$}"
  value="${value//\`/\\\`}"
  printf '"%s"' "$value"
}

sed_replacement_escape() {
  sed -e 's/[&|]/\\&/g'
}

set_env_value() {
  local file="$1" key="$2" value="$3" rendered escaped
  rendered="$(quote_env_assignment_value "$value")"
  escaped="$(printf '%s' "$rendered" | sed_replacement_escape)"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${escaped}|" "$file"
  else
    printf '%s=%s\n' "$key" "$rendered" >> "$file"
  fi
}

set_stack_default_env_value() {
  local file="$1" key="$2" fallback="${3:-}"
  set_env_value "$file" "$key" "$(stack_default "$key" "$fallback")"
}

set_existing_or_stack_default_env_value() {
  local file="$1" key="$2" fallback="${3:-}" existing
  existing="$(env_value "$key" "")"
  if [[ -n "$existing" ]]; then
    set_env_value "$file" "$key" "$existing"
  else
    set_stack_default_env_value "$file" "$key" "$fallback"
  fi
}

apply_stack_defaults_env() {
  local file="$1" line key value
  [[ -f "$BDAG_STACK_DEFAULTS_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    key="${line%%=*}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if grep -q "^${key}=" "$file"; then
      continue
    fi
    value="$(stack_default "$key" "${line#*=}")"
    set_env_value "$file" "$key" "$value"
  done < "$BDAG_STACK_DEFAULTS_FILE"
}

configure_active_node_env() {
  set_env_value .env COMPOSE_PROFILES ""
  set_stack_default_env_value .env BDAG_POOL_CONTAINER pool
  set_stack_default_env_value .env BDAG_POOL_CONTAINERS pool
  set_stack_default_env_value .env BDAG_POOL_DB_CONTAINER postgres
  set_stack_default_env_value .env BDAG_NODE_SERVICE node
  set_stack_default_env_value .env BDAG_STACK_SERVICES "postgres,node,pool"
  set_stack_default_env_value .env BDAG_START_SERVICES "postgres,node,pool"
  set_stack_default_env_value .env BDAG_ASIC_EXPECTED_MACS
  set_stack_default_env_value .env POOL_ASIC_MAC_OVERRIDES
  set_env_value .env NODE_RPC_URL "http://node:38131"
  set_env_value .env WALLET_RPC_URL "http://node:18545"
  set_env_value .env WALLET_RPC_URLS "http://node:18545"
  set_stack_default_env_value .env POOL_GBT_MIN_INTERVAL_MS
  set_stack_default_env_value .env POOL_GBT_PRESSURE_INTERVAL_MS
  set_stack_default_env_value .env POOL_GBT_PRESSURE_WINDOW_SECONDS
  set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_ENABLED
  set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_PROBE_SECONDS
  set_stack_default_env_value .env POOL_RPC_ROUTER_NODE_HEALTH_MAX_AGE_SECONDS
}

configure_node_mining_env() {
  local enabled="$1" mining_address="$2"
  if [[ "$enabled" == "1" ]]; then
    set_env_value .env BDAG_ENABLE_NODE_MINING 1
    set_env_value .env BDAG_NODE_MODULES "Blockdag"
    set_env_value .env BDAG_NODE_MINING_ARGS "--miner --miningaddr=${mining_address} --maxinbound=1"
  else
    set_env_value .env BDAG_ENABLE_NODE_MINING 0
    set_env_value .env BDAG_NODE_MODULES "Blockdag"
    set_env_value .env BDAG_NODE_MINING_ARGS ""
  fi
}

env_value() {
  local key="$1" fallback="${2:-}" value
  value="$(grep -E "^${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  value="${value:-$fallback}"
  strip_env_quotes "$value"
  printf '\n'
}

absolute_path() {
  local path="$1"
  if [[ "$path" == /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s\n' "$ROOT/${path#./}"
  fi
}

env_path_value() {
  local key="$1" fallback="$2"
  absolute_path "$(env_value "$key" "$fallback")"
}

env_path_value_for_auto_profile() {
  local key="$1" fallback="$2" shipped_default="$3" value
  value="$(env_value "$key" "")"
  if [[ -z "$value" || "$value" == "auto" || "$value" == "$shipped_default" || "$value" == "${shipped_default#./}" ]]; then
    absolute_path "$fallback"
  else
    absolute_path "$value"
  fi
}

existing_parent() {
  local path="$1"
  while [[ ! -e "$path" && "$path" != "/" ]]; do
    path="$(dirname "$path")"
  done
  printf '%s\n' "$path"
}

path_free_gib() {
  local path parent
  path="$1"
  parent="$(existing_parent "$path")"
  df -Pk "$parent" 2>/dev/null | awk 'NR == 2 {printf "%d", $4 / 1048576}'
}

path_fstype() {
  local path parent
  path="$1"
  parent="$(existing_parent "$path")"
  findmnt -rn -T "$parent" -o FSTYPE 2>/dev/null | head -n1
}

fstab_escape() {
  printf '%s' "$1" | sed -e 's/ /\\040/g' -e 's/\t/\\011/g'
}

disabled_mode() {
  [[ "${1,,}" =~ ^(0|false|no|off|disabled)$ ]]
}

mount_source_for_path() {
  local path parent
  path="$1"
  parent="$(existing_parent "$path")"
  findmnt -rn -T "$parent" -o SOURCE 2>/dev/null | sed 's/\[.*//'
}

same_mount_device() {
  local left="$1" right="$2" left_source right_source
  left_source="$(mount_source_for_path "$left")"
  right_source="$(mount_source_for_path "$right")"
  [[ -n "$left_source" && "$left_source" == "$right_source" ]]
}

path_is_usb() {
  local source block tran
  source="$(mount_source_for_path "$1")"
  [[ "$source" == /dev/* ]] || return 1
  tran="$(lsblk -no TRAN "$source" 2>/dev/null | head -n1 || true)"
  if [[ "$tran" == "usb" ]]; then
    return 0
  fi
  block="$(lsblk -no PKNAME "$source" 2>/dev/null | head -n1 || true)"
  [[ -n "$block" ]] || block="$(basename "$source")"
  tran="$(lsblk -dn -o TRAN "/dev/$block" 2>/dev/null | head -n1 || true)"
  [[ "$tran" == "usb" ]]
}

select_chain_data_base() {
  local configured target fstype source free_gib score best="" best_score=-1 profile min_chain_gib
  configured="$(env_value BDAG_CHAIN_DATA_DIR "")"
  profile="$(env_value BDAG_STORAGE_PROFILE auto)"
  if [[ -n "$configured" && "$configured" != "auto" && ! ( "$profile" == "auto" && ( "$configured" == "./data" || "$configured" == "data" ) ) ]]; then
    absolute_path "$configured"
    return 0
  fi
  min_chain_gib="$(env_value BDAG_STORAGE_MIN_CHAIN_FREE_GIB "${BDAG_STORAGE_MIN_CHAIN_FREE_GIB:-50}")"

  while read -r target fstype source; do
    case "$target" in
      /|/boot*|/dev*|/proc*|/run*|/sys*|/snap*|/var/lib/docker*|/var/lib/snapd*) continue ;;
    esac
    case "$fstype" in
      tmpfs|devtmpfs|overlay|squashfs|proc|sysfs|cgroup*|devpts|securityfs|tracefs|debugfs|fusectl|configfs) continue ;;
    esac
    free_gib="$(path_free_gib "$target")"
    free_gib="${free_gib:-0}"
    (( free_gib >= min_chain_gib )) || continue
    score="$free_gib"
    if path_is_usb "$target"; then
      score=$(( score + 100000 ))
    fi
    if (( score > best_score )); then
      best="$target/blockdag-chain"
      best_score="$score"
    fi
  done < <(findmnt -rn -o TARGET,FSTYPE,SOURCE)

  if [[ -n "$best" ]]; then
    printf '%s\n' "$best"
  else
    printf '%s\n' "$ROOT/data"
  fi
}

select_runtime_data_base() {
  local chain_base="$1" configured runtime_free min_runtime_gib
  configured="$(env_value BDAG_RUNTIME_DATA_DIR "")"
  if [[ -n "$configured" && "$configured" != "auto" ]]; then
    absolute_path "$configured"
    return 0
  fi
  min_runtime_gib="$(env_value BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "${BDAG_STORAGE_MIN_RUNTIME_FREE_GIB:-4}")"
  runtime_free="$(path_free_gib "$ROOT")"
  runtime_free="${runtime_free:-0}"
  if ! same_mount_device "$ROOT" "$chain_base" && (( runtime_free >= min_runtime_gib )); then
    printf '%s\n' "$ROOT/runtime-data"
  else
    printf '%s\n' "$chain_base/runtime"
  fi
}

configure_storage_profile() {
  local chain_base runtime_base node_dir postgres_dir runtime_dir profile existing_profile
  chain_base="$(absolute_path "$(select_chain_data_base)")"
  runtime_base="$(absolute_path "$(select_runtime_data_base "$chain_base")")"
  existing_profile="$(env_value BDAG_STORAGE_PROFILE auto)"
  if [[ "$existing_profile" == "auto" || -z "$existing_profile" ]]; then
    node_dir="$(env_path_value_for_auto_profile BDAG_NODE_DATA_DIR "$chain_base/node" "./data/node")"
    postgres_dir="$(env_path_value_for_auto_profile BDAG_POSTGRES_DATA_DIR "$runtime_base/postgres" "./data/postgres")"
    runtime_dir="$(env_path_value_for_auto_profile BDAG_RUNTIME_DIR "$runtime_base/ops-runtime" "./ops/runtime")"
  else
    node_dir="$(env_path_value BDAG_NODE_DATA_DIR "$chain_base/node")"
    postgres_dir="$(env_path_value BDAG_POSTGRES_DATA_DIR "$runtime_base/postgres")"
    runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "$runtime_base/ops-runtime")"
  fi
  if [[ "$existing_profile" == "auto" || -z "$existing_profile" ]]; then
    if path_is_usb "$chain_base" && ! same_mount_device "$chain_base" "$runtime_base"; then
      profile="usb-chain-internal-runtime"
    elif path_is_usb "$chain_base"; then
      profile="single-usb-constrained"
    elif ! same_mount_device "$chain_base" "$runtime_base"; then
      profile="split-ssd"
    else
      profile="single-device"
    fi
  else
    profile="$existing_profile"
  fi

  set_env_value .env BDAG_STORAGE_PROFILE "$profile"
  set_env_value .env BDAG_CHAIN_DATA_DIR "$chain_base"
  set_env_value .env BDAG_DATA_DIR "$chain_base"
  set_env_value .env BDAG_NODE_DATA_DIR "$node_dir"
  set_env_value .env BDAG_POSTGRES_DATA_DIR "$postgres_dir"
  set_env_value .env BDAG_RUNTIME_DIR "$runtime_dir"
  set_env_value .env BDAG_STORAGE_MIN_CHAIN_FREE_GIB "$(env_value BDAG_STORAGE_MIN_CHAIN_FREE_GIB "${BDAG_STORAGE_MIN_CHAIN_FREE_GIB:-50}")"
  set_env_value .env BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "$(env_value BDAG_STORAGE_MIN_RUNTIME_FREE_GIB "${BDAG_STORAGE_MIN_RUNTIME_FREE_GIB:-4}")"
  set_env_value .env BDAG_NODE_CPU_SHARES "$(env_value BDAG_NODE_CPU_SHARES 6144)"
  set_env_value .env BDAG_POOL_CPU_SHARES "$(env_value BDAG_POOL_CPU_SHARES 5120)"
  set_env_value .env BDAG_POOL_DB_CPU_SHARES "$(env_value BDAG_POOL_DB_CPU_SHARES 4096)"
  set_env_value .env BDAG_DASHBOARD_CPU_SHARES "$(env_value BDAG_DASHBOARD_CPU_SHARES 128)"
  set_env_value .env BDAG_NODE_MEMORY_LOW "$(env_value BDAG_NODE_MEMORY_LOW 768M)"
  set_env_value .env BDAG_NODE_MEMORY_HIGH "$(env_value BDAG_NODE_MEMORY_HIGH auto)"
  set_env_value .env BDAG_NODE_MEMORY_HIGH_PERCENT "$(env_value BDAG_NODE_MEMORY_HIGH_PERCENT 60)"
  set_env_value .env BDAG_NODE_MEMORY_HIGH_MIN "$(env_value BDAG_NODE_MEMORY_HIGH_MIN 3072M)"
  set_env_value .env BDAG_POOL_MEMORY_LOW "$(env_value BDAG_POOL_MEMORY_LOW 256M)"
  set_env_value .env BDAG_POOL_DB_MEMORY_LOW "$(env_value BDAG_POOL_DB_MEMORY_LOW 512M)"
  set_env_value .env BDAG_DASHBOARD_MEMORY_LOW "$(env_value BDAG_DASHBOARD_MEMORY_LOW 64M)"
  set_env_value .env BDAG_TUNE_NET_QDISC "$(env_value BDAG_TUNE_NET_QDISC 1)"

  mkdir -p "$node_dir" "$postgres_dir" "$runtime_dir/logs"
  say "Storage profile: $profile"
  echo "Chain data: $chain_base"
  echo "Postgres data: $postgres_dir"
  echo "Runtime/dashboard state: $runtime_dir"
}

default_btrfs_checkpoint_image() {
  local size_gib="$1" base
  base="${BDAG_BTRFS_CHECKPOINT_VOLUME_IMAGE_BASE:-$HOME/blockdag-snapshot-volumes}"
  printf '%s/rawdatadir-checkpoint-%sg.btrfs\n' "$base" "$size_gib"
}

ensure_btrfs_subvolume_or_dir() {
  local path="$1"
  if [[ -e "$path" ]]; then
    return 0
  fi
  if need_sudo btrfs subvolume create "$path" >/dev/null 2>&1; then
    return 0
  fi
  need_sudo mkdir -p "$path"
}

ensure_btrfs_checkpoint_owned_dir() {
  local path="$1"
  need_sudo mkdir -p "$path"
  need_sudo chown "$(id -u):$(id -g)" "$path" || true
}

ensure_btrfs_fstab_entry() {
  local image="$1" mountpoint="$2" options="$3" image_parent entry escaped_image escaped_mount
  image_parent="$(dirname "$image")"
  escaped_image="$(fstab_escape "$image")"
  escaped_mount="$(fstab_escape "$mountpoint")"
  entry="$escaped_image $escaped_mount btrfs loop,$options,nofail,x-systemd.requires-mounts-for=$(fstab_escape "$image_parent") 0 0"
  if grep -qsE "^[^#].*[[:space:]]${escaped_mount}[[:space:]]+btrfs[[:space:]]" /etc/fstab; then
    return 0
  fi
  need_sudo cp /etc/fstab "/etc/fstab.bdag-pre-btrfs-$(date +%Y%m%d-%H%M%S)"
  printf '%s\n' "$entry" | need_sudo tee -a /etc/fstab >/dev/null
}

configure_btrfs_checkpoint_volume() {
  local mode size_gib min_free_gib mount_value mountpoint image_value image image_dir label options free_gib required_gib
  mode="$(env_value BDAG_BTRFS_CHECKPOINT_VOLUME_MODE "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_MODE auto)")"
  if disabled_mode "$mode"; then
    warn "Btrfs checkpoint volume disabled by BDAG_BTRFS_CHECKPOINT_VOLUME_MODE=$mode."
    return 0
  fi
  if [[ "$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')" != "linux" ]]; then
    echo "Trusted IPFS raw checkpoint storage requires Linux btrfs/ZFS/LVM semantics; this installer only provisions btrfs on Linux." >&2
    exit 1
  fi
  if ! command -v mkfs.btrfs >/dev/null 2>&1 || ! command -v btrfs >/dev/null 2>&1; then
    echo "btrfs-progs is required for the trusted checkpoint volume. Install btrfs-progs and rerun ./install.sh." >&2
    exit 1
  fi

  size_gib="$(env_value BDAG_BTRFS_CHECKPOINT_VOLUME_SIZE_GIB "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_SIZE_GIB 128)")"
  min_free_gib="$(env_value BDAG_BTRFS_CHECKPOINT_VOLUME_MIN_ROOT_FREE_GIB "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_MIN_ROOT_FREE_GIB 40)")"
  mount_value="$(env_value BDAG_BTRFS_CHECKPOINT_VOLUME_MOUNT "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_MOUNT ./data-restore/btrfs-checkpoints)")"
  mountpoint="$(absolute_path "$mount_value")"
  image_value="$(env_value BDAG_BTRFS_CHECKPOINT_VOLUME_IMAGE "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_IMAGE "")")"
  if [[ -z "$image_value" || "$image_value" == "auto" ]]; then
    image="$(default_btrfs_checkpoint_image "$size_gib")"
  else
    image="$(absolute_path "$image_value")"
  fi
  image_dir="$(dirname "$image")"
  label="$(env_value BDAG_BTRFS_CHECKPOINT_VOLUME_LABEL "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_LABEL bdag-raw-checkpoints)")"
  options="$(env_value BDAG_BTRFS_CHECKPOINT_VOLUME_OPTIONS "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_OPTIONS noatime,compress=zstd:1,space_cache=v2)")"

  if [[ ! "$size_gib" =~ ^[0-9]+$ || "$size_gib" -lt 128 ]]; then
    echo "BDAG_BTRFS_CHECKPOINT_VOLUME_SIZE_GIB must be at least 128GiB for trusted raw checkpoint storage." >&2
    exit 1
  fi
  if [[ ! "$min_free_gib" =~ ^[0-9]+$ ]]; then
    min_free_gib=40
  fi

  if [[ ! -f "$image" ]]; then
    free_gib="$(path_free_gib "$image_dir")"
    free_gib="${free_gib:-0}"
    required_gib=$(( size_gib + min_free_gib ))
    if (( free_gib < required_gib )); then
      echo "Not enough free space for the btrfs checkpoint image at $image_dir: need ${required_gib}GiB (${size_gib}GiB volume + ${min_free_gib}GiB reserve), found ${free_gib}GiB." >&2
      exit 1
    fi
    say "Creating ${size_gib}GiB btrfs checkpoint image"
    mkdir -p "$image_dir"
    fallocate -l "${size_gib}G" "$image"
    chmod 0600 "$image"
    mkfs.btrfs -f -L "$label" "$image" >/dev/null
  fi

  mkdir -p "$mountpoint"
  ensure_btrfs_fstab_entry "$image" "$mountpoint" "$options"
  if ! mountpoint -q "$mountpoint"; then
    say "Mounting btrfs checkpoint volume at $mountpoint"
    need_sudo mount "$mountpoint" || need_sudo mount -o "loop,$options" "$image" "$mountpoint"
  fi

  if [[ "$(path_fstype "$mountpoint")" != "btrfs" ]]; then
    echo "Checkpoint mount $mountpoint is not btrfs; refusing to continue because trusted raw checkpoints require snapshot-capable storage." >&2
    exit 1
  fi

  ensure_btrfs_subvolume_or_dir "$mountpoint/rawdatadir-sidecar"
  ensure_btrfs_subvolume_or_dir "$mountpoint/rawdatadir-sidecar-content"
  ensure_btrfs_subvolume_or_dir "$mountpoint/rawdatadir-sidecar-open"
  ensure_btrfs_subvolume_or_dir "$mountpoint/rawdatadir-artifacts"
  ensure_btrfs_checkpoint_owned_dir "$mountpoint/rawdatadir-sidecar"
  ensure_btrfs_checkpoint_owned_dir "$mountpoint/rawdatadir-sidecar-content"
  ensure_btrfs_checkpoint_owned_dir "$mountpoint/rawdatadir-sidecar-content/artifacts"
  ensure_btrfs_checkpoint_owned_dir "$mountpoint/rawdatadir-sidecar-content/chunk-store"
  ensure_btrfs_checkpoint_owned_dir "$mountpoint/rawdatadir-sidecar-content/chunk-store/sha256"
  ensure_btrfs_checkpoint_owned_dir "$mountpoint/rawdatadir-sidecar-open"
  ensure_btrfs_checkpoint_owned_dir "$mountpoint/rawdatadir-artifacts"

  set_env_value .env BDAG_BTRFS_CHECKPOINT_VOLUME_MODE "$mode"
  set_env_value .env BDAG_BTRFS_CHECKPOINT_VOLUME_SIZE_GIB "$size_gib"
  set_env_value .env BDAG_BTRFS_CHECKPOINT_VOLUME_MIN_ROOT_FREE_GIB "$min_free_gib"
  set_env_value .env BDAG_BTRFS_CHECKPOINT_VOLUME_IMAGE "$image"
  set_env_value .env BDAG_BTRFS_CHECKPOINT_VOLUME_MOUNT "$mount_value"
  set_env_value .env BDAG_BTRFS_CHECKPOINT_VOLUME_LABEL "$label"
  set_env_value .env BDAG_BTRFS_CHECKPOINT_VOLUME_OPTIONS "$options"
  set_env_value .env BDAG_RAWDATADIR_ARTIFACT_BASE "$mount_value/rawdatadir-artifacts"
  set_env_value .env BDAG_RAWDATADIR_SIDECAR_DIR "$mount_value/rawdatadir-sidecar/mainnet"
  set_env_value .env BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE "$mount_value/rawdatadir-sidecar-content"
  set_env_value .env BDAG_RAWDATADIR_OPEN_SIDECAR_BASE "$mount_value/rawdatadir-sidecar-open/mainnet"
  set_env_value .env BDAG_IPFS_CONTENT_ARTIFACT_DIR "$mount_value/rawdatadir-sidecar-content/current"
  set_env_value .env BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST "$mount_value/rawdatadir-sidecar-content/current/manifest.json"

  say "Btrfs checkpoint volume ready: $mountpoint"
}

configure_ephemeral_storage() {
  local enabled ephemeral_dir tmpfs_size mem_kb mem_gb
  enabled="$(env_value BDAG_EPHEMERAL_TMPFS_ENABLED 1)"
  ephemeral_dir="$(env_path_value BDAG_EPHEMERAL_DIR /run/bdag-pool)"
  tmpfs_size="$(env_value BDAG_CONTAINER_TMPFS_SIZE "")"
  if [[ -z "$tmpfs_size" ]]; then
    mem_kb="$(awk '/MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    mem_gb=$(( mem_kb / 1024 / 1024 ))
    if (( mem_gb > 0 && mem_gb <= 4 )); then
      tmpfs_size="64m"
    else
      tmpfs_size="128m"
    fi
  fi

  set_env_value .env BDAG_EPHEMERAL_TMPFS_ENABLED "$enabled"
  set_env_value .env BDAG_EPHEMERAL_DIR "$ephemeral_dir"
  set_env_value .env BDAG_HOST_TMPDIR "$ephemeral_dir/tmp"
  set_env_value .env BDAG_CONTAINER_TMPFS_SIZE "$tmpfs_size"
  set_env_value .env BDAG_NODE_TMPFS_SIZE "$(env_value BDAG_NODE_TMPFS_SIZE 512m)"

  if [[ "$enabled" == "1" ]]; then
    if ! need_sudo mkdir -p "$ephemeral_dir/tmp" ||
      ! need_sudo chmod 0755 "$ephemeral_dir" ||
      ! need_sudo chmod 1777 "$ephemeral_dir/tmp"; then
      warn "Could not create $ephemeral_dir. Container tmpfs mounts will still protect in-container scratch; create the host ephemeral dir during host-profile install."
    fi
  fi
}

guard_runtime_compose() {
  if [[ ! -f docker-compose.yml ]]; then
    echo "Missing docker-compose.yml in release root." >&2
    exit 1
  fi
  if ! grep -q '^# BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1$' docker-compose.yml; then
    echo "This installer requires the generated Pi5 runtime compose. Refusing to start an unmarked compose file." >&2
    exit 1
  fi
  if grep -Eq '^[[:space:]]*(build|dockerfile):' docker-compose.yml; then
    echo "Runtime compose contains build/dockerfile entries. Refusing to overwrite the deployed image set." >&2
    exit 1
  fi
}

install_packages() {
  local packages=()
  if ! command -v python3 >/dev/null 2>&1; then
    packages+=(python3)
  elif ! python3 -c 'import cryptography' >/dev/null 2>&1; then
    packages+=(python3-cryptography)
  fi
  if ! disabled_mode "$(stack_default BDAG_BTRFS_CHECKPOINT_VOLUME_MODE auto)" && ! command -v mkfs.btrfs >/dev/null 2>&1; then
    packages+=(btrfs-progs)
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && (( ${#packages[@]} == 0 )); then
    return 0
  fi
  say "Installing Docker and helper packages"
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer expects Debian/Ubuntu with apt-get. Install Docker, Python cryptography, and rerun ./install.sh." >&2
    exit 1
  fi
  if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    packages+=(docker.io docker-compose-plugin curl jq rsync unzip zip zstd openssl iproute2)
  fi
  need_sudo apt-get update
  need_sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
  if [[ "$(id -u)" != "0" ]]; then
    need_sudo usermod -aG docker "$USER" || true
    warn "If Docker permission fails, log out and back in, or rerun with: sudo ./install.sh"
  fi
}

configure_env() {
  say "Preparing configuration"
  [[ -f .env ]] || cp .env.example .env
  configure_storage_profile
  configure_ephemeral_storage

  local lan_ip scan_target mining_address node_mining_enabled mem_kb mem_gb
  local miner_route_subnet miner_route_gateway miner_route_dev
  lan_ip="$(detect_lan_ip)"
  lan_ip="$(ask "Pool LAN IP miners should connect to" "${lan_ip:-192.168.1.10}")"
  scan_target="$(ask "LAN scan range for ASIC discovery" "$(default_cidr "$lan_ip")")"
  configure_miner_network
  mining_address="$(ask "Reward wallet address for this pool" "$(grep -E '^MINING_ADDRESS=' .env | cut -d= -f2-)")"
  if [[ -z "$mining_address" || "$mining_address" == "0x0000000000000000000000000000000000000000" ]]; then
    echo "A real reward wallet address is required." >&2
    exit 1
  fi
  node_mining_enabled=0
  if yes_no "Enable node mining/template support now? Choose yes only when miners are attached" "n"; then
    node_mining_enabled=1
  fi

  local node_rpc_pass postgres_password postgres_user postgres_db
  node_rpc_pass="$(random_secret)"
  postgres_password="$(random_secret)"
  postgres_user="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)"
  postgres_db="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)"
  postgres_user="${postgres_user:-test}"
  postgres_db="${postgres_db:-pool}"

  set_env_value .env MINING_ADDRESS "$mining_address"
  set_env_value .env NODE_RPC_PASS "$node_rpc_pass"
  set_env_value .env POSTGRES_USER "$postgres_user"
  set_env_value .env POSTGRES_PASSWORD "$postgres_password"
  set_env_value .env POSTGRES_DB "$postgres_db"
  set_env_value .env PG_URL "postgres://${postgres_user}:${postgres_password}@postgres:5432/${postgres_db}"
  set_env_value .env BDAG_POOL_HOST "$lan_ip"
  set_env_value .env BDAG_POOL_URL "stratum+tcp://$lan_ip:3334"
  set_env_value .env BDAG_MINER_SCAN_TARGET "$scan_target"
  set_env_value .env BDAG_ASIC_LAN_CIDRS "$scan_target"
  set_env_value .env BDAG_MINER_ROUTE_SUBNET "$miner_route_subnet"
  set_env_value .env BDAG_MINER_ROUTE_GATEWAY "$miner_route_gateway"
  set_env_value .env BDAG_MINER_ROUTE_DEV "$miner_route_dev"
  validate_pool_lan_config
  apply_stack_defaults_env .env
  configure_btrfs_checkpoint_volume
  set_stack_default_env_value .env BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED
  set_stack_default_env_value .env BDAG_CHAIN_PEERSTORE_LOG_TAIL
  set_stack_default_env_value .env BDAG_NODE_PEER_LIMIT
  set_stack_default_env_value .env BDAG_NODE_PEER_STABLE_PORTS
  set_env_value .env BDAG_INSTALL_APPLIANCE_HOST_PROFILE "$(env_value BDAG_INSTALL_APPLIANCE_HOST_PROFILE 1)"
  set_env_value .env BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES "$(env_value BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES 0)"
  set_env_value .env BDAG_INSTALL_APPLIANCE_PROFILE_RELOAD_DOCKER "$(env_value BDAG_INSTALL_APPLIANCE_PROFILE_RELOAD_DOCKER 1)"
  set_env_value .env BDAG_INSTALL_APPLIANCE_PROFILE_STRICT "$(env_value BDAG_INSTALL_APPLIANCE_PROFILE_STRICT 0)"
  set_env_value .env BDAG_INSTALL_STACK_SUPPORT_SERVICES "$(env_value BDAG_INSTALL_STACK_SUPPORT_SERVICES 1)"
  set_env_value .env BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT "$(env_value BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT 0)"
  set_existing_or_stack_default_env_value .env BDAG_CHAIN_STATE_RESTORE_IPFS_ARTIFACT_CID
  set_existing_or_stack_default_env_value .env BDAG_CHAIN_STATE_RESTORE_IPFS_INDEX_CID
  set_existing_or_stack_default_env_value .env BDAG_CHAIN_STATE_RESTORE_IPFS_INDEX_FILE
  set_env_value .env NODE_ARGS_APPEND ""
  set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_ARTIFACT_BASE
  set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_SIDECAR_DIR
  set_stack_default_env_value .env BDAG_RAWDATADIR_SIDECAR_MODE
  set_stack_default_env_value .env BDAG_RAWDATADIR_SIDECAR_CONTENT_MODE
  set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_SIDECAR_CONTENT_BASE
  set_stack_default_env_value .env BDAG_RAWDATADIR_SIDECAR_CONTENT_KEEP
  set_stack_default_env_value .env BDAG_RAWDATADIR_SIDECAR_CONTENT_REQUIRE_SIGNED
  set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_OPEN_SIDECAR_BASE
  set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_SIGNING_KEY_FILE
  set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_TRUSTED_SIGNERS
  set_existing_or_stack_default_env_value .env BDAG_RAWDATADIR_REQUIRE_TRUSTED_SIGNER
  set_stack_default_env_value .env BDAG_RAWDATADIR_REQUIRE_CHAIN_ANCHOR
  set_stack_default_env_value .env BDAG_RAWDATADIR_CHAIN_ANCHOR_REFERENCE_EVM_URL
  set_stack_default_env_value .env BDAG_RAWDATADIR_CHAIN_ANCHOR_TIMEOUT
  set_stack_default_env_value .env BDAG_RAWDATADIR_CHAIN_ANCHOR_FINALITY_BLOCKS
  set_env_value .env BDAG_RAWDATADIR_ACTIVE_SERVICE "node"
  set_stack_default_env_value .env BDAG_RAWDATADIR_FINALIZE
  set_env_value .env BDAG_RAWDATADIR_PEERS ""
  set_stack_default_env_value .env BDAG_IPFS_CONTENT_SIDECAR_MODE
  set_existing_or_stack_default_env_value .env BDAG_IPFS_CONTENT_ARTIFACT_DIR
  set_existing_or_stack_default_env_value .env BDAG_IPFS_CONTENT_ARTIFACT_MANIFEST
  set_stack_default_env_value .env BDAG_IPFS_CONTENT_ALLOW_UNSIGNED_ARTIFACT
  set_stack_default_env_value .env BDAG_IPFS_CONTENT_PUBLISH_IPNS
  set_env_value .env BDAG_IPFS_CONTENT_IPNS_KEY ""
  set_stack_default_env_value .env BDAG_IPFS_CONTENT_REPUBLISH_IPNS_WHILE_WAITING
  set_stack_default_env_value .env BDAG_IPFS_CONTENT_IPNS_TTL
  set_stack_default_env_value .env BDAG_IPFS_CONTENT_IPNS_LIFETIME
  set_env_value .env BDAG_IPFS_CONTENT_DISCOVERY_FILE "./ops/ipfs-content-discovery.json"
  set_env_value .env BDAG_IPFS_CONTENT_LATEST_IPNS "/ipns/k51qzi5uqu5djjlh4vxtmzyswx0qk4s3wdlf3yrpkszp38gq5sl71zcgmmc3jk"
  set_env_value .env BDAG_IPFS_CONTENT_DEFAULT_INDEX_CID "bafkreia7jk2ljqi3raiohugp6nw3633njfp7jmnuvqh47po52et4kupu2a"
  set_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_CONTENT_INDEX_PATH
  set_existing_or_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_CONTENT_DEFAULT_INDEX_CID
  set_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_CONTENT_PUBLISH_IPNS
  set_existing_or_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_CONTENT_IPNS_KEY
  set_stack_default_env_value .env BDAG_IPFS_STATE_CHECKPOINT_REQUIRED
  set_stack_default_env_value .env BDAG_RESTORE_POINT_MAX_AGE_SECONDS
  set_stack_default_env_value .env BDAG_RESTORE_GUARD_IPFS_TIMERS
  set_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_ENABLED
  set_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_INDEX_PATH
  set_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_STATUS_FILE
  set_existing_or_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_DEFAULT_CID
  set_existing_or_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_IPNS
  set_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_PUBLISH_IPFS
  set_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_MAX_PEERS
  set_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_REQUIRE_SIGNATURES
  set_stack_default_env_value .env BDAG_IPFS_PEER_ROSTER_ADD_ARGS
  set_stack_default_env_value .env BDAG_IPFS_CONTENT_DEFAULT_ROOT_CID
  set_env_value .env BDAG_IPFS_CONTENT_STATUS_FILE "./ops/runtime/ipfs-content-sidecar-status.json"
  set_env_value .env BDAG_IPFS_CONTENT_LATEST_INDEX_PATH "./ops/runtime/ipfs-content/latest-index.json"
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_WRITER_MODE
  set_existing_or_stack_default_env_value .env BDAG_IPFS_SEGMENT_WRITER_ID
  set_existing_or_stack_default_env_value .env BDAG_IPFS_SEGMENT_WRITER_ROSTER
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_WRITER_ELECTION_RULE
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_BOOTSTRAP_LOCAL_PUBLISH
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_BOOTSTRAP_UNTRUSTED_PUBLISH
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_START_POLICY
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_STALE_HEAD_RESET_ENABLED
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_STALE_HEAD_MAX_LAG_ORDERS
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_FINALITY_LAG_ORDERS
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_ORDERS_PER_SEGMENT
  set_stack_default_env_value .env BDAG_CHAIN_INTEGRITY_MAX_SEGMENT_ORDERS
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_MAX_SEGMENTS_PER_RUN
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_MAX_RPC_PER_SECOND
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_RPC_TIMEOUT
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_BLOCK_RPC_RETRIES
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_PUBLISH_IPNS
  set_env_value .env BDAG_IPFS_SEGMENT_IPNS_KEY ""
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_IPNS_TTL
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_IPNS_LIFETIME
  set_existing_or_stack_default_env_value .env BDAG_IPFS_SEGMENT_SIGNING_KEY_FILE
  set_existing_or_stack_default_env_value .env BDAG_IPFS_SEGMENT_TRUSTED_SIGNERS
  set_stack_default_env_value .env BDAG_IPFS_SEGMENT_REQUIRE_SIGNATURES
  set_env_value .env BDAG_IPFS_SEGMENT_STATUS_FILE "./ops/runtime/ipfs-content/segment-writer-status.json"
  set_env_value .env BDAG_IPFS_SEGMENT_INDEX_PATH "./ops/runtime/ipfs-content/latest-index.json"
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_MODE
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_MAX_SEGMENTS
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_MATERIALIZE
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_REQUIRE_SIGNATURES
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_VERIFY_INDEX_LINEAGE
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_MAX_INDEX_LINEAGE_DEPTH
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_ACCEPTED_HEAD_ENABLED
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_CHAIN_ANCHOR_ENABLED
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_REQUIRE_CHAIN_ANCHOR
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_CHAIN_SOURCE_RPC_URL
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_CHAIN_ANCHOR_FULL_SPAN_MAX_ORDERS
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_CHAIN_ANCHOR_SKIP_ENVIRONMENT_GATES
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_PRESTART_DRILL
  set_stack_default_env_value .env BDAG_IPFS_RESTORE_PRESTART_STRICT
  set_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART
  set_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART_STRICT
  set_existing_or_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_ARTIFACT_CID
  set_existing_or_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_CID
  set_existing_or_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_FILE
  set_existing_or_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_DISCOVERY_FILE
  set_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_STATUS_FILE
  set_stack_default_env_value .env BDAG_IPFS_RAWDATADIR_RESTORE_IPFS_TIMEOUT
  set_stack_default_env_value .env BDAG_IPFS_BACKFILL_INDEX_PATH
  set_stack_default_env_value .env BDAG_IPFS_BACKFILL_STATUS_FILE
  set_stack_default_env_value .env BDAG_IPFS_BACKFILL_START_ORDER
  set_stack_default_env_value .env BDAG_IPFS_BACKFILL_MAX_SEGMENTS_PER_RUN
  set_env_value .env BDAG_IPFS_RESTORE_STATUS_FILE "./ops/runtime/ipfs-content/restore-drill-status.json"
  set_env_value .env BDAG_IPFS_RESTORE_CANDIDATE_DIR "./ops/runtime/ipfs-content/restore-candidate"
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOTS
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_HOURS
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WINDOW_BLOCKS
  set_stack_default_env_value .env BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WORKERS
  set_stack_default_env_value .env BDAG_DASHBOARD_HISTORY_REBUILD_PRESERVE_ASIC_HISTORY
  set_stack_default_env_value .env BDAG_SYNC_COORDINATOR_FAST_RESTART_COOLDOWN_SECONDS
  set_stack_default_env_value .env BDAG_SYNC_COORDINATOR_RESTART_ON_STALE_IMPORT
  set_stack_default_env_value .env BDAG_CATCHUP_PAUSE_ENABLED
  set_stack_default_env_value .env BDAG_CATCHUP_PAUSE_THRESHOLD_BLOCKS
  set_stack_default_env_value .env BDAG_CATCHUP_IO_PRESSURE_PAUSE_ENABLED
  set_stack_default_env_value .env BDAG_CATCHUP_IO_PRESSURE_MIN_LAG_BLOCKS
  set_stack_default_env_value .env BDAG_CATCHUP_IOWAIT_WARN_PERCENT
  set_stack_default_env_value .env BDAG_CATCHUP_IO_SOME_AVG10_WARN
  set_stack_default_env_value .env BDAG_CATCHUP_IO_FULL_AVG10_WARN
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_RECREATE_ENABLED
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_CACHE_MB
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_CACHE_MIN_MB
  set_stack_default_env_value .env BDAG_CATCHUP_NODE_CACHE_MEMORY_PERCENT
  set_stack_default_env_value .env BDAG_HOST_PRESSURE_MEMORY_AVAILABLE_WARN_PERCENT
  set_stack_default_env_value .env BDAG_HOST_PRESSURE_SWAP_USED_WARN_PERCENT
  set_stack_default_env_value .env BDAG_ADAPTIVE_MEMORY_AVAILABLE_WARN_PERCENT
  set_stack_default_env_value .env BDAG_ADAPTIVE_SWAP_USED_WARN_PERCENT
  set_stack_default_env_value .env BDAG_SHARED_STATUS_CACHE_ENABLED
  set_stack_default_env_value .env BDAG_SHARED_STATUS_CACHE_SECONDS
  set_stack_default_env_value .env BDAG_STATUS_SAMPLER_ENABLED
  set_stack_default_env_value .env BDAG_STATUS_SAMPLER_INTERVAL_SECONDS
  set_stack_default_env_value .env BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS
  set_stack_default_env_value .env BDAG_BACKGROUND_MAINTENANCE_MEMORY_AVAILABLE_WARN_PERCENT
  set_stack_default_env_value .env BDAG_BACKGROUND_MAINTENANCE_SWAP_USED_WARN_PERCENT
  set_stack_default_env_value .env BDAG_BACKGROUND_MAINTENANCE_POOL_READY_STATUS_MAX_AGE_SECONDS
  set_stack_default_env_value .env BDAG_MINING_IMPERATIVE_CHAIN_STATE_RESTORE_ENABLED
  set_stack_default_env_value .env BDAG_CHAIN_STATE_MISSING_TRIE_RESTORE_WARNINGS
  set_stack_default_env_value .env BDAG_CHAIN_STATE_ACTIVE_MINING_DEFER_SECONDS
  configure_active_node_env
  configure_node_mining_env "$node_mining_enabled" "$mining_address"

  mem_kb="$(awk '/MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  mem_gb=$(( mem_kb / 1024 / 1024 ))
  if (( mem_gb > 0 && mem_gb <= 8 )); then
    say "Applying Pi/low-memory defaults"
    set_env_value .env BDAG_NODE_CACHE_MB 1024
    set_env_value .env NODE_MAX_PEERS 160
    set_env_value .env POSTGRES_SHARED_BUFFERS 256MB
    set_env_value .env POSTGRES_EFFECTIVE_CACHE_SIZE 1GB
  fi

  if yes_no "Expose the local dashboard on the LAN instead of only this machine?" "n"; then
    set_env_value .env BDAG_DASHBOARD_BIND "0.0.0.0"
  fi

}

provision_ipfs_segment_identity() {
  if [[ ! -x ops/ipfs_segment_identity.py ]]; then
    warn "Cannot provision IPFS segment writer identity: ops/ipfs_segment_identity.py is missing."
    return 0
  fi
  say "Provisioning signed IPFS segment writer identity"
  if ! python3 ops/ipfs_segment_identity.py --env-file "$ROOT/.env" --json >/dev/null; then
    echo "IPFS segment writer identity provisioning failed. Install python3-cryptography and rerun ./install.sh." >&2
    exit 1
  fi
}

install_appliance_host_profile() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  case "${BDAG_INSTALL_APPLIANCE_HOST_PROFILE:-1}" in
    0|false|False|no|No|off|Off)
      warn "Skipping appliance host profile because BDAG_INSTALL_APPLIANCE_HOST_PROFILE=${BDAG_INSTALL_APPLIANCE_HOST_PROFILE:-0}."
      return 0
      ;;
  esac
  if [[ ! -f scripts/install-mining-appliance-profile.sh ]]; then
    warn "Cannot install appliance host profile: scripts/install-mining-appliance-profile.sh is missing."
    return 0
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "Cannot install appliance host profile: systemctl is not available on this host."
    return 0
  fi

  local args=()
  if [[ "${BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES:-0}" != "1" ]]; then
    args+=(--no-disable-services)
  fi
  if [[ "${BDAG_INSTALL_APPLIANCE_PROFILE_RELOAD_DOCKER:-1}" != "1" ]]; then
    args+=(--no-docker-reload)
  fi

  say "Installing non-destructive mining appliance host profile"
  if bash scripts/install-mining-appliance-profile.sh "${args[@]}"; then
    return 0
  fi
  if [[ "${BDAG_INSTALL_APPLIANCE_PROFILE_STRICT:-0}" == "1" ]]; then
    echo "Appliance host profile installation failed and strict mode is enabled." >&2
    exit 1
  fi
  warn "Appliance host profile installation failed. Continuing because BDAG_INSTALL_APPLIANCE_PROFILE_STRICT=0."
}

run_appliance_preflight() {
  if [[ "${BDAG_APPLIANCE_PREFLIGHT:-1}" != "1" ]]; then
    warn "Skipping mining appliance preflight because BDAG_APPLIANCE_PREFLIGHT=0."
    return 0
  fi
  if [[ ! -f scripts/mining-appliance-preflight.py ]]; then
    warn "Mining appliance preflight script is missing from this package."
    return 0
  fi

  say "Running mining appliance preflight"
  if [[ "${BDAG_APPLIANCE_PREFLIGHT_STRICT:-0}" == "1" ]]; then
    python3 scripts/mining-appliance-preflight.py --root "$ROOT" --env-file "$ROOT/.env"
  else
    python3 scripts/mining-appliance-preflight.py --root "$ROOT" --env-file "$ROOT/.env" --warn-only --enforce-blockers
  fi
}

load_or_build_images() {
  local arch="$1"
  say "Loading BlockDAG images for linux/$arch"
  local image_dir="artifacts/images/linux-$arch"
  local loaded=0

  if compgen -G "$image_dir/*.tar.zst" >/dev/null; then
    for image in "$image_dir"/*.tar.zst; do
      echo "Loading $image"
      zstd -dc "$image" | "${DOCKER[@]}" load
      loaded=1
    done
  fi

  if (( loaded == 0 )); then
    say "No prebuilt image archives found; building local images from bundled binaries"
    if command -v ionice >/dev/null 2>&1; then
      ionice -c 3 nice -n 19 src/build-images.sh "$arch" "bundle"
    else
      nice -n 19 src/build-images.sh "$arch" "bundle"
    fi
  fi

  if "${DOCKER[@]}" image inspect "bdag-release/asic-pool:bundle-$arch" >/dev/null 2>&1; then
    "${DOCKER[@]}" tag "bdag-release/asic-pool:bundle-$arch" bdag-release/asic-pool:local
  fi
  if "${DOCKER[@]}" image inspect "bdag-release/node:bundle-$arch" >/dev/null 2>&1; then
    "${DOCKER[@]}" tag "bdag-release/node:bundle-$arch" bdag-release/node:local
  fi
}

find_or_extract_chain_seed() {
  if [[ -f chain-data/chain-data-seed.zip ]]; then
    printf '%s\n' "chain-data/chain-data-seed.zip"
    return 0
  fi

  local candidate
  for candidate in "$ROOT"/*chain-data*.zip "$ROOT"/../*chain-data*.zip; do
    [[ -f "$candidate" ]] || continue
    if unzip -l "$candidate" 'chain-data/chain-data-seed.zip' >/dev/null 2>&1; then
      say "Extracting chain seed from separate data package: $candidate"
      unzip -qo "$candidate" 'chain-data/chain-data-seed.zip' -d "$ROOT"
      printf '%s\n' "chain-data/chain-data-seed.zip"
      return 0
    fi
    if unzip -l "$candidate" 'mainnet/*' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

seed_chain_data() {
  local seed chain_base node_dir template_dir
  if ! seed="$(find_or_extract_chain_seed)"; then
    warn "No separate chain-data seed found. The node will sync from configured P2P peers."
    warn "If you received chain-data parts, reassemble them first, then rerun ./install.sh."
    return 0
  fi

  chain_base="$(env_path_value BDAG_CHAIN_DATA_DIR data)"
  node_dir="$(env_path_value BDAG_NODE_DATA_DIR "$chain_base/node")"
  template_dir="$chain_base/chain-template"
  if [[ -z "$template_dir" || "$template_dir" == "/" ]]; then
    echo "Refusing unsafe chain template directory: $template_dir" >&2
    exit 1
  fi

  if [[ -d "$node_dir/mainnet/BdagChain" ]]; then
    if ! yes_no "Existing node chain data was found. Replace it from the chain seed?" "n"; then
      return 0
    fi
    mv "$node_dir" "$node_dir.backup.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    mkdir -p "$node_dir"
  fi

  say "Unpacking one chain seed for the configured node datadir"
  rm -rf "$template_dir"
  mkdir -p "$template_dir" "$node_dir"
  unzip -q "$seed" -d "$template_dir"
  if [[ -d "$template_dir/chain-data" ]]; then
    rsync -a "$template_dir/chain-data/" "$node_dir/"
  else
    rsync -a "$template_dir/" "$node_dir/"
  fi
}

node_chain_markers_present() {
  local chain_base node_dir network_dir
  chain_base="$(env_path_value BDAG_CHAIN_DATA_DIR data)"
  node_dir="$(env_path_value BDAG_NODE_DATA_DIR "$chain_base/node")"
  network_dir="$node_dir/mainnet"
  [[ -d "$network_dir/BdagChain" || -d "$network_dir/bdageth/chaindata" || -d "$network_dir/chaindata" ]]
}

run_prestart_ipfs_rawdatadir_restore() {
  local enabled strict artifact_cid index_cid index_file discovery status_file timeout chain_base node_dir network_dir
  local args=()
  enabled="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART 1)"
  case "$enabled" in
    0|false|False|no|No|off|Off)
      return 0
      ;;
  esac
  if node_chain_markers_present; then
    say "Existing mainnet chain markers found; skipping pre-start IPFS raw-datadir restore"
    return 0
  fi
  if [[ ! -x ops/restore-rawdatadir-segment-artifact.py ]]; then
    warn "Cannot run pre-start IPFS raw-datadir restore: ops/restore-rawdatadir-segment-artifact.py is missing."
    return 0
  fi
  artifact_cid="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_ARTIFACT_CID "")"
  index_cid="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_CID "")"
  index_file="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_FILE "")"
  discovery="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_DISCOVERY_FILE "")"
  if [[ -z "$artifact_cid" && -z "$index_cid" && -z "$index_file" && -z "$discovery" ]]; then
    warn "No IPFS raw-datadir restore artifact/index is configured. The node will continue with normal P2P sync."
    return 0
  fi
  if ! command -v "$(env_value BDAG_IPFS_BINARY ipfs)" >/dev/null 2>&1; then
    warn "IPFS raw-datadir restore is configured, but the IPFS/Kubo CLI is not available. Install Kubo or set BDAG_IPFS_BINARY."
    strict="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART_STRICT 0)"
    if [[ "$strict" =~ ^(1|true|True|yes|Yes|on|On)$ ]]; then
      exit 1
    fi
    return 0
  fi
  chain_base="$(env_path_value BDAG_CHAIN_DATA_DIR data)"
  node_dir="$(env_path_value BDAG_NODE_DATA_DIR "$chain_base/node")"
  network_dir="$node_dir/mainnet"
  status_file="$(env_path_value BDAG_IPFS_RAWDATADIR_RESTORE_STATUS_FILE "ops/runtime/ipfs-content/rawdatadir-restore-status.json")"
  timeout="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_IPFS_TIMEOUT 600)"
  mkdir -p "$network_dir" "$(dirname "$status_file")"
  args=(--target-dir "$network_dir" --status-file "$status_file" --ipfs-timeout "$timeout" --network mainnet)
  if [[ -n "$artifact_cid" ]]; then
    args+=(--ipfs-artifact-cid "$artifact_cid")
  elif [[ -n "$index_cid" ]]; then
    args+=(--ipfs-index-cid "$index_cid")
  elif [[ -n "$index_file" ]]; then
    args+=(--ipfs-index-file "$index_file")
  else
    args+=(--discovery "$discovery")
  fi
  if [[ -n "$(env_value BDAG_RAWDATADIR_TRUSTED_SIGNERS "")" ]]; then
    args+=(--trusted-signers "$(env_value BDAG_RAWDATADIR_TRUSTED_SIGNERS "")")
  fi

  say "Restoring initial chain data from verified IPFS raw-datadir artifact"
  if python3 ops/restore-rawdatadir-segment-artifact.py "${args[@]}"; then
    say "IPFS raw-datadir restore completed for empty mainnet datadir"
    return 0
  fi
  strict="$(env_value BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART_STRICT 0)"
  if [[ "$strict" =~ ^(1|true|True|yes|Yes|on|On)$ ]]; then
    echo "Pre-start IPFS raw-datadir restore failed and BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART_STRICT=1." >&2
    exit 1
  fi
  warn "Pre-start IPFS raw-datadir restore failed. Continuing because BDAG_IPFS_RAWDATADIR_RESTORE_PRESTART_STRICT=0."
}

run_prestart_ipfs_restore_drill() {
  local enabled strict status_file max_segments args=()
  enabled="$(env_value BDAG_IPFS_RESTORE_PRESTART_DRILL 1)"
  case "$enabled" in
    0|false|False|no|No|off|Off)
      return 0
      ;;
  esac
  if node_chain_markers_present; then
    say "Existing mainnet chain markers found; skipping pre-start IPFS restore drill"
    return 0
  fi
  if [[ ! -x ops/ipfs_restore_drill.py ]]; then
    warn "Cannot run pre-start IPFS restore drill: ops/ipfs_restore_drill.py is missing."
    return 0
  fi
  status_file="$(env_path_value BDAG_IPFS_RESTORE_STATUS_FILE "ops/runtime/ipfs-content/restore-drill-status.json")"
  max_segments="$(env_value BDAG_IPFS_RESTORE_MAX_SEGMENTS 16)"
  args=(--status-file "$status_file" --max-segments "$max_segments")
  if [[ "$(env_value BDAG_IPFS_RESTORE_MATERIALIZE 0)" =~ ^(1|true|True|yes|Yes|on|On)$ ]]; then
    args+=(--materialize)
  fi

  say "Running pre-start IPFS restore drill for empty node datadir"
  if python3 ops/ipfs_restore_drill.py "${args[@]}"; then
    warn "IPFS archive verified into restore drill status. Segment-to-node import is not enabled yet, so install will continue with normal sync."
    return 0
  fi
  strict="$(env_value BDAG_IPFS_RESTORE_PRESTART_STRICT 0)"
  if [[ "$strict" =~ ^(1|true|True|yes|Yes|on|On)$ ]]; then
    echo "Pre-start IPFS restore drill failed and BDAG_IPFS_RESTORE_PRESTART_STRICT=1." >&2
    exit 1
  fi
  warn "Pre-start IPFS restore drill failed. Continuing because BDAG_IPFS_RESTORE_PRESTART_STRICT=0."
}

start_stack() {
  say "Starting BlockDAG sync services"
  guard_runtime_compose
  python3 ops/automation_control.py ensure-normal \
    --owner release-installer \
    --owner-unit release-install \
    --reason "Provision default automation control before sync-only first start" >/dev/null
  if [[ "${BDAG_RELEASE_PULL_BASE_IMAGES:-0}" == "1" ]]; then
    compose_cmd pull postgres || true
  else
    warn "Skipping implicit image pulls. Set BDAG_RELEASE_PULL_BASE_IMAGES=1 for an explicit base-image refresh."
  fi
  compose_cmd up -d --no-build --pull never --no-deps postgres node dashboard
  compose_cmd ps
}

install_stack_support_services() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  case "${BDAG_INSTALL_STACK_SUPPORT_SERVICES:-1}" in
    0|false|False|no|No|off|Off)
      warn "Skipping P2P/IPFS/mining-host support services because BDAG_INSTALL_STACK_SUPPORT_SERVICES=${BDAG_INSTALL_STACK_SUPPORT_SERVICES:-0}."
      return 0
      ;;
  esac
  if [[ ! -f ops/install-p2p-services.sh ]]; then
    warn "Cannot install P2P/IPFS/mining-host support services: ops/install-p2p-services.sh is missing."
    return 0
  fi

  say "Installing P2P, IPFS, and mining-host tuning services"
  if bash ops/install-p2p-services.sh; then
    return 0
  fi
  if [[ "${BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT:-0}" == "1" ]]; then
    echo "Support service installation failed and strict mode is enabled." >&2
    exit 1
  fi
  warn "Support service installation failed. Continuing because BDAG_INSTALL_STACK_SUPPORT_SERVICES_STRICT=0."
}

discover_preserved_chain_peers() {
  local runtime_dir manifest
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  if [[ "${BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED:-1}" == "0" ]]; then
    warn "Preserved chain peerstore extraction disabled by BDAG_CHAIN_PEERSTORE_PEER_EXTRACTION_ENABLED=0."
    return 0
  fi
  if [[ ! -f ops/update-local-peers.py ]]; then
    warn "Cannot extract preserved chain peers: missing ops/update-local-peers.py."
    return 0
  fi
  runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "ops/runtime")"
  manifest="$runtime_dir/peer-discovery-current.json"

  say "Extracting sync peer candidates from preserved chain evidence"
  if python3 ops/update-local-peers.py --env-file "$ROOT/.env" --force-apply; then
    echo "Peer discovery manifest: $manifest"
    if [[ -s "$runtime_dir/live-peers-current.txt" ]]; then
      echo "TCP-open peer candidates:"
      sed 's/^/  /' "$runtime_dir/live-peers-current.txt"
    else
      warn "No TCP-open peer candidates were discovered. Continue only if normal sync readiness later proves the node has peers and fresh templates."
    fi
  else
    warn "Peer discovery from preserved chain evidence failed. Continue only after validating peer connectivity, sync freshness, and template health."
  fi
}

rebuild_dashboard_plot_data() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
  if [[ "${BDAG_INSTALL_REBUILD_DASHBOARD_PLOTS:-1}" == "0" ]]; then
    warn "Dashboard plot rebuild disabled by BDAG_INSTALL_REBUILD_DASHBOARD_PLOTS=0."
    return 0
  fi
  if [[ ! -f ops/rebuild_dashboard_plot_history.py ]]; then
    warn "Cannot rebuild dashboard plot data: missing ops/rebuild_dashboard_plot_history.py."
    return 0
  fi

  local runtime_dir log_file hours window_blocks workers
  runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "ops/runtime")"
  mkdir -p "$runtime_dir/logs"
  log_file="$runtime_dir/logs/dashboard-rpc-history-rebuild-install.log"
  hours="${BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_HOURS:-720}"
  window_blocks="${BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WINDOW_BLOCKS:-64}"
  workers="${BDAG_INSTALL_REBUILD_DASHBOARD_PLOT_WORKERS:-12}"

  say "Rebuilding dashboard Global and Wallet plot data from local chain RPC"
  local cmd=(
    python3 ops/rebuild_dashboard_plot_history.py
    --install
    --write-report
    --hours "$hours"
    --window-blocks "$window_blocks"
    --workers "$workers"
  )
  if command -v ionice >/dev/null 2>&1; then
    cmd=(ionice -c 3 nice -n 19 "${cmd[@]}")
  else
    cmd=(nice -n 19 "${cmd[@]}")
  fi
  if BDAG_DASHBOARD_HISTORY_REBUILD_LOG_FILE="$log_file" "${cmd[@]}" >"$log_file" 2>&1; then
    echo "Dashboard plot rebuild complete: $log_file"
  else
    warn "Dashboard plot rebuild did not finish cleanly. See $log_file."
    tail -n 40 "$log_file" >&2 || true
  fi
}

install_dashboard() {
  if yes_no "Install the local dashboard/watchdog service?" "y"; then
    local runtime_dir
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    runtime_dir="$(env_path_value BDAG_RUNTIME_DIR "ops/runtime")"
    ops/install-dashboard.sh --bind "${BDAG_DASHBOARD_BIND:-127.0.0.1}" --port "${BDAG_DASHBOARD_PORT:-8088}" --runtime-dir "$runtime_dir" || true
  fi
}

configure_miners() {
  if yes_no "After initial sync, scan the LAN and optionally configure discovered miner sources now?" "n"; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    python3 tools/miner_wizard.py \
      --scan-target "${BDAG_MINER_SCAN_TARGET:-}" \
      --pool-url "${BDAG_POOL_URL:-stratum+tcp://${BDAG_POOL_HOST:-127.0.0.1}:3334}" \
      --worker "$MINING_ADDRESS"
  fi
}

main() {
  local arch
  arch="$(detect_arch)" || { echo "Unsupported architecture: $(uname -m)" >&2; exit 2; }
  install_packages
  init_docker_access
  enforce_wired_route_policy
  configure_env
  provision_ipfs_segment_identity
  install_appliance_host_profile
  run_appliance_preflight
  load_or_build_images "$arch"
  seed_chain_data
  run_prestart_ipfs_rawdatadir_restore
  run_prestart_ipfs_restore_drill
  start_stack
  install_stack_support_services
  discover_preserved_chain_peers
  rebuild_dashboard_plot_data
  install_dashboard
  configure_miners
  say "Install complete"
  echo "Stratum: ${BDAG_POOL_URL:-$(grep '^BDAG_POOL_URL=' .env | cut -d= -f2-)}"
  echo "Dashboard: http://${BDAG_DASHBOARD_BIND:-127.0.0.1}:${BDAG_DASHBOARD_PORT:-8088}"
  echo "Run ./tools/status.sh for a status check."
}

main "$@"

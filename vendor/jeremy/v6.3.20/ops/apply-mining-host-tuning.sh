#!/usr/bin/env bash
set -euo pipefail

# Passive mining-host tuning. This is intentionally safe to reapply: it adjusts
# block-device queue/read-ahead, Docker weights, and process scheduler hints
# without changing chain data, node topology, ASIC configuration, or service
# state.
#
# Policy: paid block production wins local contention. The active node, pool,
# and PostgreSQL get high work-conserving CPU/IO weights. Dashboard,
# observability, release seeding, browser, and maintenance work yield under load.

ROOT="${BDAG_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
read_ahead_kb="${BDAG_BLOCK_READ_AHEAD_KB:-1024}"
nr_requests="${BDAG_BLOCK_NR_REQUESTS:-256}"
active_node_nice="${BDAG_MINING_ACTIVE_NODE_NICE:--10}"
pool_nice="${BDAG_MINING_POOL_NICE:--8}"
observability_nice="${BDAG_OBSERVABILITY_NICE:-15}"
desktop_nice="${BDAG_DESKTOP_BACKGROUND_NICE:-19}"
pool_metrics_url="${BDAG_POOL_METRICS_URL:-http://127.0.0.1:9090/metrics}"
sync_state_file="${BDAG_SYNC_COORDINATOR_STATE_FILE:-$ROOT/ops/runtime/sync-coordinator-state.json}"
node_memory_low="${BDAG_NODE_MEMORY_LOW:-768M}"
node_memory_high="${BDAG_NODE_MEMORY_HIGH:-auto}"
node_memory_high_percent="${BDAG_NODE_MEMORY_HIGH_PERCENT:-60}"
node_memory_high_min="${BDAG_NODE_MEMORY_HIGH_MIN:-3072M}"
pool_memory_low="${BDAG_POOL_MEMORY_LOW:-256M}"
pool_db_memory_low="${BDAG_POOL_DB_MEMORY_LOW:-512M}"
dashboard_memory_low="${BDAG_DASHBOARD_MEMORY_LOW:-64M}"

log() {
  printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*"
}

env_value() {
  key="$1"
  fallback="${2:-}"
  value=""
  for env_file in "$ROOT/.env"; do
    [ -f "$env_file" ] || continue
    value="$(sed -n "s/^${key}=//p" "$env_file" | tail -n1 || true)"
    [ -n "$value" ] && break
  done
  printf '%s\n' "${value:-$fallback}"
}

compose_project_name() {
  env_value COMPOSE_PROJECT_NAME "${COMPOSE_PROJECT_NAME:-}"
}

block_device_for_path() {
  source="$(findmnt -no SOURCE -T "$1" 2>/dev/null || true)"
  [ -n "$source" ] || return 0
  name="$(lsblk -no PKNAME "$source" 2>/dev/null | head -n1 || true)"
  if [ -z "$name" ]; then
    name="$(basename "$source" | sed -E 's/p?[0-9]+$//')"
  fi
  [ -n "$name" ] && printf '%s\n' "$name"
}

tune_block_device() {
  queue="/sys/block/$1/queue"
  [ -d "$queue" ] || return 0
  [ -w "$queue/read_ahead_kb" ] && printf '%s\n' "$read_ahead_kb" > "$queue/read_ahead_kb" || true
  [ -w "$queue/nr_requests" ] && printf '%s\n' "$nr_requests" > "$queue/nr_requests" || true
  log "block_device=$1 read_ahead_kb=$(cat "$queue/read_ahead_kb" 2>/dev/null || echo unknown) nr_requests=$(cat "$queue/nr_requests" 2>/dev/null || echo unknown)"
}

renice_pids() {
  value="$1"
  shift
  for pid in "$@"; do
    [ -n "$pid" ] && renice -n "$value" -p "$pid" >/dev/null 2>&1 || true
  done
}

ionice_pids() {
  class="$1"
  priority="$2"
  shift 2
  command -v ionice >/dev/null 2>&1 || return 0
  for pid in "$@"; do
    [ -n "$pid" ] || continue
    if [ "$class" = "3" ]; then
      ionice -c "$class" -p "$pid" >/dev/null 2>&1 || true
    else
      ionice -c "$class" -n "$priority" -p "$pid" >/dev/null 2>&1 || true
    fi
  done
}

oom_score_pids() {
  score="$1"
  shift
  for pid in "$@"; do
    [ -n "$pid" ] || continue
    proc_file="/proc/$pid/oom_score_adj"
    [ -w "$proc_file" ] && printf '%s\n' "$score" > "$proc_file" || true
  done
}

tune_pids() {
  nice_value="$1"
  io_class="$2"
  io_priority="$3"
  oom_score="$4"
  shift 4
  [ "$#" -gt 0 ] || return 0
  renice_pids "$nice_value" "$@"
  ionice_pids "$io_class" "$io_priority" "$@"
  oom_score_pids "$oom_score" "$@"
}

docker_container_exists() {
  docker inspect "$1" >/dev/null 2>&1
}

first_live_container() {
  for container in "$@"; do
    [ -n "$container" ] || continue
    if docker_container_exists "$container"; then
      printf '%s\n' "$container"
      return 0
    fi
  done
}

compose_service_container() {
  service="$1"
  project="$(compose_project_name)"
  if [ -n "$project" ]; then
    docker ps \
      --filter "label=com.docker.compose.project=$project" \
      --filter "label=com.docker.compose.service=$service" \
      --format '{{.Names}}' 2>/dev/null | head -n1
    return 0
  fi
  docker ps \
    --filter "label=com.docker.compose.service=$service" \
    --format '{{.Names}}' 2>/dev/null | head -n1
}

service_container() {
  service="$1"
  configured=""
  project="$(compose_project_name)"
  shift
  case "$service" in
    node) configured="${BDAG_NODE_CONTAINER:-$(env_value BDAG_NODE_CONTAINER "")}" ;;
    pool) configured="${BDAG_POOL_CONTAINER:-$(env_value BDAG_POOL_CONTAINER "")}" ;;
    postgres) configured="${BDAG_POOL_DB_CONTAINER:-${BDAG_POSTGRES_CONTAINER:-$(env_value BDAG_POOL_DB_CONTAINER "")}}" ;;
    dashboard) configured="${BDAG_DASHBOARD_CONTAINER:-$(env_value BDAG_DASHBOARD_CONTAINER "")}" ;;
  esac

  if [ -n "$configured" ] && docker_container_exists "$configured"; then
    printf '%s\n' "$configured"
    return 0
  fi

  found="$(compose_service_container "$service")"
  if [ -n "$found" ]; then
    printf '%s\n' "$found"
    return 0
  fi

  first_live_container "$@" "${project:+$project-$service-1}"
}

container_cgroup_root() {
  container="$1"
  pid="$(docker inspect -f '{{.State.Pid}}' "$container" 2>/dev/null || true)"
  case "$pid" in
    ''|0) return 0 ;;
  esac
  cgroup_path="$(awk -F: '$1 == "0" || $2 == "" { print $3; exit }' "/proc/$pid/cgroup" 2>/dev/null || true)"
  [ -n "$cgroup_path" ] || return 0
  printf '/sys/fs/cgroup%s\n' "$cgroup_path"
}

docker_container_pids() {
  docker_container_exists "$1" || return 0
  cgroup_root="$(container_cgroup_root "$1")"
  if [ -n "$cgroup_root" ] && [ -r "$cgroup_root/cgroup.procs" ]; then
    awk '$1 ~ /^[0-9]+$/ { print $1 }' "$cgroup_root/cgroup.procs"
    return 0
  fi
  docker top "$1" -eo pid 2>/dev/null | awk 'NR > 1 && $1 ~ /^[0-9]+$/ { print $1 }' || true
}

docker_update_one() {
  container="$1"
  cpu_shares="$2"
  blkio_weight="$3"
  docker_container_exists "$container" || return 0
  # Docker Compose owns OOMScoreAdj at container create time. docker update in
  # common distro builds does not support --oom-score-adj, so runtime tuning
  # only reapplies work-conserving CPU and block I/O weights.
  docker update \
    --cpu-shares "$cpu_shares" \
    --blkio-weight "$blkio_weight" \
    "$container" >/dev/null 2>&1 || true
}

bytes_value() {
  value="$1"
  case "$value" in
    *[Kk]) printf '%s\n' "$(( ${value%[Kk]} * 1024 ))" ;;
    *[Mm]) printf '%s\n' "$(( ${value%[Mm]} * 1024 * 1024 ))" ;;
    *[Gg]) printf '%s\n' "$(( ${value%[Gg]} * 1024 * 1024 * 1024 ))" ;;
    *) printf '%s\n' "$value" ;;
  esac
}

total_memory_bytes() {
  awk '$1 == "MemTotal:" { print $2 * 1024; exit }' /proc/meminfo 2>/dev/null || true
}

node_memory_high_bytes() {
  case "$node_memory_high" in
    ""|0|off|none|disabled)
      return 0
      ;;
    auto)
      total="$(total_memory_bytes)"
      case "$total" in
        ''|0) return 0 ;;
      esac
      min_bytes="$(bytes_value "$node_memory_high_min")"
      target="$(( total * node_memory_high_percent / 100 ))"
      if [ "$target" -lt "$min_bytes" ]; then
        target="$min_bytes"
      fi
      if [ "$target" -ge "$total" ]; then
        target="$(( total - 512 * 1024 * 1024 ))"
      fi
      [ "$target" -gt 0 ] && printf '%s\n' "$target"
      ;;
    *)
      bytes_value "$node_memory_high"
      ;;
  esac
}

write_cgroup_value() {
  path="$1"
  value="$2"
  [ -e "$path" ] || return 0
  if [ -w "$path" ]; then
    printf '%s\n' "$value" > "$path" 2>/dev/null || true
  fi
}

apply_cgroup_policy() {
  container="$1"
  cpu_weight="$2"
  io_weight="$3"
  memory_low="$4"
  memory_high="${5:-}"
  cgroup_root="$(container_cgroup_root "$container")"
  [ -n "$cgroup_root" ] && [ -d "$cgroup_root" ] || return 0
  write_cgroup_value "$cgroup_root/cpu.weight" "$cpu_weight"
  write_cgroup_value "$cgroup_root/io.weight" "default $io_weight"
  write_cgroup_value "$cgroup_root/memory.low" "$(bytes_value "$memory_low")"
  if [ -n "$memory_high" ]; then
    write_cgroup_value "$cgroup_root/memory.high" "$memory_high"
  fi
  log "container=$container cpu_weight=$(cat "$cgroup_root/cpu.weight" 2>/dev/null || echo unknown) io_weight=$(cat "$cgroup_root/io.weight" 2>/dev/null | head -n1 || echo unknown) memory_low=$(cat "$cgroup_root/memory.low" 2>/dev/null || echo unknown) memory_high=$(cat "$cgroup_root/memory.high" 2>/dev/null || echo unknown)"
}

selected_backend_from_metrics() {
  command -v curl >/dev/null 2>&1 || return 0
  curl -fsS --max-time 2 "$pool_metrics_url" 2>/dev/null |
    awk '
      $0 ~ /^pool_rpc_backend_selected/ && $0 ~ /} 1$/ {
        if (match($0, /backend="[^"]+"/)) {
          backend=substr($0, RSTART + 9, RLENGTH - 10)
          print backend
          exit
        }
      }'
}

selected_backend() {
  backend="$(selected_backend_from_metrics || true)"
  if [ -z "$backend" ]; then
    backend="node"
  fi
  case "$backend" in
    node|primary) printf '%s\n' "node" ;;
    *) printf '%s\n' "node" ;;
  esac
}

node_container_for_backend() {
  case "$1" in
    node) printf '%s\n' "node" ;;
    *) printf '%s\n' "node" ;;
  esac
}

sync_coordinator_leader_node() {
  [ -f "$sync_state_file" ] || return 0
  python3 - "$sync_state_file" <<'PY' 2>/dev/null || true
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        state = json.load(handle)
except Exception:
    raise SystemExit(0)

if state.get("mode") != "active_node_catchup":
    raise SystemExit(0)

leader = str(state.get("active_node") or "")
mapping = {
    "node": "node",
}
if leader in mapping:
    print(mapping[leader])
PY
}

tune_processes() {
  active_backend="$(selected_backend)"
  active_node="$(node_container_for_backend "$active_backend")"
  active_node="$(first_live_container "$active_node" "$(service_container node node)")"
  catchup_node="$(sync_coordinator_leader_node || true)"
  if [ -n "$catchup_node" ]; then
    active_node="$(first_live_container "$catchup_node" "$active_node")"
  fi

  for container in "$active_node" "$(service_container node node)"; do
    [ -n "$container" ] || continue
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$active_node_nice" 2 0 -950 $pids
  done

  for container in "$(service_container pool pool)" "$(service_container postgres postgres)"; do
    [ -n "$container" ] || continue
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$pool_nice" 2 0 -900 $pids
  done

  for container in \
    "$(service_container dashboard dashboard bdag-dashboard)" \
    bdag-prometheus bdag-grafana bdag-loki \
    bdag-alertmanager bdag-cadvisor bdag-alloy bdag-blackbox-exporter \
    bdag-exporter bdag-node-exporter bdag-postgres-exporter; do
    [ -n "$container" ] || continue
    pids="$(docker_container_pids "$container")"
    [ -n "$pids" ] && tune_pids "$observability_nice" 3 7 300 $pids
  done

  if [ "${BDAG_TUNE_DESKTOP_BACKGROUND:-1}" = "1" ]; then
    desktop_pids="$(pgrep -f '(/firefox|/chrome|/chromium|/code|Web Content|Socket Process|Utility Process|grafana|prometheus|loki|alloy|cadvisor|bdag_exporter.py)' 2>/dev/null || true)"
    if [ -n "$desktop_pids" ]; then
      # shellcheck disable=SC2086
      renice_pids "$desktop_nice" $desktop_pids
      # shellcheck disable=SC2086
      ionice_pids 3 7 $desktop_pids
    fi
  fi
}

tune_docker_weights() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  active_backend="$(selected_backend)"
  active_node="$(node_container_for_backend "$active_backend")"
  active_node="$(first_live_container "$active_node" "$(service_container node node)")"
  catchup_node="$(sync_coordinator_leader_node || true)"
  if [ -n "$catchup_node" ]; then
    active_node="$(first_live_container "$catchup_node" "$active_node")"
  fi

  if [ -n "$catchup_node" ]; then
    docker_update_one "$active_node" 8192 1000
  else
    docker_update_one "$active_node" 6144 1000
  fi
  for container in "$(service_container node node)"; do
    [ -n "$container" ] || continue
    [ "$container" = "$active_node" ] && continue
    docker_update_one "$container" 6144 1000
  done

  docker_update_one "$(service_container pool pool)" 5120 950
  docker_update_one "$(service_container postgres postgres)" 4096 950
  for container in \
    "$(service_container dashboard dashboard bdag-dashboard)" \
    bdag-prometheus bdag-grafana bdag-loki \
    bdag-alertmanager bdag-cadvisor bdag-alloy bdag-blackbox-exporter \
    bdag-exporter bdag-node-exporter bdag-postgres-exporter; do
    [ -n "$container" ] || continue
    docker_update_one "$container" 128 100
  done

  if [ -n "$catchup_node" ]; then
    log "resource_policy=leader-catchup active_backend=$active_backend active_node=$active_node"
  else
    log "resource_policy=active-passive active_backend=$active_backend active_node=$active_node"
  fi
}

tune_cgroups() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  node_container="$(service_container node node)"
  pool_container="$(service_container pool pool)"
  pool_db_container="$(service_container postgres postgres)"
  dashboard_container="$(service_container dashboard dashboard bdag-dashboard)"
  node_memory_high_value="$(node_memory_high_bytes)"

  [ -n "$node_container" ] && apply_cgroup_policy "$node_container" 10000 10000 "$node_memory_low" "$node_memory_high_value"
  [ -n "$pool_container" ] && apply_cgroup_policy "$pool_container" 8500 9500 "$pool_memory_low"
  [ -n "$pool_db_container" ] && apply_cgroup_policy "$pool_db_container" 8500 9500 "$pool_db_memory_low"
  [ -n "$dashboard_container" ] && apply_cgroup_policy "$dashboard_container" 250 100 "$dashboard_memory_low"
}

absolute_path() {
  path="$1"
  if [ -z "$path" ] || [ "$path" = "auto" ]; then
    return 0
  fi
  case "$path" in
    /*) printf '%s\n' "$path" ;;
    *) printf '%s\n' "$ROOT/${path#./}" ;;
  esac
}

default_network_iface() {
  ip -o route get 1.1.1.1 2>/dev/null |
    awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
}

active_lan_ifaces() {
  ip -o -4 addr show up scope global 2>/dev/null |
    awk -F': ' '
      {
        iface=$2
        sub(/@.*/, "", iface)
        sub(/[[:space:]].*/, "", iface)
        if (iface !~ /^(lo|docker|br-|veth|virbr|tailscale|zt|wg)/) {
          print iface
        }
      }'
}

network_ifaces() {
  if [ -n "${BDAG_STACK_NET_IFACE:-}" ]; then
    printf '%s\n' "$BDAG_STACK_NET_IFACE"
    return 0
  fi
  {
    asic_iface="$(env_value BDAG_ASIC_LAN_INTERFACE "")"
    [ -n "$asic_iface" ] && ip link show "$asic_iface" >/dev/null 2>&1 && printf '%s\n' "$asic_iface"
    p2p_iface="$(env_value BDAG_P2P_INTERFACE "")"
    [ -n "$p2p_iface" ] && ip link show "$p2p_iface" >/dev/null 2>&1 && printf '%s\n' "$p2p_iface"
    active_lan_ifaces
    default_network_iface
  } | awk 'NF && !seen[$0]++'
}

tune_network_queue() {
  [ "${BDAG_TUNE_NET_QDISC:-1}" = "1" ] || return 0
  command -v tc >/dev/null 2>&1 || return 0
  network_ifaces | while read -r iface; do
    [ -n "$iface" ] || continue
    tc qdisc replace dev "$iface" root fq_codel target 5ms interval 100ms ecn >/dev/null 2>&1 || true
    log "network_iface=$iface qdisc=$(tc qdisc show dev "$iface" 2>/dev/null | head -n1 || echo unknown)"
  done
}

devices="$(
  {
    chain_data_path="$(absolute_path "$(env_value BDAG_CHAIN_DATA_DIR "$ROOT/data")")"
    [ -n "$chain_data_path" ] && block_device_for_path "$chain_data_path"
    block_device_for_path "$ROOT"
    block_device_for_path /var/lib/docker
    block_device_for_path /
  } | awk 'NF' | sort -u
)"
for dev in $devices; do
  tune_block_device "$dev"
done
tune_docker_weights
tune_cgroups
tune_processes
tune_network_queue

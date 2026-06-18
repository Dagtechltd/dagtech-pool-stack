#!/usr/bin/env bash

run_low_priority() {
  local command=("$@")
  if command -v ionice >/dev/null 2>&1; then
    command=(ionice -c3 "${command[@]}")
  fi
  if command -v nice >/dev/null 2>&1; then
    command=(nice -n 19 "${command[@]}")
  fi
  "${command[@]}"
}

snapshot_sync_summary() {
  local project_root="$1"
  PYTHONPATH="$project_root/ops" python3 -c '
from pool_ops import collect_sync_progress, json_rpc_call, node_rpc_urls

progress = collect_sync_progress()
nodes = progress.get("nodes") or {}
remaining = []
unknown = 0
for item in nodes.values():
    if item.get("status") == "unknown":
        unknown += 1
    value = item.get("remaining_blocks")
    if value is not None:
        remaining.append(int(value))
max_remaining = max(remaining) if remaining else -1
blocks = []
for _, url in node_rpc_urls():
    try:
        value = json_rpc_call(url, "eth_blockNumber", [], timeout=4.0)
        blocks.append(int(str(value), 16))
    except Exception:
        unknown += 1
block_lag = max(blocks) - min(blocks) if len(blocks) >= 2 else -1
print(progress.get("status") or "unknown", max_remaining, unknown, block_lag)
'
}

snapshot_rsync_node() {
  local source_dir="$1"
  local stage_dir="$2"
  local bwlimit="${BDAG_SNAPSHOT_RSYNC_BWLIMIT_KB:-12000}"
  local rsync_args=(
    -a
    --delete-during
    --no-owner
    --no-group
    --chmod=Du+rwx,Dgo+rx,Fu+rw,Fgo+r
    --exclude=/mainnet/LOCK
    --exclude=/mainnet/network.key
    --exclude=/mainnet/peerstore/
    --exclude=/mainnet/keystore/
    --exclude=/mainnet/BdagChain/LOCK
    --exclude=/mainnet/bdageth/nodekey
    --exclude=/mainnet/bdageth/LOCK
    --exclude=/mainnet/bdageth/nodes/
    --exclude=/mainnet/bdageth/blobpool/
    --exclude=/mainnet/bdageth/transactions.rlp
    --exclude=/mainnet/bdageth/chaindata/LOCK
  )
  if [[ "$bwlimit" != "0" ]]; then
    rsync_args+=(--bwlimit="$bwlimit")
  fi

  mkdir -p "$stage_dir"
  run_low_priority rsync "${rsync_args[@]}" "$source_dir"/ "$stage_dir"/
}

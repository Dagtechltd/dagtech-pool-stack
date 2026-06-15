#!/usr/bin/env bash
# Pre-seed the cantina candidate node's chain data dir by hot-rsyncing from
# production node-1's running mainnet directory. Same trick we proved with
# Remz: excludes network.key + peerstore + LOCK so the candidate generates
# its own libp2p identity at first boot.
#
# Run as bdag on UAE node-1. Takes ~5-10 min when both sides are local.
set -euo pipefail

SRC="/home/bdag/blockdag-release/extracted/blockdag-pool-release-20260510-152225/data/node1/mainnet"
DST="/home/bdag/dagtech-pool-stack-cantina/data/mainnet"

echo "=== cantina chain pre-seed $(date -u +%FT%TZ) ==="
echo "src: $SRC"
echo "dst: $DST"
sudo du -sh "$SRC" | awk '{print "src size: "$1}'

mkdir -p "$DST"

sudo rsync -a --info=progress2 \
  --exclude='network.key' \
  --exclude='peerstore' \
  --exclude='peerstore.bak.*' \
  --exclude='keystore' \
  --exclude='keystore.bak.*' \
  --exclude='LOCK' \
  --exclude='bdageth/nodekey' \
  --exclude='bdageth/LOCK' \
  --exclude='bdageth/transactions.rlp' \
  "$SRC/" "$DST/"

sudo chown -R bdag:bdag "$DST"
sudo du -sh "$DST" | awk '{print "dst size: "$1}'

echo "=== pre-seed done $(date -u +%FT%TZ) ==="
echo "next: docker compose -f /home/bdag/dagtech-pool-stack-cantina/cantina-candidate.compose.yml up -d"

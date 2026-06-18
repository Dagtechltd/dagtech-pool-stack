# BlockDAG Collector

This repository is the canonical source for the read-only Python collector API
used by the BlockDAG ASIC pool dashboard.

## Source Of Truth

- Live origin captured from: `/home/jeremy/blockdag-asic-pool/ops`
- Collector entrypoint: `ops/collector.py`
- Primary support library: `ops/pool_ops.py`

The collector must stay passive: no repair actions, miner configuration routes,
action tokens, saved ASIC credentials, or HTML UI.

## Production Invariants

- Do not commit runtime data, `.env` files, dashboard tokens, miner admin
  passwords, chain data, logs, reports, private keys, or wallet secrets.
- Physical ASIC miners are identified persistently by MAC address only.
  IP addresses, worker labels, ports, display names, and pool-log labels are
  ephemeral observations.
- The dashboard miner column defaults to the full MAC address. If an operator
  assigns a human name, render it as `Name-abc` using the last three hex
  characters of the MAC. Never suffix or identify a physical ASIC by IP, and do
  not auto-generate site-specific names in fresh release installs.
- When any managed node is more than 1000 blocks behind the observed network
  tip, pause the laggiest running node, sync exactly one selected leader, and
  give that leader the highest Docker CPU/IO priority.
- The release candidate must be able to build the full pool stack, start the
  dashboard/support services, and sync a fresh node from the blockchain without
  relying on the retired dashboard repositories.

## Change Safety

Work in source first, run validation, then update the stack release candidate.
Do not restart live mining services, BlockDAG nodes, Docker, or ASICs unless
Jeremy explicitly asks for that operation.

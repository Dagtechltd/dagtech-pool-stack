# Cantina rollout — X100 Lane 1 first, measured, reversible

This document is the structured plan to swap our current X100 mining stack
(asic-pool + hans-solo + node1/2/3 + RPC failover, our own builds) for the
DagTech-flavored fork of BlockdagEngineering's `jeremy-dev-release v6.3.20`
stack vendored at `vendor/jeremy/v6.3.20/`.

**The bar:** the new stack must demonstrate an unambiguous performance
upshift against the gold-standard metric (BDAG/hour earned to the X100
wallet `0x6387C32ccDD60BfBa00EC70A67715Dcd52E8083f`). No upshift, no swap.

## Rollout sequence

| # | Node | Role | When | Owner |
|---|---|---|---|---|
| 1 | **UAE Cantina X100** | production mining node | Phase 1 | JARVIS, Boss approves |
| 2 | UAE node-2 (failover) | read-only relay | Phase 2 | JARVIS |
| 3 | Mac peer | archive + RPC | Phase 2 | JARVIS |
| 4 | Luke | community operator | Phase 3 | Luke + JARVIS |
| 5 | Remz | community operator | Phase 3 | Remz + JARVIS |
| 6 | Chad | community operator | Phase 3 | Chad + JARVIS |

Each phase is gated by the upshift verdict from the previous phase. No
phase advances until the gold-standard metric is met for **24 consecutive
hours**.

## The 3-phase benchmark methodology

### Phase A — Baseline (24h, no changes)

`/opt/jarvis/bin/jarvis-bench-cantina` runs every 60s with
`BENCH_PHASE=baseline`. Captures the current production stack on the
Cantina X100 node. JSONL output at
`/opt/jarvis/log/bench/baseline/bdag-MS-7E16.jsonl`.

### Phase B — Parallel deploy (no production touch, ~4h)

New stack stood up in a **side-by-side** namespace on the same host:
- Different docker network (`dagtech-pool-stack-net`)
- Different host ports (stratum **3344** instead of 3334, hans-solo
  candidate **3346** instead of 3336, dashboard **9281**, EVM **18555**)
- Different cgroup lane (Lane 4 — services — borrows 2 cores from there
  for the parallel run only)
- Different chain data dir (`/home/bdag/dagtech-pool-stack-cantina/data`)
  pre-seeded from a rsync of node-1's mainnet data (the trick we proved
  on Remz)
- Sentry remains pointed at the **production** Lane 1 ports during this
  phase — no overlap

The candidate stack is run with a **test wallet** (or zero wallet) so it
generates work + accepts shares but the **rewards still flow to the
production wallet via the X100 → production hans-solo channel**. No
revenue at risk during phase B.

### Phase C — Cutover (single transaction, instrumented)

When phase B health checks pass (containers stable, peers > 10, EVM tip
matching production within 1 block, candidate stratum accepting probes,
mining-pool log shows job push every < 2s):

1. `BENCH_PHASE=implementation jarvis-bench-cantina` (15-min ramp window)
2. Repoint X100 firmware pool slot 0 from `192.168.1.21:3336` to
   `192.168.1.21:3346` (candidate). The X100 firmware "one-pool-only"
   rule (memory `x100_firmware_single_pool_only`) means this is a single
   PUT to `/mcb/pools` followed by a `PUT /mcb/restart`.
3. Sentry shadow-watches both old and new for the first 5 min.
4. If first 3 shares accepted within 90s: continue.
   If not: rollback (revert X100 pool URL, takes 15s).
5. `BENCH_PHASE=post jarvis-bench-cantina` starts the post-cutover
   measurement window.

### Phase D — Post-bench (24h)

Same capture script, `BENCH_PHASE=post`. Then:

```bash
jarvis-bench-compare baseline post --hours 24
```

Produces a markdown delta report. Exit code:
- `0` = ✅ upshift confirmed (wallet earnings rate up ≥ 5%) → **promote**
- `1` = ~ inconclusive (within ±5% on gold metric) → **investigate**
- `2` = ⚠ regression → **roll back** (revert X100 pool URL, tear down candidate)

## What "amazing upshift" means here

The gold-standard metric is **BDAG/hour earned to the X100 wallet**,
measured by `eth_getBalance(0x6387C32c…, latest)` delta over the bench
window. Memory says today's benchmark is **100K BDAG/hour at ~280 MH/s**
(`x100_benchmark_and_design.md`).

For a promote verdict we want at minimum a **5% upshift** on BDAG/hour —
the upper bound the new mining-pool binary can plausibly deliver without
silicon change. Targets above that come from:

1. **Jeremy's `mining-pool` binary** (separate from `nathanbdagnetwork/asic-pool`) —
   different stratum scheduler may push notifies tighter, reducing X100
   idle gaps. Measured by `hans_solo.submit_lines` and inter-notify gap.
2. **Watchdog + sentinel** auto-heal — fewer vardiff death spirals, so
   fewer 0-MHs episodes in the histogram. Measured by `x100.MHS_20s`
   p5 percentile.
3. **mining_health_triage** / **mining_readiness_gate** — preempt
   degraded states. Measured by Sentry firing rate.
4. **ipfs_segment_writer** — chain snapshots without our manual rsync
   cycle. Measured by sync uptime % over the window.
5. **Native node binary** (Jeremy's `blockdag-node` instead of our
   `bdag-release/node:local` build) — same chain, possibly different
   cache defaults. Measured by EVM tip lag delta.

## Metric scoreboard (captured every 60s, both phases)

| Bucket | Field path | Goal | Notes |
|---|---|---|---|
| **Economic (gold)** | `wallet_bdag` rate | ↑ | delta over window / window_hours |
| Hashrate | `x100.MHS_av` | ↑ | mean and p5 |
| Hashrate stability | `x100.MHS_20s` | ↑ p5 | p5 measures vardiff spiral floor |
| Pool throughput | `hans_solo.submit_lines` | ↑ | submits in 60s window |
| Pool blocks | `hans_solo.block_lines` | ↑ | blocks found in 60s window |
| Pool rejects | `hans_solo.rejected_lines` | ↓ | rejected shares |
| Sync | `evm_block` rate | match prod | tip lag |
| P2P | `evm_peer_count` | ≥ 5 | not zero |
| Health | container counts | == expected | regressions = bad |
| Resource | `loadavg.0` | ↓ | should not pin |
| Resource | `mem.MemAvailable_kB` | ↑ | should not exhaust |
| Disk | `disk_chain.pct_used` | bounded | snapshot pruning works |
| Net | `net_eth0.tx_bytes` rate | ~match prod | not pathologically up |

## Rollback levers (in increasing radius)

1. **Soft rollback (15s)** — PUT X100 pool slot back to `192.168.1.21:3336`,
   PUT `/mcb/restart`. Production hans-solo never went away during phase B
   so it accepts traffic instantly. This is the rollback we expect to use
   if the post-cutover bench shows regression.
2. **Pool restart (45s)** — production hans-solo restarted.
3. **Stack rebuild (5 min)** — production `docker compose up -d` recreates
   any container that drifted during the parallel-deploy period.
4. **Chain rsync from node-3** — last resort, ~30 min.

Sentry stays armed throughout. If `chip_reject_spiral` fires during the
candidate cutover, JARVIS auto-rolls back per its `chip_reject_spiral`
runbook (cooldown 5 min, 3-strike disarm).

## Open questions to resolve before phase C

1. Which wallet to use during phase B — the production X100 wallet (test
   uses of the candidate flow into production accounting) or a fresh
   bench wallet (cleaner separation, but ~0 BDAG cost to set up)?
2. Whether to keep our 4-lane cgroup isolation as-is or adopt Jeremy's
   cpu_shares-only model (which doesn't pin cores). Affects the bench
   apples-to-apples comparison.
3. Whether to allow the candidate's `cpu-miner` service to run during
   bench (it will idle the host cores if active) — recommend leave it off.

These get answered in a pre-cutover review with Boss.

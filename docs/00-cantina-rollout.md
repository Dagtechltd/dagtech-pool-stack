# Cantina rollout — X100 Lane 1, measured, reversible

Structured swap of our current X100 mining stack (asic-pool + hans-solo +
node1/2/3 + RPC failover, our own builds) for the DagTech-flavored fork of
BlockdagEngineering's `jeremy-dev-release v6.3.20`.

**The bar:** the new stack must demonstrate an unambiguous performance
upshift on the gold-standard metric — BDAG/hour earned to the X100 wallet
`0x6387C32ccDD60BfBa00EC70A67715Dcd52E8083f`. No upshift, no swap.

## Locked decisions

| # | Question | Decision | Reason |
|---|---|---|---|
| 1 | Bench wallet | **Production X100 wallet `0x6387C32c…E8083f`** | Phase B candidate idles; no real X100 traffic to credit. Phase C/D continues same ledger. Avoids two X100 firmware writes during cutover. |
| 2 | cgroup model | **Hybrid 4-lane preserved.** Candidate runs in Lane 4 during Phase B; takes over Lane 1's cgroup spec at Phase C cutover. | Apples-to-apples in Phase D (same substrate, different binaries). 4-lane isolation memory `excalibur_4lane_isolation` proven essential after blockscout 1271% CPU incident. |
| 3 | `cpu-miner` service | **Off the entire bench** (`profiles: [disabled]` in candidate compose). | Removes confounding variable. We're testing Jeremy's mining-pool binary against ours via X100 ASIC — CPU mining is a separate question for a separate bench (community operators without ASICs). |

## Rollout sequence

| # | Node | Role | When | Owner |
|---|---|---|---|---|
| 1 | **UAE Cantina X100** | production mining node | Phase 1 | JARVIS, Boss greenlights cutover |
| 2 | UAE node-2 (failover) | read-only relay | Phase 2 | JARVIS |
| 3 | Mac peer | archive + RPC | Phase 2 | JARVIS |
| 4 | Luke | community operator | Phase 3 | Luke + JARVIS |
| 5 | Remz | community operator | Phase 3 | Remz + JARVIS |
| 6 | Chad | community operator | Phase 3 | Chad + JARVIS |

Each phase advances only on a passing verdict from the previous.

## 4-phase benchmark methodology

### Phase A — Baseline (24h, no production change)

- **Started:** 2026-06-15T09:35:41Z (13:35:41 GST)
- **Ends:** 2026-06-16T09:35:41Z (13:35:41 GST next day)
- **Capture:** `jarvis-bench-cantina` systemd timer, every 60s,
  `BENCH_PHASE=baseline`
- **Output:** `/opt/jarvis/log/bench/baseline/bdag-MS-7E16.jsonl`
- **Expected sample count:** ≈ 1,440 rows
- **What's measured:** wallet BDAG (gold), X100 hashrate stack, hans-solo
  log signals, EVM tip & peers, container fleet, host load/mem/disk/net

### Phase B — Parallel deploy (~4h elapsed, zero production impact)

Candidate stood up in a **side-by-side** namespace on the same host:

| Resource | Production (untouched) | Candidate |
|---|---|---|
| docker network | `excalibur-net` (existing) | `dagtech-pool-stack-net` (new) |
| asic-pool stratum | `0.0.0.0:3334` | `0.0.0.0:3344` |
| hans-solo SOLO | `0.0.0.0:3336` | `0.0.0.0:3346` |
| dashboard | `0.0.0.0:9280` (existing) | `0.0.0.0:9281` |
| EVM JSON-RPC | `127.0.0.1:18545` | `127.0.0.1:18555` |
| QNG RPC | `127.0.0.1:38131` | `127.0.0.1:38132` |
| cgroup lane | Lane 1 (cores 0-7) | Lane 4 (cores 20-27, shared with services) |
| chain data | `/home/bdag/blockdag-release/.../data/node1/` | `/home/bdag/dagtech-pool-stack-cantina/data/` |
| wallet | `0x6387C32c…E8083f` | same (decision #1) |
| cpu-miner | n/a | **disabled** (decision #3) |

Sentry stays pointed at production Lane 1 ports during Phase B — no overlap.
The X100 stays on production hans-solo (`192.168.1.21:3336`) — no real share
flow to the candidate.

**Health gate before Phase C (60s pre-cutover window, all must pass):**

1. `printf 'devs' | nc 192.168.1.20 4028` returns Status=Alive
2. Candidate stratum mining.subscribe probe answered <500ms
3. Candidate node tip within 1 block of production node-1 tip
4. Candidate has ≥ 5 connected libp2p peers
5. Candidate containers all healthy for ≥ 10 consecutive minutes
6. No Sentry P1 alerts in the last 10 minutes

If any gate fails, Phase B extends or rolls back — Phase C does not start.

### Phase C — Cutover (single transaction, ~5s dead-air expected)

1. `BENCH_PHASE=implementation jarvis-bench-cantina` starts (15-min ramp)
2. JARVIS PUTs X100 pool slot 0: `192.168.1.21:3336` → `192.168.1.21:3346`
3. PUT `/mcb/restart` on X100 (firmware reconnects to new pool)
4. Sentry shadow-watches both old and new for 5 min
5. **Pass criterion:** first 3 shares accepted within 90s, X100 hashrate
   recovers to ≥ 200 MH/s within 3 min
6. **Fail criterion:** any of the above misses → JARVIS auto-rolls back
   (PUT pool slot back to `3336`, PUT restart) — 15s total reversal

### Phase D — Post-bench (24h)

- **Capture:** `jarvis-bench-cantina` with `BENCH_PHASE=post`
- **Window:** 24h from successful Phase C cutover
- **Analysis:** `jarvis-bench-compare baseline post --hours 24`

## Verdict thresholds (gold-standard wallet BDAG/hour delta)

| Range | Verdict | Action |
|---|---|---|
| ≥ +5%  | 🟢 ECONOMIC UPSHIFT | **Promote** — continue rollout to node-2, then Mac, then Luke/Remz/Chad |
| +5% to −1% | 🟡 Inconclusive | **Investigate** — read metric table for the story, decide manually |
| ≤ −1% | 🔴 ECONOMIC REGRESSION | **Roll back** — single PUT to X100 pool URL (15s reversal) |

Memory bench `x100_benchmark_and_design`: current target is ~100K BDAG/hour
at ~280 MH/s. Phase A baseline first row showed `wallet 15,178,675.58 BDAG`,
X100 `MHS av 256.7 / MHS 20s 269.0` — within normal operating range.

## Risk register

**R1 — Cutover dead-air.** X100 firmware one-pool-only rule means a PUT to
`/mcb/pools` stops mining for the reconnect duration. Mitigated by the 60s
pre-cutover health gate. Expected dead-air: 2-5 seconds.

**R2 — Sentry compatibility.** Sentry's `chip_reject_spiral` detector reads
cgminer port 4028 directly, not stratum protocol. Candidate pool's stratum
format doesn't affect Sentry's heuristics. **No change needed.**

**R3 — Wallet balance granularity.** X100 produces ~100K BDAG/hour but
coinbase maturity is 10 blocks (~10 min ticks). 24h windows give ~144
balance ticks each — enough for statistical confidence on a 5% delta.
The 24h Phase A / D windows are not arbitrary.

## Rollback ladder

1. **Soft (15s):** PUT X100 pool slot back to `192.168.1.21:3336`,
   PUT `/mcb/restart`. Production hans-solo never went down during Phase B.
2. **Pool restart (45s):** `docker restart hans-solo`.
3. **Stack rebuild (5m):** `docker compose up -d` from production scripts.
4. **Chain rsync from node-3 (~30m):** last resort.

Sentry stays armed throughout. If `chip_reject_spiral` fires during the
candidate cutover, JARVIS auto-rolls back per its existing runbook.

## Metric scoreboard (captured every 60s, both phases)

| Bucket | Field path | Goal | Notes |
|---|---|---|---|
| **Economic (gold)** | `wallet_bdag` rate | ↑ | delta / window_hours |
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

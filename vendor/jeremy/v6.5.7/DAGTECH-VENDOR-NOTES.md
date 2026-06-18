# DagTech Vendor Notes — Jeremy pool-v6.5.7

**Source:** BDAG Community Pool Stack release, published 2026-06-18T08:41:16Z, distributed via IPFS pinning (no GitHub release artifacts).

**Imported by DagTech** on 2026-06-18 to `vendor/jeremy/v6.5.7/`.

## Verified provenance

| Asset | SHA256 | IPFS CID |
| --- | --- | --- |
| `pool-stack-docker-pool-v6.5.7-linux-amd64.zip` | `8d292703d77b656d85bfabf16df7b4ce4f86454a5c075a3c292a5b00f08bd852` | `bafybeibc562phfnizztpulf76p57dvhw3xl7kv6zwmhjwu37iglk4sizua` |
| `pool-stack-docker-pool-v6.5.7-linux-arm64.zip` | `53ac85c8f6337fd1d0cebbc17c3cf17804f371dcaf2270515e196c4223e50c6c` | `bafybeibobaofgsdlingiday5ea3hf62rludb4u5n6lgxaj3mgjbv35goqe` |
| Mainnet snapshot `blockdag-mainnet-20260616-010313Z.tar.zst` (9.8 GB, NOT vendored) | `7398f24a50cbe47aba1417da559c53c8a821ec145c1235c5ecfb9032e0306403` | root `bafybeie4pwyhppgcsld44i4ezj66npxmaxcpu7dcjcabincwsv5pqoram4` |

## What's new vs v6.3.20

- **Runtime snapshot bind mount** — node container now mounts `${SNAPSHOT_HOST_PATH:-./docker/no-snapshot.marker}:/snapshot/latest.bdsnap:ro`. Snapshot restore is no longer image-build-time; it's a startup step against an external file. Big improvement.
- **macOS installer wrapper** — `installers/install-macos.sh` (12 lines) delegates to `install-unix-common.sh` with `BDAG_INSTALL_OS=macos`, uses `aria2c` for snapshot download, falls back to browser snapshot. NOTE: still routes to Docker-Desktop-on-Mac running `linux-arm64` image. NOT a native Apple Silicon mining binary.
- **Pool engine knobs** — new flags addressing the stale-race / expired-job / "You're overdue" failure modes we see on hans-solo at high block-discovery velocity:
  - `POOL_SUBMIT_BLOCK_HEADER_V2_ENABLED` (default true)
  - `POOL_STALE_RACE_REJECT_WINDOW_SECONDS` (10)
  - `POOL_STALE_RACE_CLIENT_RESEND_THRESHOLD` (1)
  - `POOL_STALE_RACE_RECOVERY_COOLDOWN_SECONDS` (5)
  - `POOL_EXPIRED_JOB_CLIENT_RECONNECT_THRESHOLD` (3)
  - `POOL_EXPIRED_JOB_CLIENT_RECONNECT_WINDOW_SECONDS` (120)
  - `POOL_EXPIRED_JOB_CLIENT_RECONNECT_COOLDOWN_SECONDS` (60)
  - `POOL_ALLOW_MULTIPLE_BLOCK_CANDIDATES_PER_JOB` (true)
  - `POOL_PREEMPTIVE_BLOCK_CANDIDATE_REFRESH_ENABLED` (true)
  - `POOL_TEMPLATE_TTL_REFRESH_MS` (2000)
- **Pool RPC backends exposed** — `POOL_RPC_BACKENDS` and `POOL_SUBMIT_RPC_URLS` are first-class env vars now (matches what we hacked together in `nodex100` single-backend setup, but cleaner).
- **Collector hardening** — `BDAG_COLLECTOR_*` family of cache TTLs, status sample wait, global RPC workers (24), global block window (600), global cache TTL (60s), max tip lag (30 blocks).
- **ASIC LAN auto-detect** — new vars `BDAG_ASIC_LAN_CIDRS`, `BDAG_ASIC_LAN_INTERFACE`, `BDAG_DETECTED_NETWORK_TOPOLOGY`, `BDAG_MINER_ROUTE_*`. Useful for the installer to scan and route ASICs.
- **Resource budgets simplified** — `cpu_shares` defaults dropped (node 6144→4096, pool 5120→3072), `mem_swappiness` and `ulimits` moved out of compose (now in image or systemd). Matches our 4-lane isolation pattern.

## Credit-math question (the inverse-difficulty bug)

Symbols in `bin/mining-pool` still show `asicPool/internal/pool/pplns.(*Window).PushShare` and `.Snapshot` — same function names as the broken 6.3.20 build. New symbol: `asicPool/internal/pool/pplns.CalculateCredits` (top-level export). **Cannot confirm the fix from symbols alone** — would require an empirical test with two miners of different difficulty connecting to a freshly-deployed 6.5.7 pool.

**Safe operational stance:** keep `dagtech/credit-rebalancer/` running on top of any 6.5.7 deployment. If Jeremy fixed it, our rebalancer is a no-op per block (it skips when amounts are already correctly proportional). If he didn't, we stay protected.

## Deployment decision

Do NOT auto-cutover cantina/hans-solo to 6.5.7. The X100 is mining at ~6,000 BDAG/hour right now on the 6.3.20-based stack. Test deploy 6.5.7 to:

1. **A throwaway test pool** at a non-3334/3335/3336 port — connect Joshua/a single soft-miner — run for 30 min — confirm credit math.
2. **Hans-solo Lane 1 only** as Phase 2 — only after credit math verified — overnight A/B against the live 6.3.20 lane2/lane3.

## What 6.5.7 means for the Mac miner build

- Jeremy did NOT solve Apple Silicon native CPU+GPU mining. His `install-macos.sh` just docker-runs the existing linux-arm64 pool/node stack on a Mac. The Mac is the host, not a miner.
- The **release pattern** is reusable: pinned IPFS CIDs, SHA256-verified payloads, an OS-routing installer wrapper. We mirror this for `dagtech-mac-miner` v0.1.
- The **snapshot pattern** is gold for our Mac archive node (192.168.1.22) — pull 9.8 GB snapshot + verify + restore in ~30 min instead of days of rsync.

## Files preserved in this vendor

- `release-manifest.json` — full IPFS pinning provenance
- `HUMAN-INSTALL.md` — Jeremy's published install guide
- `AI-AGENT-RUNBOOK.md` — Jeremy's agent install playbook
- `pool-stack-docker-pool-v6.5.7-linux-amd64.zip` + `.arm64.zip` — full payloads
- Unpacked source under this directory for fast diff/grep
- `DAGTECH-VENDOR-NOTES.md` (this file)

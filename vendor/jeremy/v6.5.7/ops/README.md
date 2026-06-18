# BlockDAG Pool Ops

This folder contains local monitoring and repair tools for the BlockDAG ASIC pool stack.

## Dashboard

Run locally:

```bash
python3 ops/dashboard.py
```

Open:

```text
http://127.0.0.1:8088
```

The dashboard shows container state, latest imported block numbers, node sync state, pool errors, and recent logs.

Sync health checks are intentionally conservative. The dashboard and watchdog warn when:

- the active node has not imported a block for `BDAG_NODE_IMPORT_STALE_SECONDS`, default `180`
- recent node logs contain repeated malformed peer or P2P stream-reset errors, controlled by `BDAG_NODE_P2P_ERROR_WARN_COUNT`, default `10`

Only real catch-up problems put the dashboard into `syncing`: pool initial download, node import staleness, peer-ahead lag, or RPC refusal. Maintenance warnings such as malformed peer spam stay visible in the alert list, but they do not mark the pool as syncing when the active node is importing current blocks.

The dashboard also watches for pool share stalls. If miners are connected but the pool stops accepting valid shares for several minutes, that is treated as a recovery condition and the watchdog will restart the stack after the configured threshold.

When the node is catching up, automation leaves the pool container running. The pool's node-health gate pauses `getBlockTemplate` refreshes while the node reports template generation is not ready, so miners are not disconnected just to reduce template pressure.

The watchdog also has a fast-sync recovery path. If real syncing warnings persist for `BDAG_WATCHDOG_SYNCING_THRESHOLD` checks, default `5`, it runs a normal stack restart to force fresh peer/RPC connections and apply the current config. This restart is cooldown-limited by `BDAG_SYNCING_RESTART_COOLDOWN`, default `900` seconds, so it cannot loop continuously.

The persisted peer list in `.env` should contain only valid multiaddrs. Removing a bad peer from `.env` takes effect on the next controlled node restart; it does not interrupt currently running miners by itself.

The pool is configured to use the local node service directly as its DAG RPC endpoint on the next stack start. The dashboard compares the local chain view against external references where configured.

The Miners tab can scan the private LAN for ASIC web interfaces and configure selected miners to the current local pool endpoint. The scanner is limited to private IPv4 LAN targets, and every miner's existing pool list is backed up under:

```text
ops/runtime/miner-backups/
```

Default miner settings are derived from the running pool:

- Pool URL: `stratum+tcp://<pool-lan-ip>:3334`
- Worker/wallet: the `MINING_ADDRESS` in `.env`

The pool LAN IP and ASIC LAN scope must be explicit in `.env`:
`BDAG_POOL_HOST`, `BDAG_POOL_URL`, `BDAG_MINER_SCAN_TARGET`, and
`BDAG_ASIC_LAN_CIDRS`. Dashboard code runs inside Docker, so it must not infer a
public Stratum endpoint from its container address. Docker bridge networks
default to `172.16.0.0/12`; those IPs are filtered from ARP/DHCP hints, miner
scan targets, pool-log pseudo-miners, and displayed pool endpoints unless an
operator intentionally disables the bridge filter.
- Pool password: `1234`

Managed miners are stored in:

```text
ops/runtime/miners.json
```

The watchdog checks these miners on every loop. If a managed miner is no longer configured for the local pool or stops submitting shares, the watchdog can re-apply the pool configuration when an admin password has been saved from the dashboard. The saved password file is local-only and mode `0600`:

```text
ops/runtime/miner-admin-password.txt
```

Two checks are used for miner health:

- the ASIC web API still reports the expected pool configuration
- the pool log shows recent accepted shares or active jobs for that miner IP

## Earnings

The Earnings tab reads the postgres database for authoritative address credits, parses recent pool logs to estimate per-ASIC contribution, and records snapshots to:

```text
ops/runtime/earnings-snapshots.jsonl
```

The postgres database credits the wallet/worker address, not the ASIC IP. Per-miner earnings are therefore estimated from accepted share work in the recent pool log window. The dashboard shows estimated per-miner totals, average BDAG per hour, and a USD/ZAR bar plot when a live price is available.

The dashboard checks wallet balance from the local BlockDAG nodes and attempts best-effort cross-checks against public explorer/API endpoints. Some explorer endpoints may block server-side requests or may not expose an Etherscan-compatible API; those failures are shown in the Wallet Cross-Check table without stopping local monitoring.

For CoinMarketCap prices, set an API key in the dashboard/watchdog service environment file:

```bash
mkdir -p ops/runtime
printf 'CMC_PRO_API_KEY=your-key\n' > ops/runtime/ops.env
systemctl --user restart bdag-boot-repair.service bdag-dashboard.service bdag-watchdog.service
```

The BlockDAG CoinMarketCap ID used by default is `31162`; override it with `BDAG_CMC_ID` if CoinMarketCap changes the listing.

Action buttons are intentionally limited to known maintenance tasks:

- Start stack
- Restart stack
- Clean restore from latest snapshot
- Write a Codex handoff file
- Scan/configure LAN miners from the Miners tab

It does not provide arbitrary shell access.

## Shared Status Sampler

Routine monitoring processes should share one status collection instead of each
process independently collecting Docker logs, node RPC, pool metrics, and miner
state. Run one sample:

```bash
python3 ops/status_sampler.py --json
```

Run continuously:

```bash
python3 ops/status_sampler.py --loop
```

The sampler writes `ops/runtime/status-sampler.json` atomically. Dashboard,
watchdog, sync coordinator, P2P guard, and startup checks consume it through
`collect_status_cached()` while it is fresh. The default freshness window is
120 seconds, which keeps constrained mining hosts from adding unnecessary
status-probe I/O while the node is busy importing. Use `max_age_seconds=0` only
for explicit live diagnostics or hard repair paths that must bypass cached
state.

For safe incident testing, `ops/stack_status_source.py` can replay a recorded
status payload from `BDAG_STATUS_SOURCE_FIXTURE` or
`BDAG_STATUS_SOURCE_FIXTURE_FILE`. Use `ops/capture_status_payload.py` to save a
live `/api/status` response, then run `ops/replay_triage.py` to exercise
watchdog, sentinel, and mining-guard dry-run paths in an isolated runtime
directory.

The sampler is also the backstop for the mining imperative. If the user-systemd
guard units drift disabled, it re-enables them. If `pool` is stopped while
miner demand is visible, an ASIC LAN neighbor is present, or the chain is synced
and ready to mine, it starts the pool container without recreating dependencies.
During normal catch-up it does not stop an already-running pool; the pool remains
up and pauses template refreshes from its own node-health signal.
Set `BDAG_MINING_IMPERATIVE_REPAIR_ENABLED=0` only for an intentional maintenance
window where mining must remain stopped.

It also owns automatic chain-state self-heal. When status reports
`needs_chain_data_restore`, `chain_data_restore_required`, an irreparable sync
block, DAG tip/block damage, or repeated missing-trie state warnings, the sampler
stops `pool` and starts `bdag-chain-state-self-heal.service`. The repair script
quarantines damaged chain data, restores from a configured trusted source or
snapshot, restarts `node` and `dashboard`, and leaves `pool` stopped until normal
readiness gates make mining safe again. A stateful adjacent detector watches for
height staying frozen while peer lag grows; after the configured sustained
threshold it follows the same flow instead of leaving the dashboard stuck in a
misleading syncing state.

Run the chain-state self-heal manually only for an approved data repair:

```bash
BDAG_CHAIN_STATE_RESTORE_SOURCE=/path/to/known-good/mainnet \
  ops/chain-state-self-heal.sh --force
```

For remote restores, use key-based SSH through
`BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND`; do not save passwords in `.env`,
documentation, memory, or source.

## Thirty-Minute Mining Guard

`ops/mining_guard_30min.py` is the periodic Codex-facing proof-of-health check
for the mining path. Installed stacks run it through
`bdag-mining-30min-guard.timer` every 30 minutes with low CPU and idle IO
priority.

The guard records each sample in:

```text
ops/runtime/mining-30min-guard-state.json
ops/runtime/mining-30min-guard-history.jsonl
ops/runtime/logs/mining-30min-guard.log
```

When the sample is not healthy, the guard writes an incident with the current
mining symptoms and fetches source metadata from the configured stack
repositories. This satisfies the first repair step: check current `develop`
before deciding whether the fault is already fixed upstream. The background
guard is intentionally fetch-only. It does not edit source, build images,
commit, push, or restart the local stack; an active Codex repair session must
own those steps with tests, deployment evidence, and rollback context.

Configure source triage with:

```text
BDAG_MINING_GUARD_SOURCE_BRANCH=develop
BDAG_MINING_GUARD_SOURCE_REPOS=/path/to/pool-stack-docker:/path/to/pool:/path/to/dashboard
```

## Paid Conversion Evidence

Accepted shares prove that miners are connected and doing work; they do not
prove the pool is earning. Release promotion must use accepted block submits and
confirmed chain-paid evidence.

Capture a read-only paid-conversion baseline:

```bash
python3 ops/paid_conversion_baseline.py --duration 3600 --write-report
```

Evaluate evidence before promotion:

```bash
python3 ops/paid_conversion_release_gate.py ops/runtime/reports/paid-conversion-baseline-YYYYMMDD-HHMMSS.json --write-report
```

The gate fails by default if the window is too short, has no active miner-hours,
has an unready selected backend, has high local candidate drops, lacks accepted
submits, has dirty source repos, or lacks confirmed paid-chain block evidence.
Use override flags only for explicitly labelled research or early observe-only
runs, never for release promotion.

The miner-normalized A/B harness also treats less than 3600 seconds or less than
1 active miner-hour as ineligible for comparison by default. Shorter runs are
allowed for debugging, but they are not release evidence.

## Watchdog

Run one check:

```bash
python3 ops/watchdog.py --once
```

Run continuously:

```bash
python3 ops/watchdog.py --loop
```

Repair modes:

```bash
python3 ops/watchdog.py --repair start
python3 ops/watchdog.py --repair restart
python3 ops/watchdog.py --repair clean
```

The watchdog performs a staged repair:

1. Start missing containers.
2. Restart if the node wrapper is up but the `bdag` child process is gone.
3. Clean restore only after repeated hard failures, such as critical database startup errors.

Clean restore stops the stack, moves existing active chain data to a timestamped backup, restores the newest snapshot from `data-restore/`, and starts the stack.

Boot-time recovery is handled by `bdag-boot-repair.service`, which waits for Docker, checks the dirty-shutdown marker, and preserves existing chain data by default. A dirty marker now triggers a conservative start/restart path; automatic clean restore is disabled unless `BDAG_ENABLE_AUTOMATIC_CLEAN_RESTORE=1` is set explicitly.

## P2P Guard

The P2P guard is a passive network-health sampler. It does not restart nodes, pools, or miners. It records whether the active RPC path is healthy enough for mining and whether the local network path is still low-latency.

Run one sample:

```bash
python3 ops/p2p_guard.py --once
```

Create a comparison marker:

```bash
python3 ops/p2p_guard.py --once --mark "before network change"
```

Compare after a window:

```bash
latest=$(ls -1t ops/runtime/p2p-guard-marker-*.json | head -1)
python3 ops/p2p_guard.py --compare-marker "$latest" --window-seconds 3600
```

## Runtime Files

Runtime logs and status files are written to:

```text
ops/runtime/
```

Important files:

- `ops/runtime/watchdog-state.json`
- `ops/runtime/latest-action.json`
- `ops/runtime/codex-handoff.md`
- `ops/runtime/p2p-health-state.json`
- `ops/runtime/p2p-health-history.jsonl`
- `ops/runtime/logs/watchdog.log`
- `ops/runtime/logs/p2p-guard.log`

## User Systemd

The installed setup uses user-level systemd services, so no root-owned service files are required.

Installed unit files:

```text
~/.config/systemd/user/bdag-boot-repair.service
~/.config/systemd/user/bdag-dashboard.service
~/.config/systemd/user/bdag-stack-sentinel.service
~/.config/systemd/user/bdag-stack-sentinel.timer
~/.config/systemd/user/bdag-p2p-guard.service
~/.config/systemd/user/bdag-watchdog.service
~/.config/systemd/user/bdag-sync-coordinator.timer
~/.config/systemd/user/bdag-chain-restore-guard.timer
~/.config/systemd/user/bdag-chain-presync.timer
~/.config/systemd/user/bdag-hourly-snapshot.timer
~/.config/systemd/user/bdag-local-peers.timer
```

Service templates are in:

```text
ops/systemd/user-bdag-boot-repair.service
ops/systemd/user-bdag-dashboard.service
ops/systemd/user-bdag-watchdog.service
```

Install or update them with the generated, path-correct units:

```bash
./ops/install-dashboard.sh
```

Enable lingering so user services can start at boot without an active login:

```bash
loginctl enable-linger jeremy
```

Check status:

```bash
systemctl --user status bdag-boot-repair.service bdag-dashboard.service bdag-watchdog.service bdag-stack-sentinel.timer
```

View logs:

```bash
journalctl --user -u bdag-boot-repair.service -u bdag-dashboard.service -u bdag-watchdog.service -u bdag-stack-sentinel.service -f
```

The watchdog writes `ops/runtime/dirty-shutdown.marker` while it is running and clears it on a clean stop. If the host loses power, the marker remains; the boot-repair unit preserves current node data and starts the stack. Do not enable automatic clean restore unless the current snapshots are known safe and replacing live chain data is explicitly intended.

## Remote Access

The dashboard binds to `127.0.0.1` by default. For remote viewing, use SSH forwarding:

```bash
ssh -L 8088:127.0.0.1:8088 jeremy@POOL_HOST
```

Then open `http://127.0.0.1:8088` on your local computer.

Avoid exposing the dashboard directly to the public internet.

## Portable Installs

The dashboard is now configurable through `ops/runtime/ops.env`, so it can be copied to another pool host or run as multiple named instances on one management machine.

Create a clean bundle that excludes runtime logs, passwords, chain data, database data, snapshots, and `.env`:

```bash
./ops/package-dashboard.sh
```

Install the dashboard/watchdog from any copied repository:

```bash
./ops/install-dashboard.sh
```

For multiple pools on one host, use separate names, ports, and runtime directories:

```bash
./ops/install-dashboard.sh --name pool-a --port 8088 --runtime-dir /var/lib/bdag-pool-a
./ops/install-dashboard.sh --name pool-b --port 8089 --runtime-dir /var/lib/bdag-pool-b
```

## Codex Memory

Codex context is stored in a local SQLite database with compressed payloads and provenance:

```text
~/.codex/memories/context-store/context.sqlite
```

The memory service tails `~/.codex/history.jsonl` and also ingests markdown notes from:

- `~/.codex/memories`
- `ops/runtime`

It also writes session handoff snapshots to:

```text
~/.codex/memories/snapshots/
```

The handoff generator also writes a short restart checklist to:

```text
ops/runtime/codex-restart-checklist.md
```

Read that checklist first on a fresh restart. It is regenerated together with the main handoff file and ingested by the memory service because `ops/runtime` is part of the watched paths.

Install and start it:

```bash
./ops/install-codex-memory.sh
```

Search it:

```bash
python3 ops/codex_memory.py search "pool restart"
python3 ops/codex_memory.py session <session-id>
```

The service keeps the raw payload compressed, stores provenance for every entry, and indexes summaries for fast lookup.

Edit the generated env file for that pool's wallet, LAN pool address, miner scan target, and container names. See:

```text
ops/PORTABLE.md
ops/portable.env.example
```

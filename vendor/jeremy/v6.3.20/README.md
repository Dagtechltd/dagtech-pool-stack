# pool-stack-docker-stack

This stack can be run in any environment where docker is installed. It includes an upgradable BDAG node, a mining pool with its database, a read-only status API, and the Python compose dashboard UI.


| Service | Image / build | Purpose |
| --- | --- | --- |
| `node` | BlockDAG node, supervised by nodeworker | Consensus, P2P, and RPC |
| `pool` | Mining pool (Stratum :3334) | ASIC Stratum and block submission |
| `postgres` | Postgres | Pool persistence, schema auto-loaded |
| `collector` | Python collector | Read-only status API and normalized logs |
| `dashboard` | Python compose dashboard | Browser UI and local stack status/actions |


## Release package

GitHub Releases attach pinned bootstrap scripts (`install.sh` for Linux/macOS
and `install.ps1` for Windows) plus one runtime payload zip per Linux container
architecture:

- `pool-stack-docker-<tag>-linux-amd64.zip`
- `pool-stack-docker-<tag>-linux-arm64.zip`

The bootstrap script is generated for one release tag. It detects the host OS
and CPU architecture, selects `linux-amd64` or `linux-arm64`, and downloads only
the matching payload zip from that same tag. Linux ARM64, macOS ARM64, and
Windows ARM64 hosts use the `linux-arm64` runtime payload.

Each payload zip contains `bin/` (pre-built `blockdag-node`, `nodeworker`,
`mining-pool`, `dashboard-api`, and `dashboard`), `docker-compose.yml`, `dockerfile`,
`.env.example`,
`docker/`, `collector/` from `BlockdagEngineering/collector`, and the
cross-platform payload installers. **Node, pool, and dashboard release images** stage
binaries from `./bin`; the `collector` image stages the packaged collector
source from `./collector`.

Run the bootstrap script from the GitHub release, or manually unpack the
matching payload zip and run the payload installer from the extracted directory:

```bash
# Linux / macOS
bash install.sh
```

```powershell
# Windows
.\install.ps1
```

The payload installer makes two independent choices in two steps:

**Step 1 — what to install:**

1. **Mining pool stack with dashboard** (default) — the full stack: node, pool,
   Postgres, collector, and dashboard.
2. **Standalone node only** — just the node, no pool/dashboard/ASIC services.

**Step 2 — chain data type (applies to either deployment):**

1. **Non-archive** (default) — pruned chain data, bootstrapped from the standard
   snapshot.
2. **Archive** — node started with `--archival` (consensus keeps full block
   history instead of pruning), bootstrapped from the archive snapshot.

Set `BDAG_DEPLOY_KIND=pool|node` and/or `BDAG_CHAIN_MODE=archive|non-archive` to
preselect either step for non-interactive installs (the legacy
`BDAG_INSTALL_MODE=pool|archive-node|node` is still accepted and seeds both).
Standalone-node installs can also use the dedicated entry script, which fixes
step 1 to a node and lets the installer ask step 2:

```bash
# Linux / macOS: install just a node (installer prompts archive vs non-archive)
bash install-node.sh
bash install-node.sh --archive      # archive node, no prompt
bash install-node.sh --no-archive   # pruned node, no prompt
```

The chain-data choice determines the snapshot link written to `BDAG_SNAPSHOT_URL`
in `.env`. By convention the snapshot host serves `latest.bdsnap`
(non-archive/pruned) and `latest-archive.bdsnap` (archive/full history); the
host can be overridden with `BDAG_SNAPSHOT_BASE_URL` and the full link with
`BDAG_SNAPSHOT_URL`. The node container also reads `BDAG_SNAPSHOT_URL` at
first start and downloads/imports the snapshot itself when no local snapshot
or chain data exists. Choosing archive additionally sets `BDAG_NODE_ARCHIVAL=1`
in `.env`, which makes the node entrypoint append the `--archival` flag.

The payload installer writes `.env` and `node.conf`, generates a strong Postgres
password unless `POSTGRES_PASSWORD` is already set, provisions a signed IPFS
segment-writer identity, attempts a trusted signed IPFS raw-datadir restore for
an empty datadir when a raw artifact/index is configured, verifies configured
IPFS segment evidence, sets `DOCKER_PLATFORM` from the downloaded payload's
`release-payload.env`, and runs
`docker compose build && docker compose up -d --no-build --pull never --no-deps postgres node dashboard`.

Fresh installs assume zero miner sources. Initial install and chain sync must
work with no ASICs or Stratum miners configured; operators can opt in to the
miner wizard after sync and may configure 0..N miner sources. The RC must not
treat this host's five X100 devices as a release default.

The installer uses host-path chain storage at `BDAG_NODE_DATA_DIR` and preserves
existing chain data. If the configured node datadir has no chain markers, the
installer first tries `ops/restore-rawdatadir-segment-artifact.py` when
`BDAG_IPFS_RAWDATADIR_RESTORE_ARTIFACT_CID`,
`BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_CID`, or
`BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_FILE` is configured, or when
`BDAG_IPFS_RAWDATADIR_RESTORE_DISCOVERY_FILE` names a raw-datadir checkpoint
index. That path reconstructs a raw `mainnet` datadir only after the artifact
manifest signature verifies
against `BDAG_RAWDATADIR_TRUSTED_SIGNERS`, the manifest root matches, and every
chunk/file hash matches. The installer then runs the chain-order IPFS restore
drill in verification-only mode and records the result under
`ops/runtime/ipfs-content/restore-drill-status.json`. Chain-order segment data
is not promoted into the live datadir until a segment-to-node replay/importer
and scratch-node validation path exists. To replace existing chain data, stop
the stack and move the configured datadir aside deliberately before running the
installer.

On macOS, if Docker reports an `xattr` error for files such as `._.env.example`, those are AppleDouble metadata files from the extracted folder or external drive. Current release packages include `.dockerignore` and the installer removes those files before building. For an older extracted folder, clean it manually and run the installer again:

```bash
find . -name '._*' -type f -delete
find . -name '.DS_Store' -type f -delete
rm -rf __MACOSX
bash install.sh
```

The same cleanup also ignores common Windows metadata such as `Thumbs.db`, `desktop.ini`, `$RECYCLE.BIN`, and `System Volume Information`.

## Configuration (what loads where)

Docker Compose reads `**.env`** in this directory for variable substitution and passes pool / miner settings into containers.


| Piece           | Purpose                                                                                                                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `**node.conf`** | **Project root.** Mounted into the `**node`** container as `/etc/bdagStack/node.conf` (peers, `miningaddr`, RPC modules). **Copy from `node.conf.example**` — `node.conf` is gitignored. `**rpcuser` / `rpcpass` here must match `NODE_RPC_USER` / `NODE_RPC_PASS` in `.env`.** |
| `**.env`**      | Start from `**.env.example`**. `******NODE_RPC_URL` / `**PG_URL**` are set in `docker-compose.yml`. **Miner:** `MINER_POOL_URL`, `MINING_POOL_ADDRESS`, `MINER_POOL_PASS`, `MINER_WORKERS`.                                                                                     |

`MINING_POOL_ADDRESS` is required for pool and miner deployments. The stack
must fail configuration/rendering rather than mine to
`0x0000000000000000000000000000000000000000`.


The `**pool`** image bakes `**.env.example`** into the image at `/var/lib/bdagStack/pool/.env` for `godotenv` (release `**dockerfile`** uses `**COPY .env.example**` from repo root; git dev `**dockerfile-dev**` copies it from the named `**stack_src**` context). Compose still sets most variables via `environment:`.

## Mining resource priority

The compose file sets work-conserving Docker CPU and IO weights so mining-path
services win contention without reserving or wasting idle CPU:

| Service | CPU shares | Block IO weight | OOM score | Reason |
| --- | ---: | ---: | ---: | --- |
| `node` | `6144` | `1000` | `-900` | Block templates, validation, and P2P propagation are consensus-critical. |
| `pool` | `5120` | `950` | `-800` | ASIC submits must reach the selected node with the lowest possible tail latency. |
| `postgres` | `4096` | `950` | `-800` | Accounting writes matter, but source code keeps them off the solved-block submit path. |
| `dashboard` | `128` | `100` | `300` | Operator visibility must not compete with paid block production. |

Do not replace these weights with hard CPU quotas or realtime priority unless a
profile proves normal cgroup weighting is insufficient. The goal is maximum paid
blocks per miner-hour, not maximum dashboard refresh rate or synthetic CPU use.

## P2P Peer Configuration

Configure complete P2P multiaddrs with peer IDs in `.env` or `node.conf`.
`BOOTSTRAP_PEER_ADDRESSES` and `node.conf` `addpeer` lines are ordinary startup
peers; address class is not a sync mode, priority class, or eligibility signal.

During upgrades, `ops/update-local-peers.py` imports any existing
address-bucket values only long enough to normalize complete P2P multiaddrs
into `BDAG_NODE_PEER_ADDRESSES`, then clears those bucket values. Do not add new
LAN, VPN, or public sync options.

Upgrades that keep existing chain data should also mine that data for peer
evidence. After the node starts, the release installer runs
`ops/update-local-peers.py --force-apply`, parses preserved chain peerstore
startup logs, probes candidate multiaddrs for TCP reachability, writes
`ops/runtime/peer-discovery-current.json`, and applies the resulting
`BDAG_NODE_PEER_ADDRESSES` to the active single node. TCP-open status is only a
bootstrap hint; install completion and mining readiness still require normal
peer handshakes, sync freshness, RPC health, and template checks.

## IPFS Sync Source

IPFS is the only supported chain-data bootstrap, archive, and recovery source.
The stack keeps a conservative, low-priority raw-datadir sidecar copy near the
live node, seals safe generations into content-addressed chunks, and publishes
verified indexes and live-tail segments through IPFS/IPNS. Segment manifests
and indexes are signed with a local Ed25519 writer key provisioned by
`ops/ipfs_segment_identity.py`; the same key file signs raw-datadir checkpoint
artifacts via `BDAG_RAWDATADIR_SIGNING_KEY_FILE`. Receivers must trust the
writer public key before using any archive object. Empty-datadir installs can
restore a signed raw-datadir artifact from IPFS when a trusted raw content
artifact/index is configured. Chain-order segments remain the continuous
append-only mirror and verification source; destructive replay from those
segments is still blocked until an importer and scratch-node validation path
exists.

## IPFS Content Discovery

Future systems should read `ops/ipfs-content-discovery.json` for the durable
IPFS/IPNS discovery contract. The stable segment latest pointer is
`/ipns/k51qzi5uqu5djjlh4vxtmzyswx0qk4s3wdlf3yrpkszp38gq5sl71zcgmmc3jk`; the current
immutable chain-order segment index CID is recorded in `current_latest_index_cid`.
Raw checkpoint restore uses separate discovery keys such as
`current_rawdatadir_index_cid` and a separate local
`ops/runtime/ipfs-content/rawdatadir-content-index.json` file. The current
implementation writes append-only live-tail chain-order segments from the local node.
The durable protocol design is recorded in
`docs/ipfs-append-only-segment-protocol.html`. IPFS and IPNS are
not chain trust. Receivers must verify Ed25519 signatures against a trusted
writer roster, recursive previous-index lineage, raw artifact manifests, segment
CIDs, payload hashes, order continuity, network/genesis identity, tip/state
roots, finality, and normal consensus before using the data.

## Runtime Stability Defaults

No-miner deployments are sync-only by default: `BDAG_ENABLE_NODE_MINING=0`,
`BDAG_NODE_MODULES=Blockdag`, and an empty `BDAG_NODE_MINING_ARGS`. Enable node
mining/template flags only when real miners are attached. Do not add unsynced
mining bypass flags; readiness gates must fail closed until node sync and P2P
freshness are healthy. The dashboard,
watchdog, stack sentinel, P2P guard, peer refresh, chain restore guard, and
IPFS sidecar timers are installed by `ops/install-dashboard.sh` unless explicitly
disabled. Runtime tooling uses the current stack service names: `node`, `pool`,
and `postgres`. Concrete Compose container names may include project and ordinal
suffixes.

Catch-up has priority over mining when a production node is I/O-bound while it
is behind peers or while the selected backend is not mineable/submit-ready.
`BDAG_CATCHUP_IO_PRESSURE_PAUSE_ENABLED=1` makes this the primary mitigation
using `iowait`, `io_some`, and `io_full` pressure signals; a production node
more than `BDAG_CATCHUP_PAUSE_THRESHOLD_BLOCKS=300` blocks behind peers is the
backup trigger when pressure signals are missing or delayed.
The status sampler stops the pool, disables node mining/template runtime churn,
raises the node cache toward `BDAG_CATCHUP_NODE_CACHE_MB` within the host memory
budget, and recreates only the node service when that runtime change is needed.
The dashboard reports this as a deliberate catch-up pause, not a pool failure,
and tells operators to leave miners configured until I/O pressure drops, peer lag
is back inside the safe window, and template health is ready.

Collector block height is sourced from chain RPC `getBlockCount`; template
height, logs, and main-order values are shown only as
diagnostics. Build and release flows should run through
`scripts/bdag-low-io-build.sh`, which uses idle I/O priority, low CPU priority,
and `BDAG_BUILD_TMPDIR` so image builds do not compete with chain sync or block
submission. Chain RPC checks retry slow storage-bound samples via
`BDAG_NODE_CHAIN_RPC_TIMEOUT` and `BDAG_NODE_CHAIN_RPC_RETRIES`, and the status
payload exposes RPC latency and Linux IO pressure metrics. When PSI is unavailable, the collector falls back to `/proc/stat`
`iowait` deltas and raises a maintenance warning after sustained high IO wait.
The ops layer also detects a host profile with `BDAG_HOST_PROFILE=auto` and
uses adaptive worker budgets for expensive collector/global/miner scans. The
same release source is expected to behave conservatively on constrained ARM64
hosts, while AMD64 and larger ARM64 hosts can use more parallelism when pressure
is low. See `docs/platform-adaptive-runtime.md`.

The collector, sync coordinator, P2P guard, and startup checks also share one
cross-process status sample. `ops/status_sampler.py` writes
`ops/runtime/status-sampler.json` atomically, and routine callers read it
through `collect_status_cached()` when it is fresh. The default sampler reuse
window is bounded at 120 seconds so constrained hosts do not repeatedly probe
Docker, node RPC, pool metrics, and miner state while the node is catching up.
Diagnostics can still force a live collection with `max_age_seconds=0`.
Repair actors should acquire stack status through `ops/stack_status_source.py`.
That module prefers the collector status API, then falls back to the shared
status sampler/direct collection path, so watchdogs and sentinels do not each
recreate their own monitoring fallback order.

For offline triage testing, `ops/stack_status_source.py` also accepts a fixture
payload via `BDAG_STATUS_SOURCE_FIXTURE` or `BDAG_STATUS_SOURCE_FIXTURE_FILE`.
Capture a live payload with `ops/capture_status_payload.py`, then replay it
through the guards with `ops/replay_triage.py`. Watchdog, sentinel, and the
30-minute mining guard all support dry-run execution so they can classify
incidents without mutating the stack.

If a node stops importing while peers continue advancing, the dashboard must not
describe the state as ordinary catch-up. Node logs that contain `Irreparable
error`, `Not DAG block`, DAG tip/block damage, or repeated `missing trie node`
warnings are chain-data restore triggers. The status sampler fails mining closed,
starts the one-shot `${INSTANCE}-chain-state-self-heal.service`, and the script
`ops/chain-state-self-heal.sh` quarantines the damaged node datadir, restores
from `BDAG_CHAIN_STATE_RESTORE_SOURCE` or the signed IPFS rawdatadir restore settings,
restarts `node` and `dashboard` with `--no-build --pull never`, and leaves
`pool` stopped until readiness gates pass. A softer adjacent detector records
sustained stuck height while peer lag grows; by default it requires 900 seconds,
at least 1000 blocks of peer lead, and 60 blocks of gap growth before it triggers
the same fail-closed self-heal flow. Remote restore sources should use key-based
SSH via `BDAG_CHAIN_STATE_RESTORE_SSH_COMMAND`; do not put passwords in source or
checked-in env files.

The Pi5 release builder marks generated runtime compose files with
`BDAG_GENERATED_PI5_RUNTIME_COMPOSE=1` and rejects `build:`/`dockerfile:`
entries in runtime packages. Runtime starts use `--no-build --pull never` by
default; set an explicit pull/build flag only when intentionally refreshing
images. Keep `scripts/validate-pi5-restart-hardening.sh` in the release gate
before cutting an RC, and use `--mode live-runtime` for an installed stack where
`ops/runtime` and Python bytecode are expected service artifacts.

Constrained mining appliances also run a read-only install preflight before
chain seeding or stack start. `scripts/mining-appliance-preflight.py` checks the
host profile, root and chain-data free space, filesystem and mount options,
storage profile split, duplicate node data, swap sizing, Docker root
placement, network route, schema presence, and resource-sensitive `.env`
defaults. The installer resolves `BDAG_STORAGE_PROFILE=auto` into concrete
chain, Postgres, and runtime paths so capacity USB storage can carry the growing
chain while internal or other non-USB storage absorbs small frequent writes when
it has enough headroom. USB-backed chain data always prefers this split. Small
ephemeral scratch is kept on bounded tmpfs through `BDAG_EPHEMERAL_DIR`,
`BDAG_CONTAINER_TMPFS_SIZE`, and node-specific `BDAG_NODE_TMPFS_SIZE`; service
containers also mount `/var/tmp` as tmpfs and export `TMPDIR`, `TMP`, and
`TEMP` to avoid accidental temp spillover into overlay layers. Large
IPFS/rawdatadir restore staging stays on capacity storage unless
deliberately overridden. The installer reports
warnings and continues by default. Set `BDAG_APPLIANCE_PREFLIGHT_STRICT=1` to
make hard failures stop the install, or `BDAG_APPLIANCE_PREFLIGHT=0` to skip it
explicitly. The field report behind these checks is in
`docs/t430-appliance-hardening.md`.

Mining hosts install `bdag-mining-host-tuning.service` and timer through
`ops/install-p2p-services.sh`; fresh release installs run that support-service
installer after the stack starts. The release installer also applies
`scripts/install-mining-appliance-profile.sh` in non-destructive mode by
default, which installs sysctl/tmpfiles/Docker log defaults and a recurring
runtime-priority timer without masking common background services unless
`BDAG_INSTALL_APPLIANCE_PROFILE_DISABLE_SERVICES=1` is set. The tuning script
discovers the active Compose containers, raises node/pool/Postgres CPU and
block I/O weights, applies process `nice`/`ionice`, writes cgroup v2
`memory.low` protection, and keeps selected host interfaces on `fq_codel` when
`tc` is available. Docker does not provide a portable per-container network
priority control in this release path; network protection is host qdisc tuning
plus keeping mining-critical process, CPU, and disk I/O scheduling ahead of
dashboard and maintenance work. The policy is safe to reapply and uses the
`BDAG_*_CPU_SHARES`, `BDAG_*_MEMORY_LOW`, and `BDAG_TUNE_NET_QDISC` knobs from
`.env`.

The release builder also runs `scripts/verify-release-architecture.py` before
image assembly so ARM64 packages cannot silently receive AMD64 binaries; the
checker reads ELF/Mach-O/PE headers directly so it can be used from Linux,
macOS, and Windows build hosts.

The dashboard UI is normally exposed on host port `8088`. It talks to the
collector status API, which is bound to localhost on host port `9280` by default. Global
production data must be sourced from native BlockDAG chain RPC
`getBlockCount`/ordered block/coinbase calls. EVM RPC belongs to wallet balance
views only. The packaged web dashboard on `DASHBOARD_HOST_PORT` is the operator
UI; the collector on `9280` is the read-only JSON API behind it.

When testing directly from a source checkout, start the status API with
environment that matches the actual container names for the stack it is
watching. On Linux, that process needs Docker API access for container status
and logs; use a system service account with Docker socket access or an explicit
`DOCKER_HOST`.

Source checkout tests require Python's standard library test runner,
`cryptography` for signed IPFS archive metadata, `pytest`, and the Kubo
`ipfs` CLI for live IPFS restore/publish drills. On
Ubuntu/Debian hosts, install the test dependencies with:

```bash
sudo apt-get update
sudo apt-get install -y python3-cryptography python3-pytest
```

Agents should verify it with `python3 -m pytest --version` before running
`ops/tests` through pytest-backed deployment checks. Verify the signing
dependency with `python3 -c 'import cryptography'`. Verify Kubo with
`ipfs version` before running live IPFS restore, pin, or publish tests.

The collector runtime uses Python's standard HTTP client for local
pool metrics and public enrichment calls. Do not make live status depend on
host utilities such as `curl`; release packages should behave the same on Linux
AMD64, Linux ARM64, macOS Docker Desktop, and Windows Docker Desktop once Docker
and Python are available.

For live collector-only updates, use:

```bash
ops/deploy-live-runtime-update.sh --target /path/to/installed/runtime --mark-runtime-compose
```

The deploy helper copies only a small whitelist, backs up changed files, refuses
dev compose files, validates source and target, restarts only the configured
services, and rolls back copied files if validation or restart fails.
It also checks that every live-runtime file required by the RC hardening
validator is present in the copy contract before touching the installed stack.

For source and release-candidate performance slices, collect comparable baseline
evidence with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B ops/optimization_measurement.py --duration-seconds 300 --interval-seconds 15 --label baseline
```

Add `--status-url http://127.0.0.1:9280/api/status` when measuring collector
HTTP latency as part of the same run. The harness writes JSONL samples and an
HTML summary under `ops/runtime/measurements`.

## Quick start

```bash
# 1. Run the pinned bootstrap from the GitHub release, or unzip the matching
#    pool-stack-docker-<tag>-linux-<arch>.zip payload.

# 2. Run the installer
bash install.sh

# 3. Logs
docker compose logs -f node
docker compose logs -f pool
```

To include optional services controlled by `.env`, set `COMPOSE_PROFILES` before
`docker compose up`. Example: `COMPOSE_PROFILES=miner` enables the CPU miner
service; leave `COMPOSE_PROFILES` empty to disable it.

Once everything is running:

- Dashboard: `http://localhost:8088`
- Collector API: `http://localhost:9280`
- Mining pool Stratum endpoint: `stratum+tcp://localhost:3334`
- RPC endpoint: `http://localhost:38131`

For ASIC deployments, the installer records the host-facing pool address and
ASIC LAN scope in `.env` as `BDAG_POOL_HOST`, `BDAG_POOL_URL`,
`BDAG_MINER_SCAN_TARGET`, and `BDAG_ASIC_LAN_CIDRS`. The dashboard and repair
tools use those values instead of guessing from inside Docker. Docker bridge
networks default to `172.16.0.0/12` and are filtered from ASIC discovery and
displayed Stratum endpoints; seeing `172.*` as a miner IP or pool endpoint is a
configuration failure, not a valid physical miner.

## Default IPFS Sync Source

New installs can use trusted IPFS raw checkpoint content as the destructive
bootstrap path when a raw checkpoint artifact, index, or discovery source is
configured. Append-only chain-order segment indexes are the continuous signed
verification mirror; they are not replayed into the node datadir until a
segment importer and scratch-node validation path exists. Active mining hosts
maintain a low-priority raw datadir sidecar, seal signed restore artifacts, and
publish signed, verified sidecar content only after the safety and finalization
gates pass. Configure
`BDAG_IPFS_RAWDATADIR_RESTORE_INDEX_CID` or
`BDAG_IPFS_RAWDATADIR_RESTORE_ARTIFACT_CID` for unattended fresh-node bootstrap
from a trusted IPFS raw checkpoint.

Plan bounded chain-order backfill without RPC/IPFS mutation with:

```bash
./ops/ipfs_segment_backfill.py --plan --stop-order <safe-finalized-order> --json
```

The current segment format treats order `0` as genesis identity for validation;
candidate backfill payloads start at order `1` and remain candidate-only until a
separate full-coverage verification and promotion path exists.

Every successful chain-order restore drill records the highest accepted segment
index in `BDAG_IPFS_RESTORE_ACCEPTED_HEAD_STATE_FILE`. Later drills reject older
indexes, and reject non-lineage indexes when both the previous and current index
CIDs are known. This protects against IPNS rollback while keeping chain-order
segments verify-only; it does not import segment data into the node datadir.

The same restore drill can chain-anchor verified IPFS data with
`BDAG_IPFS_RESTORE_CHAIN_ANCHOR_ENABLED=1`. When
`BDAG_IPFS_RESTORE_CHAIN_SOURCE_RPC_URL` and
`BDAG_IPFS_RESTORE_CHAIN_REFERENCE_RPC_URL` are configured, the drill compares
the signed segment index against live source and independent reference chain RPC
block hashes. Set `BDAG_IPFS_RESTORE_REQUIRE_CHAIN_ANCHOR=1` for fail-closed
promotion drills. CIDs, signatures, hashes, lineage, accepted-head state, and
live-chain block-hash anchors must all agree before treating IPFS as canonical
chain evidence.

The archive seed timer is not part of this stack because IPFS segments and
finalized raw-datadir sidecars own source publication.

Btrfs checkpoint storage is a production install requirement when
`BDAG_IPFS_STATE_CHECKPOINT_REQUIRED=1`. On ordinary Ubuntu/Lubuntu installs the
release installer creates a loop-backed btrfs volume, mounted by default at
`./data-restore/btrfs-checkpoints`, with a 128 GiB minimum size and a root free
space reserve. Raw sidecar copies, open restore points, sealed raw artifacts,
and IPFS raw content publication all live under that mount. This keeps recovery
state on snapshot-capable storage while allowing the active node database to
stay on its best available chain-data filesystem. Existing native btrfs, ZFS,
or LVM checkpoint storage can be used by pointing the checkpoint and sidecar
paths at that volume, but ext4-only sidecar paths are a deployment blocker for
trusted raw checkpoint recovery. See
`docs/btrfs-checkpoint-volume.html` for the install-time contract and trust
boundary.

Check sidecar safety and status with:

```bash
./ops/rawdatadir_sidecar_safety.py --full --json
```

Refresh the local raw datadir sidecar with:

```bash
./ops/maintain-rawdatadir-sidecar.sh
```

See `docs/ipfs-append-only-segment-protocol.html`.

IPFS segments and finalized raw-datadir sidecars are the supported
content-publication paths. Published files must be manifest-indexed and
consensus-validated before use.

## Release readiness

Container health alone does not prove that a deployment can mine. Before
marking an install healthy, run:

```bash
./scripts/release-readiness-check.py
./scripts/validate-rc-local.sh
```

These checks do not touch live services. The local RC validator copies the
tracked and unignored source tree to a temporary directory, runs tests with a
temporary runtime directory, and leaves any live `ops/runtime` state in the
checkout alone. It verifies the pool schema, source-health gates, no-miner
service semantics, dashboard source-of-truth rules, and packaged self-healing
files. See
`docs/release-readiness-gates.html`. Active multi-miner deployments, including
five-X100 hosts, must also preserve the template-conversion release guard in
`docs/five-asic-template-conversion-guard.html`: accepted block conversion per
miner-hour is the success metric for active multi-miner deployments, and
tip-overdue, duplicate-local, invalidated-job, and non-current-job losses must
not be hidden by connected miner count alone. The guard is conditional on the
configured or observed miner source count; five miners are not an install-time
default. Background maintenance must preserve bounded CPU/I/O policy.

Issue #26 final-release mitigations are captured in
`docs/final-release-issue-26-checklist.md`; keep that checklist current when
changing pinned source repos, installer reset behavior, sync defaults, or
release packaging.
  

# Common operations

## Show the resolved compose config

docker compose config

## Stop everything (keeps volumes)

docker compose down

## Stop + delete named volumes (DESTRUCTIVE)

docker compose down -v

```

```

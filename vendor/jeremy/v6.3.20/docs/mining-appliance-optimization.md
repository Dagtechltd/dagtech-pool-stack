# Mining Appliance Optimization

This repo ships defaults for dedicated BlockDAG mining hosts. They are intended
for the default one-node deployment; every node service should use the same node
resource profile and the same host profile.

## Docker Defaults

The Compose files use:

- Docker `local` logs capped at `10m` x `2` files.
- High CPU and block IO weights for node, pool, and Postgres.
- Lower CPU and block IO weights for dashboard/control-plane services.
- Large `nofile` limits for node and pool sockets.
- Graceful stop windows for node and database shutdown.
- Node cache, BD cache, DAG cache, reduced log verbosity, and no file logging
  via `NODE_ARGS_APPEND`.

For the production node, apply:

```yaml
logging: *mining-logging
cpu_shares: 4096
blkio_config:
  weight: 1000
oom_score_adj: -900
ulimits:
  nofile:
    soft: 1048576
    hard: 1048576
environment:
  NODE_ARGS_APPEND: >-
    --cache=${BDAG_NODE_CACHE_MB:-4096}
    --bdcachesize=${BDAG_NODE_BD_CACHE_SIZE:-8192}
    --dagcachesize=${BDAG_NODE_DAG_CACHE_SIZE:-8192}
    --debuglevel=${BDAG_NODE_DEBUG_LEVEL:-error}
    --evmtrietimeout=${BDAG_EVM_TRIE_TIMEOUT_SECONDS:-7200}
    --nofilelogging
```

Do not add `--allowminingwhennearlysynced`, `--miner`, `--miningaddr`, or
`modules=miner` on no-miner hosts. When a node is behind tip, catch-up is the
first priority. The runtime priority service therefore boosts node import above
all other stack work while the dashboard reports `syncing`; when no miners are
tracked, it also idles pool, database, and RPC-routing containers so the host is
effectively sync-only until caught up.

## Host Profile

Install once on a dedicated mining host before starting the stack:

```bash
sudo scripts/install-mining-appliance-profile.sh
```

This installs:

- `/etc/sysctl.d/90-mining-appliance.conf`
- `/etc/systemd/journald.conf.d/90-mining-appliance.conf`
- `/usr/local/sbin/mining-appliance-host-tuning`
- `/usr/local/sbin/bdag-runtime-priority`
- `/usr/local/sbin/bdag-node-child-guard`
- `/etc/systemd/system/mining-appliance-tuning.service`
- `/etc/systemd/system/bdag-runtime-priority.service`
- `/etc/systemd/system/bdag-runtime-priority.timer`
- `/etc/systemd/system/bdag-node-child-guard.service`
- `/etc/systemd/system/bdag-node-child-guard.timer`
- `/etc/docker/daemon.json` defaults for `live-restore` and local logs

It also tunes P2P/RPC socket buffers, raises the mining block-device queue,
keeps the CPU governor in performance mode, and re-applies runtime nice/ionice
priorities every minute. Node, pool, Postgres, RPC routing, Docker/containerd,
Wi-Fi, and ZeroTier are favored. Dashboard, browser, Codex, and other desktop
helpers are lowered so live blockchain and pool work wins CPU, memory pressure,
disk IO, and network scheduling.

The node child guard checks every minute that a nodeworker container still has a
real `bdag` child process and an open RPC or WebSocket listener. If the wrapper
is alive but the node child has crashed, the guard restarts only the node
container; it does not start mining services on no-miner hosts.

## USB Chain Data

For Pi-class hosts where SD-card random write latency is the sync bottleneck,
keep the OS on the SD card and place the active stack data on a faster USB
filesystem. The runtime profile detects USB block devices and applies the
mining storage queue profile at boot: `mq-deadline`, 2048 KiB read-ahead, 256
queue requests where the device allows it, `max_sectors_kb=1024`, non-rotational
media classification, and no entropy collection from block IO.

Use a stable mount such as `/mnt/bdag-usb`, mount ext4 or F2FS with
`noatime,lazytime`, and make Docker require that mount before container
startup. For any install where the active chain data is on USB,
`BDAG_STORAGE_PROFILE=auto` now treats split IO as the preferred pattern. The
default policy keeps the large, growing node datadirs on the USB capacity disk,
then moves frequent small writes to internal or other non-USB storage when it
has at least 4 GiB free:

```bash
BDAG_CHAIN_DATA_DIR=/mnt/bdag-usb/blockdag-chain
BDAG_NODE_DATA_DIR=/mnt/bdag-usb/blockdag-chain/node
BDAG_POSTGRES_DATA_DIR=/opt/blockdag-pool/runtime-data/postgres
BDAG_RUNTIME_DIR=/opt/blockdag-pool/runtime-data/ops-runtime
```

Leave old parked chain snapshots on the SD card unless the USB has enough spare
space. This keeps the USB focused on the hot node chain
while the OS disk absorbs Postgres WAL, dashboard history, guard state, and
small log churn. If the internal disk is too small, the installer falls back to
a single-device USB profile and the preflight reports that all hot writes share
one device.

USB-backed chain hosts must not serve bulk chain-data traffic from the active
chain path by default. The node still syncs and relays found blocks; IPFS
archive work stays on the low-priority raw-datadir sidecar so it cannot compete
aggressively with mining.

Small scratch files that are safe to lose should not use the chain disk either.
The release defaults create `/run/bdag-pool` through tmpfiles and set:

```bash
BDAG_EPHEMERAL_TMPFS_ENABLED=1
BDAG_EPHEMERAL_DIR=/run/bdag-pool
BDAG_HOST_TMPDIR=/run/bdag-pool/tmp
BDAG_CONTAINER_TMPFS_SIZE=128m
```

Compose services that generate small temporary files get a bounded `/tmp`
tmpfs. Do not put large chain snapshots or import artifacts on this RAM-backed
path unless the host has been sized for it.

The installer disables common non-mining timers and services such as apt daily
jobs, cron, Avahi, CUPS, NFS/rpcbind, and desktop disk/power helpers. It leaves
Bluetooth available so local keyboards and mice can be paired without undoing
the mining appliance profile.

For a desktop that should only run the dashboard and Codex, run as the desktop
user:

```bash
scripts/install-mining-user-session-profile.sh
```

This masks audio, keyring, GVFS, and desktop portal services that are not needed
for a dashboard/Codex appliance session.

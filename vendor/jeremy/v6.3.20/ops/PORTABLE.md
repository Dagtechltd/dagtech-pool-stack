# Portable Dashboard Install

The dashboard can run on any Linux host with Python 3 and access to the target
pool's Docker daemon. Install one copy per pool host, or run multiple instances
on one management machine by using different service names, ports, and runtime
directories.

## Create a Portable Bundle

On the current pool host:

```bash
./ops/package-dashboard.sh
```

The bundle excludes runtime logs, miner admin passwords, chain data, database
data, snapshots, and `.env`.

## Install on Another Pool Host

Copy the bundle or repository to the new host, then run:

```bash
cd /path/to/blockdag-asic-pool
./ops/install-dashboard.sh
```

This creates:

```text
~/.config/systemd/user/bdag-dashboard.service
~/.config/systemd/user/bdag-watchdog.service
ops/runtime/ops.env
```

Edit `ops/runtime/ops.env` for that pool. The most important values are:

```bash
BDAG_POOL_ENV_FILE=/path/to/blockdag-asic-pool/.env
BDAG_MINING_ADDRESS=0xYourWalletAddress
BDAG_POOL_HOST=192.168.1.10
BDAG_POOL_URL=stratum+tcp://192.168.1.10:3334
BDAG_MINER_SCAN_TARGET=192.168.1.0/24
```

If that pool uses different container names, update:

```bash
BDAG_POOL_CONTAINER=pool
BDAG_POOL_CONTAINERS=pool
BDAG_POOL_DB_CONTAINER=postgres
BDAG_NODE_SERVICE=node
BDAG_STACK_SERVICES=postgres,node,pool,dashboard
```

Restart after edits:

```bash
systemctl --user restart bdag-dashboard.service bdag-watchdog.service
```

## Multiple Pools from One Machine

Install each pool as its own instance:

```bash
./ops/install-dashboard.sh --name pool-a --port 8088 --runtime-dir /var/lib/bdag-pool-a
./ops/install-dashboard.sh --name pool-b --port 8089 --runtime-dir /var/lib/bdag-pool-b
```

For a remote Docker daemon over SSH, put this in that instance's env file:

```bash
DOCKER_HOST=ssh://pool-admin@192.168.1.10
```

The management machine still needs network access to the ASIC web interfaces if
you want browser-driven miner scanning/configuration from that dashboard.

## Access

The default install binds to `127.0.0.1`. View it with an SSH tunnel:

```bash
ssh -L 8088:127.0.0.1:8088 user@POOL_HOST
```

Then open:

```text
http://127.0.0.1:8088
```

Avoid exposing the dashboard directly to the public internet.

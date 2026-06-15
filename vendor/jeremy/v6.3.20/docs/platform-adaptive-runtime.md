# Platform Adaptive Runtime

The release stack is expected to run on Linux AMD64 and ARM64 hosts first, with
installer support for macOS and Windows Docker hosts. Runtime optimization must
therefore be adaptive instead of Pi-only.

`ops/pool_ops.py` detects a lightweight host profile from OS, CPU architecture,
CPU count, memory, and hardware model:

- `pi5`: Linux ARM64 Raspberry Pi 5 class hosts.
- `constrained`: small ARM64 or AMD64 hosts with low CPU or memory.
- `standard`: mid-size desktops, laptops, mini PCs, and VMs.
- `large`: higher-core, higher-memory servers and workstations.

The profile is advisory. Operators can override it with:

```sh
BDAG_HOST_PROFILE=auto
```

Supported override values are `pi5`, `constrained`, `standard`, and `large`.
`auto` is the default.

Adaptive concurrency is enabled by default:

```sh
BDAG_ADAPTIVE_CONCURRENCY_ENABLED=1
```

Routine control-plane loops also share one sampled status file by default:

```sh
BDAG_STATUS_SAMPLER_ENABLED=1
BDAG_STATUS_SAMPLER_INTERVAL_SECONDS=10
BDAG_STATUS_SAMPLER_MAX_AGE_SECONDS=120
BDAG_STATUS_PAYLOAD_STALE_AFTER_SECONDS=120
```

`ops/status_sampler.py` writes `ops/runtime/status-sampler.json` atomically.
Dashboard, watchdog, sync coordinator, P2P guard, and startup checks use that
file through `collect_status_cached()` while it is fresh, instead of each
process independently collecting Docker logs, node RPC, pool metrics, and miner
state. The 120-second maximum is a bounded I/O protection window for
constrained hosts; explicit repair diagnostics can still bypass the sampler and
short cache with `max_age_seconds=0`.

The existing worker settings remain hard caps:

```sh
BDAG_GLOBAL_RPC_WORKERS=24
BDAG_MINER_SCAN_WORKERS=64
BDAG_MINER_HASHRATE_PROBE_WORKERS=8
```

The adaptive layer chooses lower worker counts when the detected host class is
small or when pressure signals show the host is waiting on I/O, CPU, or slow
chain RPC. On Linux it uses `/proc/pressure/*`, `/proc/stat` iowait, chain RPC
latency from node status, and the sustained iowait state already exposed in the
dashboard. On macOS and Windows Docker hosts those Linux pressure files are not
assumed to exist; the profile still detects OS/arch/CPU, and pressure-specific
shrinking simply degrades to the available signals.

This preserves the Pi5 behavior that protects USB-backed chain import, while
letting AMD64 or larger ARM64 hosts use more concurrency when the machine is
idle enough to benefit.

Pool block submit configuration now stays single-endpoint by default. The pool
uses one direct node RPC URL, avoiding endpoint races while normal share
validation and no-miner sync-only mode stay low-overhead.

Systemd timers are also staggered. Short-interval guards and priority loops use
small `RandomizedDelaySec` values so they remain responsive but do not all wake
on the same second after boot or after a shared interval boundary. Longer
snapshot, chain pre-sync, local-peer, and incident-report timers
use larger jitter because freshness can safely lag behind chain import and live
mining.

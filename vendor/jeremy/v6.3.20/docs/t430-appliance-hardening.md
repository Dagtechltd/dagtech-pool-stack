# T430 Appliance Hardening

Field report from the `/home/hpool` mining host on 2026-05-26.

## Observed Environment

- OS: Ubuntu 26.04 LTS, Linux 7.0.0.
- CPU: Intel Celeron N4020, 2 cores.
- RAM: about 3 GiB.
- Swap: 512 MiB.
- Boot disk: 29 GiB internal eMMC, about 4.7 GiB free during recovery.
- Chain disk: 128 GB USB flash, F2FS, mounted with `noatime,lazytime`.
- Network: default route over Wi-Fi on `wlp2s0`, pool host `192.168.49.193`.
- ASIC: single X100 at `192.168.49.179`.
- Snapshot source: low-latency trusted peer on `192.168.49.186`.

## Adverse Conditions

1. The internal eMMC was too small for chain data, Docker churn, old snapshots,
   package archives, and rollback backups at the same time.
2. Normal peer sync from an old local chain state was too slow for a practical
   recovery window.
3. A partial pre-V2 datadir could not be trusted as the final single source of
   node data after the V2 artifact import.
4. The postgres database initially lacked the `block_submissions` history table, so
   accepted blocks could be logged without being available to dashboard and
   earnings views.
5. Dashboard top-level health briefly reported `syncing` while nested sync state
   and pool logs showed accepted blocks. Runtime truth had to come from node RPC,
   pool logs, and database rows.
6. The host has only two cores and about 3 GiB RAM. Expensive dashboard scans,
   duplicate chain data and large caches can directly compete
   with mining.
7. Disk-backed swap and repeated ownership walks over chain data can turn memory
   pressure into disk write latency.
8. Wi-Fi was the active network path. It worked on this LAN, but it should be
   treated as a latency risk if shares or block submissions stall.
9. A later status check found the node container wrapper running while the
   actual `blockdag-node` child had exited, leaving RPC refused and paid block
   submission stopped until a controlled node restart.

## Mitigations Used

1. Moved chain data off the internal eMMC and onto a dedicated F2FS USB
   filesystem, then split small frequent writes back to internal storage where
   there was enough free space.
2. Used the default one-node stack for the appliance.
3. Downloaded and verified the legacy restore bundle before import.
4. Parked old datadirs with timestamped names instead of deleting them, then
   imported the verified legacy restore bundle into a clean datadir.
5. Applied `sql/pool-schema.sql` so `block_submissions` and credit idempotency
   indexes existed before relying on dashboard earnings.
6. Verified health through node RPC, pool accepted-share and accepted-block logs,
   and Postgres counts rather than one dashboard aggregate field.
7. Kept the node cache small, peer count bounded, shared status sampling
   enabled, and adaptive concurrency enabled.
8. Kept disk-backed swap small and avoided repeated chown scans on node volumes.
9. Restarted the node container when the wrapper/child-process split was
   detected, then let the node catch up before trusting pool mining readiness.
10. Treated any running node more than 1000 blocks behind as a catch-up
    condition rather than normal background drift. The release default applies
    leader CPU/IO catch-up weights and allows a cooldown-bound restart if the
    importer is stale.

## Release Hardening Added

The RC now includes `scripts/mining-appliance-preflight.py`, which is a read-only
install preflight for constrained mining appliances. The package installer runs
it after `.env` generation and before chain seeding or stack start.

The preflight checks:

- OS, CPU count, RAM, kernel, and constrained-host profile.
- project/root filesystem free space.
- chain data filesystem free space, mount point, filesystem type, and mount
  options.
- trusted IPFS raw checkpoint storage, including the default loop-backed btrfs
  checkpoint volume, 128 GiB minimum volume size, and sidecar/artifact/open
  restore paths resolving to btrfs, ZFS, or LVM-backed storage.
- whether chain data is separated from the project/root filesystem.
- whether the selected storage profile keeps USB chain writes separated from
  Postgres, dashboard/runtime state, and Docker churn.
- whether small ephemeral scratch resolves to RAM-backed storage rather than
  adding disk writes to the USB chain device.
- USB chain filesystem suitability.
- duplicate node datadirs.
- old parked chain backups that should be cleaned only after stable mining.
- node mode, cache, peer count, status sampler, adaptive concurrency, and chown
  policy.
- swap sizing on constrained hosts.
- default route and Wi-Fi latency risk.
- Docker root placement and free space.
- live node wrapper versus `blockdag-node` child-process consistency.
- automatic sync acceleration once a running node is more than 1000
  blocks behind.
- pool schema presence for block submissions and credit idempotency.
- reward wallet presence before mining is enabled.

By default the installer reports warnings and continues so portable installs are
not blocked by advisory checks. Set `BDAG_APPLIANCE_PREFLIGHT_STRICT=1` to make
preflight failures stop the install, or `BDAG_APPLIANCE_PREFLIGHT=0` to skip the
preflight explicitly.

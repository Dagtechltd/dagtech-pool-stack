# Final Release Checklist From Issue 26

Issue #26 identified the RC4 deployment gaps found on `/home/matt`. The final
release candidate must preserve the operational hardening from that deployment
while removing local assumptions that caused install or sync drift.

## Source Manifest

- `pool-stack-docker`: `release/pool-stack-20260524-rc4-sre`
- `blockdag-corechain`: raw-datadir V2 sync source commit `c74f88b9c1b4fbf4213e15272d3bf1f63943e839`
  or newer, including directory V2 artifact support, latency-first artifact
  peer preference, and the zero-state-root `HasState` guard.
- `pool`: `develop` at `61b231c0501b32338f4ad47561a09e03e5933adc` or newer,
  pinned to a single backend submit path.
- `dashboard2`: `main`; release builds always use this branch.

## Release Requirements

- GitHub release workflows pin known source commits, use Go 1.26, and set
  `GOFLAGS=-buildvcs=false`; pool binaries also pass `-buildvcs=false`
  explicitly. Normal pool releases build both `linux-amd64` and `linux-arm64`
  runtime payload zips and generate pinned bootstrap scripts for the same tag.
- Release archives are audited by `scripts/check-release-archive.py` so `.git`,
  package metadata, mutable data directories, local `.env`, `node.conf`, and
  transient chain restore working files do not ship.
- Payload installers preserve existing node data, peer identity, signer
  material, and runtime state. IPFS segment restore is the only supported
  accelerated chain-sync path for new installs once chain data is absent. They
  set `DOCKER_PLATFORM` from `release-payload.env`, not from a universal AMD64
  assumption.
- Installers preflight architecture, Docker Compose, disk, port occupancy, time
  sync, optional `jq`, and seed reachability. Old/orphan Compose cleanup is a
  dry-run unless `BDAG_CLEAN_ORPHAN_CONTAINERS=1` is set.
- Installs configure one direct submit endpoint and do not enable endpoint
  fanout by default.
- IPFS segment sync is the default restore path. When a node falls materially
  behind, the sync coordinator protects mining and allows only the shared sync
  control plane to decide whether the node should keep catching up from peers or
  recover from verified IPFS chain segments.
- Peer selection is latency/usefulness-first over libp2p. Address class is not a
  sync option or priority signal; complete P2P multiaddrs are the only sync
  candidates.
- Directory restore sources must publish a signed IPFS segment manifest;
  otherwise startup reports the restore source as unavailable instead of
  silently pretending a local archive mode is active.
- Scripts that still need `jq` preflight it explicitly. Release installers avoid
  `jq` for required JSON parsing.
- Live data scans must avoid mutable Postgres/node paths; release packaging uses
  tracked source plus explicit runtime/data exclusions.

Run `scripts/validate-release-build.sh .` before tagging the release candidate.

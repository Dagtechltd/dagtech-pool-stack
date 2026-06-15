# DagTech Pool Stack

DagTech-flavored BlockDAG mainnet node + mining pool, based on
[BlockdagEngineering's jeremy-dev-release v6.3.20](k51qzi5uqu5di0diurqi5rquevlgj4fv4ykm67tgovktd8vtnlimcvqywk1jhg.ipns.dweb.link)
with proven DagTech production overlays.

```
chain: mainnet 1404  |  default stratum: 3334  |  default API: 9280
```

## What this is

A curated fork of Jeremy's open community release, repackaged for operators in the
DagTech network (Excalibur RAK node, CT/USW WG relays, hans-solo SOLO pool,
Sentry inline operator, JARVIS triage). The vendored upstream is unchanged;
DagTech additions live in `dagtech/`.

## Layout

```
.
├── vendor/jeremy/v6.3.20/   unmodified upstream (binaries IPFS-pinned, see bin/README.md)
├── dagtech/                 DagTech overlay (Sentry, JARVIS, hans-solo, Excalibur, relays)
├── docs/                    architecture, install, dissection map
├── installers/              merged install-{linux,windows,macos} with DagTech defaults
├── version.txt              upstream + dagtech version pinning
├── LICENSE-NOTICE.md        attribution + license terms
└── README.md                this file
```

## Quick start

```bash
# Pick your install profile and run:
curl -fL https://miners.dagtech.network/install.sh | bash
```

The installer detects your arch, fetches the matching upstream payload from IPFS,
verifies SHA256, applies the DagTech overlay (peer list, hans-solo, dashboards,
Sentry/JARVIS hooks), brings up the stack via docker compose, and verifies first
stratum + first synced block.

## Dissection roadmap (work in progress)

| Component | Status | DagTech change |
|---|---|---|
| docker-compose.yml | studied | adopt as base; add `hans-solo` + `sentry-lane1` services |
| .env.example (370 keys) | studied | adopt as base; add DAGTECH_* keys for relay/Excalibur |
| ops/codex_memory.py | studied | adopt pattern → fold into JARVIS engine |
| ops/ipfs_segment_*.py | studied | adopt as-is (solves our chain snapshot gap) |
| ops/dashboard.py | studied | keep ours (luke/remz/chad dashboard on :9280) |
| ops/incident_journal.py | studied | merge with JARVIS actions.jsonl |
| installers/install-windows.ps1 | studied | merge with our setup-chad-windows.bat |
| installers/install-macos.sh | not started | reference for Mac archive node |
| installers/install-linux.sh | studied | adopt; layer DagTech .env defaults on top |
| peers list | partial | adopt Jeremy's seeds + add our CT/USW relays |
| Sentry inline operator | not in upstream | DagTech original; ports forward |
| JARVIS triage | not in upstream | DagTech original; ports forward |
| hans-solo SOLO pool | not in upstream | DagTech original; ports forward |
| Excalibur v6 topology | not in upstream | DagTech original; ports forward |

## Provenance

- **Upstream zip CID**: `bafybeiga7llbde6jh2pzfdp2pvg3woqqxzy4aetmdijigaop67tu5ycffi`
- **Upstream zip SHA256**: `11363f5e91cea12a541ad39ee8df5fd02b7e479d60e7c1203b1d5c9803b4b553`
- **Upstream IPNS**: `k51qzi5uqu5di0diurqi5rquevlgj4fv4ykm67tgovktd8vtnlimcvqywk1jhg`
- **Vendored on**: 2026-06-15 by JARVIS

## Maintainers

- Dr Dawie Nel — `dawie.s.nel@dagtech.network`
- DagTech SRE / JARVIS Autonomous Operations

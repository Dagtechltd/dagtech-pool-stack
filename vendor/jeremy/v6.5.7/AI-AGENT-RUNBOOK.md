# AI Agent Runbook: BlockDAG Community Pool Stack pool-v6.5.7

Use the IPFS-pinned local payload CIDs for this release. Do not assume GitHub assets exist for `pool-v6.5.7`.

Release metadata is in `release-manifest.json` on the IPFS setup page.

## Rules

- Never print, store, commit, or echo private keys.
- First run `uname -s` and `uname -m`.
- Continue only on Linux.
- Use `linux-amd64` for `x86_64/amd64` and `linux-arm64` for `aarch64/arm64`.
- Prefer the local helper when digest verification is required before extraction.
- Ask directly for missing pool inputs; do not rely on a popup/modal tool being available.

## Verified Install

```bash
printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n' | sudo tee /etc/sysctl.d/99-bdag-disable-ipv6.conf >/dev/null
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 ca-certificates curl unzip zstd tar python3 iproute2 arp-scan nmap coreutils
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
docker --version
docker compose version
docker info
command -v curl unzip sha256sum zstd python3
mkdir -p ~/bdag-pool-v6.5.7 && cd ~/bdag-pool-v6.5.7
rm -f install-v6.5.7.sh
curl -4 -fsSL "https://ipfs.io/ipfs/bafkreidctpmfmjpdtcatg7lktqw774elpeuylamyjfpb2nqpb55y4vlfzy" -o install-v6.5.7.sh ||
  curl -4 -fsSL "https://bafkreidctpmfmjpdtcatg7lktqw774elpeuylamyjfpb2nqpb55y4vlfzy.ipfs.inbrowser.link/" -o install-v6.5.7.sh ||
  curl -4 -fsSL "https://bafkreidctpmfmjpdtcatg7lktqw774elpeuylamyjfpb2nqpb55y4vlfzy.ipfs.dweb.link/" -o install-v6.5.7.sh
test -s install-v6.5.7.sh
chmod +x install-v6.5.7.sh
BDAG_CHAIN_MODE=non-archive ./install-v6.5.7.sh
```

## Restore-First Install

Use a host with at least 120 GB of disk. This reads the pinned IPFS snapshot manifest resolved from IPNS at publish time, downloads the archive, verifies SHA256 and size, extracts it into `data/node/mainnet`, and starts the installer:

```bash
BDAG_RESTORE_SNAPSHOT=1 BDAG_DEPLOY_KIND=pool BDAG_CHAIN_MODE=non-archive ./install-v6.5.7.sh
```

Snapshot source:

- IPNS: `k51qzi5uqu5dhgizyhubligbj23gvkuoyx1zeddw3b8xdjt1lryg6jodj35v5p`
- Immutable root: `bafybeie4pwyhppgcsld44i4ezj66npxmaxcpu7dcjcabincwsv5pqoram4`
- Archive: `blockdag-mainnet-20260616-010313Z.tar.zst`
- Blocks: `11,474,570`
- SHA256: `7398f24a50cbe47aba1417da559c53c8a821ec145c1235c5ecfb9032e0306403`

Node-only restore:

```bash
BDAG_RESTORE_SNAPSHOT=1 BDAG_DEPLOY_KIND=node BDAG_CHAIN_MODE=non-archive ./install-v6.5.7.sh
```

Pool mode: collect `MINING_POOL_ADDRESS`, optional private key entry preference, `BDAG_POOL_HOST`, and `BDAG_MINER_SCAN_TARGET`. Prefer a hidden prompt for the private key.

```bash
export MINING_POOL_ADDRESS='0xREPLACE_WITH_USER_PUBLIC_ADDRESS'
export BDAG_POOL_HOST='192.168.1.REPLACE'
export BDAG_MINER_SCAN_TARGET='192.168.1.0/24'
BDAG_DEPLOY_KIND=pool BDAG_CHAIN_MODE=non-archive bash ./install.sh
```

Validate with collector status and dashboard reachability. If `sync_progress.status` is not `synced`, keep pool mining paused until gates pass.

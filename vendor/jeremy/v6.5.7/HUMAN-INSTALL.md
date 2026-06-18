# BlockDAG Community Pool Stack pool-v6.5.7 Human Install Guide

This release publishes two Linux payloads pinned to immutable IPFS CIDs from the local `stack-v6.5.7` bundle:

- `linux-amd64` for `x86_64` or `amd64` hosts.
- `linux-arm64` for `aarch64` or `arm64` hosts.

Release metadata is in `release-manifest.json` on the IPFS setup page.

Run this first:

```bash
uname -s
uname -m
```

Only Linux is supported by this release bootstrap.

## Prerequisites

On a clean Ubuntu host, install Docker and the required command-line tools first:

```bash
printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n' | sudo tee /etc/sysctl.d/99-bdag-disable-ipv6.conf >/dev/null
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 ca-certificates curl unzip zstd tar python3 iproute2 arp-scan nmap coreutils
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in, then verify:

```bash
docker --version
docker compose version
docker info
command -v curl unzip sha256sum zstd python3
```

## Required Inputs

For node-only mode, no wallet or private key is required.

For pool mode, prepare these before running the payload installer:

- `MINING_POOL_ADDRESS`: your public payout/mining wallet address.
- Pool operator private key: enter through a hidden prompt or secure local environment only.
- Pool host LAN IP for ASICs, for example `192.168.1.50`.
- ASIC scan CIDR, for example `192.168.1.0/24`.

## Pinned Payloads

The helper below selects the matching pinned payload CID, verifies the published SHA256 digest, extracts it, and runs the payload installer.

## Verified Local Helper

This repository also includes a helper that verifies the published SHA256 digest before extraction:

```bash
mkdir -p ~/bdag-pool-v6.5.7 && cd ~/bdag-pool-v6.5.7
rm -f install-v6.5.7.sh
curl -4 -fsSL "https://ipfs.io/ipfs/bafkreidctpmfmjpdtcatg7lktqw774elpeuylamyjfpb2nqpb55y4vlfzy" -o install-v6.5.7.sh ||
  curl -4 -fsSL "https://bafkreidctpmfmjpdtcatg7lktqw774elpeuylamyjfpb2nqpb55y4vlfzy.ipfs.inbrowser.link/" -o install-v6.5.7.sh ||
  curl -4 -fsSL "https://bafkreidctpmfmjpdtcatg7lktqw774elpeuylamyjfpb2nqpb55y4vlfzy.ipfs.dweb.link/" -o install-v6.5.7.sh
test -s install-v6.5.7.sh
chmod +x install-v6.5.7.sh
BDAG_CHAIN_MODE=non-archive ./install-v6.5.7.sh
```

To download, verify, and extract without starting the packaged installer:

```bash
BDAG_SKIP_PAYLOAD_INSTALL=1 ./install-v6.5.7.sh
```

Then enter the extracted payload directory:

```bash
cd pool-stack-docker-pool-v6.5.7-linux-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
```

## Manual Verification

AMD64:

```bash
curl -4 -fL https://ipfs.io/ipfs/bafybeibc562phfnizztpulf76p57dvhw3xl7kv6zwmhjwu37iglk4sizua -o pool-stack-docker-pool-v6.5.7-linux-amd64.zip
echo "8d292703d77b656d85bfabf16df7b4ce4f86454a5c075a3c292a5b00f08bd852  pool-stack-docker-pool-v6.5.7-linux-amd64.zip" | sha256sum -c -
```

ARM64:

```bash
curl -4 -fL https://ipfs.io/ipfs/bafybeibobaofgsdlingiday5ea3hf62rludb4u5n6lgxaj3mgjbv35goqe -o pool-stack-docker-pool-v6.5.7-linux-arm64.zip
echo "53ac85c8f6337fd1d0cebbc17c3cf17804f371dcaf2270515e196c4223e50c6c  pool-stack-docker-pool-v6.5.7-linux-arm64.zip" | sha256sum -c -
```

## Restore-First Install

Use a host with at least 120 GB of disk. This command reads the pinned IPFS snapshot manifest resolved from IPNS at publish time, downloads the archive, verifies SHA256 and size, extracts it into `data/node/mainnet`, and starts the installer:

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

Start pool mode:

```bash
export MINING_POOL_ADDRESS='0xYOUR_PUBLIC_ADDRESS'
export BDAG_POOL_HOST='192.168.1.50'
export BDAG_MINER_SCAN_TARGET='192.168.1.0/24'
BDAG_DEPLOY_KIND=pool BDAG_CHAIN_MODE=non-archive bash ./install.sh
```

## Success Checklist

```bash
curl -s http://127.0.0.1:9280/api/status | python3 -m json.tool
ss -ltnp | grep -E '(:8150|:3334|:8088|:9280|:38131|:18545)'
```

Expected: dashboard reachable on `8088`, sync status eventually `synced`, remaining blocks `0`, `can_accept_shares=true`, and ASICs visible after they point to `stratum+tcp://<pool-host-lan-ip>:3334`.

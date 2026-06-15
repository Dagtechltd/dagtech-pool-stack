# Portable USB Chain Restore Guide

This guide explains how to use a USB drive as a portable BlockDAG chain-data holder. The goal is fast reinstall or recovery on another system without copying the full chain every time. The USB layout is rsync-friendly, so updates transfer only changed files.

## What This USB Holds

The USB holder stores a sanitized raw datadir sidecar:

```text
<usb-root>/
  chain-sidecar/mainnet/
    BdagChain/
    bdageth/
    ...
  manifests/rawdatadir-sidecar-safe-status.json
  README-BLOCKDAG-CHAIN-SIDECAR.txt
```

It intentionally excludes private node identity and runtime files:

```text
network.key
bdageth/nodekey
keystore/
peerstore/
nodes/
LOCK
*.ipc
*.sock
.rsync-partial/
```

That means another computer can use the USB to catch up quickly without inheriting this node's identity.

## Requirements

On the source pool host:

```bash
cd /home/jeremy/blockdag-asic-pool
systemctl --user status bdag-rawdatadir-sidecar.timer
python3 -m json.tool ops/runtime/rawdatadir-sidecar-safe-status.json
```

The safe-status file must show:

```text
"safe": true
"usable": true
"unsafe_path_count": 0
```

On both source and target systems:

```bash
rsync --version
docker compose version
```

## Update The USB From This Pool

1. Mount the USB drive and choose a stable holder directory:

```bash
USB_ROOT="/run/media/jeremy/YOUR_USB_LABEL/blockdag-portable"
```

2. Dry-run the USB update:

```bash
cd /home/jeremy/blockdag-asic-pool
./ops/usb-sidecar-sync-to-drive.sh --usb-root "$USB_ROOT" --dry-run
```

3. Execute the update:

```bash
./ops/usb-sidecar-sync-to-drive.sh --usb-root "$USB_ROOT" --execute
```

4. Run the same command again later to update only changed data:

```bash
./ops/usb-sidecar-sync-to-drive.sh --usb-root "$USB_ROOT" --execute
```

The script uses `rsync -a --delete --partial` and keeps the same directory structure, so repeat updates are differential.

## Restore Or Seed Another Computer From USB

On the target computer:

1. Install or check out the stack repo.

```bash
cd /home/jeremy/blockdag-asic-pool
```

2. Stop the node and pool before copying chain data:

```bash
docker compose stop pool node
```

3. Set the USB holder path:

```bash
USB_ROOT="/run/media/jeremy/YOUR_USB_LABEL/blockdag-portable"
```

4. Dry-run the restore:

```bash
./ops/usb-sidecar-restore-from-drive.sh --usb-root "$USB_ROOT" --target "$PWD/data/node/mainnet" --dry-run
```

5. Execute the restore:

```bash
./ops/usb-sidecar-restore-from-drive.sh --usb-root "$USB_ROOT" --target "$PWD/data/node/mainnet" --execute
```

The existing target is moved aside once, for example:

```text
data/node/mainnet.before-usb-restore-20260607T145500Z
```

6. Start the non-pool stack first:

```bash
docker compose up -d --no-deps postgres node dashboard
```

7. Watch the dashboard:

```bash
curl -s http://127.0.0.1:8088/api/status | python3 -m json.tool
```

The pool should remain stopped while the node is catching up. Mining resumes only after the catch-up/readiness gates clear.

## Updating The Target Later

If the target already has data from the same USB holder, repeat:

```bash
docker compose stop pool node
./ops/usb-sidecar-restore-from-drive.sh --usb-root "$USB_ROOT" --target "$PWD/data/node/mainnet" --execute --no-backup
docker compose up -d --no-deps postgres node dashboard
```

Because the layout stays unpacked and stable, rsync sends only changed files.

## Do Not Do These

Do not zip the chain data for routine updates. A zip file forces large recopy work.

Do not copy `network.key`, node keys, keystores, peerstore, sockets, or locks to another system.

Do not start the pool while the node is hundreds of blocks behind. The dashboard catch-up pause is expected and protects mining.

Do not restore from a USB holder whose manifest says unsafe, unusable, or has unsafe paths.

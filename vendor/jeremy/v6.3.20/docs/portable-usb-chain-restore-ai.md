# AI Agent Runbook: Portable USB Chain Restore

Use this runbook when a user asks an AI agent to update a USB chain-data holder or restore BlockDAG chain data from USB.

## Non-Negotiable Rules

Preserve live chain data unless the user explicitly asks for a restore or the chain-state restore policy requires it.

Never clone private node identity to another computer. Exclude:

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

Never use zip/tar as the normal portable update mechanism. The portable USB chain holder must remain an unpacked directory so rsync can update deltas.

Do not start the pool while catch-up pause is active. If the dashboard says `mode=catchup_pause`, node/Postgres running with pool stopped is healthy.

## Source System Checks

Run:

```bash
cd /home/jeremy/blockdag-asic-pool
systemctl --user status bdag-rawdatadir-sidecar.timer
python3 -m json.tool ops/runtime/rawdatadir-sidecar-safe-status.json
```

Accept only:

```text
safe=true
usable=true
unsafe_path_count=0
latest_safe_dir is a directory
latest_safe_dir contains BdagChain/
```

If these are not true, run or wait for:

```bash
systemctl --user start bdag-rawdatadir-sidecar.service
```

Then re-check the safe-status file. Do not proceed from an unsafe sidecar.

## Update USB Holder

Detect the USB path:

```bash
lsblk -o NAME,TRAN,SIZE,FSTYPE,MOUNTPOINTS,LABEL,UUID
findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS | grep -E '/run/media|/media'
```

Set:

```bash
USB_ROOT="/run/media/jeremy/<label>/blockdag-portable"
```

Dry-run first:

```bash
./ops/usb-sidecar-sync-to-drive.sh --usb-root "$USB_ROOT" --dry-run
```

Execute only after the dry-run target is correct:

```bash
./ops/usb-sidecar-sync-to-drive.sh --usb-root "$USB_ROOT" --execute
sync
```

If the user asked to eject the USB and sync is complete:

```bash
udisksctl unmount -b /dev/<partition>
udisksctl power-off -b /dev/<disk>
```

Use the exact block devices from `lsblk`; do not guess.

## Restore Target From USB

Verify the target repo and stack:

```bash
cd /home/jeremy/blockdag-asic-pool
git status --short --branch
docker compose ps -a
```

Stop services before copying chain data:

```bash
docker compose stop pool node
```

Dry-run:

```bash
USB_ROOT="/run/media/jeremy/<label>/blockdag-portable"
./ops/usb-sidecar-restore-from-drive.sh --usb-root "$USB_ROOT" --target "$PWD/data/node/mainnet" --dry-run
```

Execute:

```bash
./ops/usb-sidecar-restore-from-drive.sh --usb-root "$USB_ROOT" --target "$PWD/data/node/mainnet" --execute
```

Start only non-pool services first:

```bash
docker compose up -d --no-deps postgres node dashboard
```

Verify:

```bash
curl -s http://127.0.0.1:8088/api/status | python3 -m json.tool
docker compose ps -a
ss -ltnp | grep -E '(:8088|:9280|:38131|:8150)'
```

Expected during catch-up:

```text
overall=syncing
mode=catchup_pause
postgres running
node running
pool exited
Stratum port 3334 not listening
```

This is not a failure. It means mining is intentionally paused until the node is close enough to peers.

## Boot Recovery Requirements

If the host has already rebooted, verify:

```bash
cat /proc/sys/kernel/random/boot_id
systemctl --user status bdag-boot-repair.service bdag-codex-boot-handoff.service bdag-codex-auto-resume.service
cat ops/runtime/codex-auto-resume.json | python3 -m json.tool
curl -s http://127.0.0.1:8088/api/status | python3 -m json.tool
```

Do not initiate host reboots for this verification. Reboot-loop testing was a one-off appliance validation exercise and must not be propagated into source code, installers, timers, watchdogs, or agent runbooks.

`bdag-codex-auto-resume.service` should open a visible desktop terminal and run:

```bash
codex resume --cd /home/jeremy/blockdag-asic-pool --ask-for-approval never --sandbox danger-full-access --dangerously-bypass-approvals-and-sandbox <session-id>
```

It must run `ops/codex_boot_handoff.py --repair` first, which checks the pool and writes:

```text
ops/runtime/codex-boot-handoff.json
ops/runtime/codex-auto-resume.json
ops/runtime/codex-handoff.md
```

## Refuse Or Stop If

The USB source does not contain `chain-sidecar/mainnet/BdagChain`.

The safe-status manifest says unsafe, unusable, or has unsafe paths.

The source and target directories are the same.

Docker is unavailable and no noninteractive sudo fallback works.

The user asks to overwrite live data while the node is running and does not permit stopping pool/node first.

The requested operation would copy private node identity to another system.

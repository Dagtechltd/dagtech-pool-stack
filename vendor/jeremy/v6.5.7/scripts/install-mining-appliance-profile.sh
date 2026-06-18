#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E "$0" "$@"
fi

disable_services=1
reload_docker=1

for arg in "$@"; do
  case "$arg" in
    --no-disable-services) disable_services=0 ;;
    --no-docker-reload) reload_docker=0 ;;
    -h|--help)
      cat <<'USAGE'
Usage: scripts/install-mining-appliance-profile.sh [--no-disable-services] [--no-docker-reload]

Installs host-level defaults for a dedicated BlockDAG mining appliance:
CPU performance governor, low-swap/writeback sysctl values, volatile capped
journald, Docker live-restore/local logs, recurring runtime priority boosts,
and optional disabling of common non-mining background services.
USAGE
      exit 0
      ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
profile_dir="${repo_root}/host/mining-appliance"

install -D -m 0644 "${profile_dir}/90-mining-appliance-sysctl.conf" /etc/sysctl.d/90-mining-appliance.conf
install -D -m 0644 "${profile_dir}/90-mining-appliance-journald.conf" /etc/systemd/journald.conf.d/90-mining-appliance.conf
install -D -m 0644 "${profile_dir}/90-bdag-ephemeral.conf" /etc/tmpfiles.d/90-bdag-ephemeral.conf
install -D -m 0755 "${profile_dir}/mining-appliance-host-tuning" /usr/local/sbin/mining-appliance-host-tuning
install -D -m 0755 "${profile_dir}/bdag-runtime-priority" /usr/local/sbin/bdag-runtime-priority
install -D -m 0755 "${profile_dir}/bdag-node-child-guard" /usr/local/sbin/bdag-node-child-guard
install -D -m 0644 "${profile_dir}/mining-appliance-tuning.service" /etc/systemd/system/mining-appliance-tuning.service
install -D -m 0644 "${profile_dir}/bdag-runtime-priority.service" /etc/systemd/system/bdag-runtime-priority.service
install -D -m 0644 "${profile_dir}/bdag-runtime-priority.timer" /etc/systemd/system/bdag-runtime-priority.timer
install -D -m 0644 "${profile_dir}/bdag-node-child-guard.service" /etc/systemd/system/bdag-node-child-guard.service
install -D -m 0644 "${profile_dir}/bdag-node-child-guard.timer" /etc/systemd/system/bdag-node-child-guard.timer

python3 - "${profile_dir}/docker-daemon.json" /etc/docker/daemon.json <<'PY'
import json
import os
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
desired = json.loads(src.read_text())
current = {}
dst.parent.mkdir(parents=True, exist_ok=True)
if dst.exists() and dst.stat().st_size:
    try:
        current = json.loads(dst.read_text())
    except json.JSONDecodeError:
        backup = dst.with_name(dst.name + ".invalid")
        if backup.exists():
            backup = dst.with_name(dst.name + f".invalid.{os.getpid()}")
        dst.rename(backup)
        print(f"WARNING: moved invalid Docker daemon config to {backup}", file=sys.stderr)
        current = {}
current.update(desired)
tmp = dst.with_suffix(dst.suffix + ".tmp")
tmp.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
os.chmod(tmp, 0o644)
tmp.replace(dst)
PY

systemctl daemon-reload
systemd-tmpfiles --create /etc/tmpfiles.d/90-bdag-ephemeral.conf
systemctl enable --now mining-appliance-tuning.service
systemctl enable --now bdag-runtime-priority.timer
systemctl enable --now bdag-node-child-guard.timer
systemctl start bdag-runtime-priority.service || true
systemctl start bdag-node-child-guard.service || true
sysctl --system >/dev/null
systemctl restart systemd-journald

if [ "${disable_services}" -eq 1 ]; then
  systemctl disable --now \
    apt-daily.timer apt-daily-upgrade.timer man-db.timer dpkg-db-backup.timer \
    e2scrub_all.timer rpi-zram-writeback.timer fstrim.timer \
    avahi-daemon.service avahi-daemon.socket cron.service \
    nfs-blkmap.service nfs-client.target rpcbind.service rpcbind.socket \
    cups.path cups.service cups.socket serial-getty@ttyAMA10.service 2>/dev/null || true

  systemctl mask --now \
    apt-daily.service apt-daily-upgrade.service man-db.service dpkg-db-backup.service \
    e2scrub_all.service e2scrub_reap.service fstrim.service \
    avahi-daemon.service avahi-daemon.socket \
    rpcbind.service rpcbind.socket cups.path cups.service cups.socket \
    udisks2.service upower.service 2>/dev/null || true
fi

if [ "${reload_docker}" -eq 1 ] && systemctl is-active --quiet docker.service; then
  if command -v docker >/dev/null 2>&1 && [ -z "$(docker ps -q 2>/dev/null || true)" ]; then
    systemctl restart docker.service
  else
    systemctl reload docker.service 2>/dev/null || true
    echo "Docker is running containers; restart Docker during a maintenance window to apply daemon-wide log defaults."
  fi
fi

echo "Mining appliance host profile installed."

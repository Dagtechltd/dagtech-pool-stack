#!/usr/bin/env bash
set -euo pipefail

systemctl --user disable --now \
  pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket \
  wireplumber.service filter-chain.service mpris-proxy.service \
  gnome-keyring-daemon.service gnome-keyring-daemon.socket 2>/dev/null || true

systemctl --user mask --now \
  pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket \
  wireplumber.service filter-chain.service mpris-proxy.service \
  gnome-keyring-daemon.service gnome-keyring-daemon.socket \
  gvfs-daemon.service gvfs-metadata.service gvfs-udisks2-volume-monitor.service \
  gvfs-afc-volume-monitor.service gvfs-goa-volume-monitor.service \
  gvfs-gphoto2-volume-monitor.service gvfs-mtp-volume-monitor.service \
  xdg-desktop-portal.service xdg-desktop-portal-gtk.service \
  xdg-desktop-portal-wlr.service xdg-document-portal.service \
  xdg-permission-store.service 2>/dev/null || true

echo "Mining dashboard/Codex user-session profile installed."

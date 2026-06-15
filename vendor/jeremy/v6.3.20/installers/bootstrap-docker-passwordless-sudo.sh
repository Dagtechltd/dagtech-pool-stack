#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this once with sudo:" >&2
  echo "  sudo $0" >&2
  exit 1
fi

user_name="${BDAG_BOOTSTRAP_USER:-${SUDO_USER:-}}"
if [[ -z "$user_name" || "$user_name" == "root" ]]; then
  user_name="$(logname 2>/dev/null || printf '%s\n' root)"
fi
if ! id "$user_name" >/dev/null 2>&1; then
  echo "User not found: $user_name" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
if ! command -v visudo >/dev/null 2>&1; then
  apt-get install -y sudo
fi

if [[ "${BDAG_ENABLE_PASSWORDLESS_SUDO:-1}" == "1" ]]; then
  tmp_sudoers="$(mktemp)"
  cat > "$tmp_sudoers" <<EOF
$user_name ALL=(ALL) NOPASSWD:ALL
EOF
  chmod 0440 "$tmp_sudoers"
  visudo -cf "$tmp_sudoers"
  install -o root -g root -m 0440 "$tmp_sudoers" "/etc/sudoers.d/90-${user_name}-nopasswd"
  rm -f "$tmp_sudoers"
fi

apt-get install -y docker.io uidmap iptables dbus-user-session curl ca-certificates python3 screen
apt-get install -y docker-buildx || apt-get install -y docker-buildx-plugin || true
apt-get install -y docker-compose-v2 || apt-get install -y docker-compose-plugin || true

groupadd -f docker
usermod -aG docker "$user_name"
systemctl enable --now docker.service

if ! docker compose version >/dev/null 2>&1; then
  for plugin in \
    "/home/${user_name}/.docker/cli-plugins/docker-compose" \
    "/root/.docker/cli-plugins/docker-compose"; do
    if [[ -x "$plugin" ]]; then
      install -d -m 0755 /usr/libexec/docker/cli-plugins
      install -m 0755 "$plugin" /usr/libexec/docker/cli-plugins/docker-compose
      break
    fi
  done
fi

docker info >/dev/null
if ! docker compose version >/dev/null 2>&1; then
  echo "Warning: Docker is running, but Docker Compose v2 is still unavailable." >&2
  echo "Install docker-compose-v2 or docker-compose-plugin, then re-run the stack installer." >&2
fi

if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$user_name" || true
fi

echo "Installed Docker host support for $user_name."
echo "A reboot or new login is recommended so the docker group applies to every session."

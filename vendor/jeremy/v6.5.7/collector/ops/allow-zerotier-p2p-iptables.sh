#!/usr/bin/env sh
set -eu

PORTS="${BDAG_P2P_PORTS:-8151,8152}"
ZT_IF="${BDAG_ZEROTIER_IF:-}"

if [ -z "$ZT_IF" ]; then
  ZT_IF="$(ip -o link show | awk -F': ' '$2 ~ /^zt/ {print $2; exit}')"
fi

if [ -z "$ZT_IF" ]; then
  echo "No ZeroTier interface found; refusing to open BlockDAG P2P ports" >&2
  exit 1
fi

add_rule() {
  chain="$1"
  if iptables -C "$chain" -i "$ZT_IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null; then
    return 0
  fi
  iptables -I "$chain" 1 -i "$ZT_IF" -p tcp -m multiport --dports "$PORTS" -j ACCEPT
}

add_rule INPUT
if iptables -nL DOCKER-USER >/dev/null 2>&1; then
  add_rule DOCKER-USER
fi


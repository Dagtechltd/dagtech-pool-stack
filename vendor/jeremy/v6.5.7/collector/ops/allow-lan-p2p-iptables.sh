#!/usr/bin/env sh
set -eu

LAN_CIDR="${BDAG_LAN_CIDR:-192.168.1.0/24}"
PORTS="${BDAG_P2P_PORTS:-8151,8152}"

add_rule() {
  chain="$1"
  if iptables -C "$chain" -p tcp -s "$LAN_CIDR" -m multiport --dports "$PORTS" -j ACCEPT 2>/dev/null; then
    return 0
  fi
  iptables -I "$chain" 1 -p tcp -s "$LAN_CIDR" -m multiport --dports "$PORTS" -j ACCEPT
}

add_rule INPUT
if iptables -nL DOCKER-USER >/dev/null 2>&1; then
  add_rule DOCKER-USER
fi

#!/usr/bin/env sh
set -eu

PORT="${P2P_PORT:-8150}"
PROTOCOLS="${BDAG_P2P_PROTOCOLS:-tcp}"

add_rule() {
  chain="$1"
  proto="$2"
  if iptables -C "$chain" -p "$proto" --dport "$PORT" -j ACCEPT 2>/dev/null; then
    return 0
  fi
  iptables -I "$chain" 1 -p "$proto" --dport "$PORT" -j ACCEPT
}

for proto in $(printf '%s' "$PROTOCOLS" | tr ',' ' '); do
  proto="$(printf '%s' "$proto" | tr '[:upper:]' '[:lower:]')"
  case "$proto" in
    tcp|udp) ;;
    "")
      continue
      ;;
    *)
      echo "Unsupported BDAG_P2P_PROTOCOLS entry: $proto" >&2
      exit 2
      ;;
  esac
  add_rule INPUT "$proto"
  if iptables -nL DOCKER-USER >/dev/null 2>&1; then
    add_rule DOCKER-USER "$proto"
  fi
done

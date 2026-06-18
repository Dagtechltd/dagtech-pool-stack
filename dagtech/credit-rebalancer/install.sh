#!/bin/bash
# install.sh — install DagTech Credit Rebalancer on a pool host
# Run on the same machine that runs asic-pool, as root.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[1/4] Building dagtech/credit-rebalancer:0.1.0 image..."
docker build -t dagtech/credit-rebalancer:0.1.0 "$HERE"

ENV_FILE="/etc/dagtech-credit-rebalancer.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "[2/4] Installing env file at $ENV_FILE — EDIT IT before starting the service"
    cp "$HERE/env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    # Auto-detect pool network
    NET=$(docker network ls --format '{{.Name}}' | grep -E 'pool-net' | head -1 || true)
    if [ -n "$NET" ]; then
        sed -i "s|^POOL_NETWORK=.*|POOL_NETWORK=$NET|" "$ENV_FILE"
        echo "  auto-detected POOL_NETWORK=$NET"
    fi
    # Auto-detect PG_URL
    POOL_CT=$(docker ps --format '{{.Names}}' | grep -E '^(asic-pool|hans-solo|cpu-gpu-pool)$' | head -1 || true)
    if [ -n "$POOL_CT" ]; then
        URL=$(docker inspect "$POOL_CT" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^PG_URL=' | cut -d= -f2- | head -1)
        if [ -n "$URL" ]; then
            sed -i "s|^PG_URL=.*|PG_URL=$URL|" "$ENV_FILE"
            echo "  auto-detected PG_URL from $POOL_CT"
        fi
    fi
fi

echo "[3/4] Installing systemd unit..."
cp "$HERE/dagtech-credit-rebalancer.service" /etc/systemd/system/
systemctl daemon-reload

echo "[4/4] Enabling + starting service..."
systemctl enable --now dagtech-credit-rebalancer.service

sleep 3
systemctl status dagtech-credit-rebalancer.service --no-pager | head -8
echo
echo "Verify: journalctl -u dagtech-credit-rebalancer.service -f"

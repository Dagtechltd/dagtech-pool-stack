#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker Compose is not installed." >&2
  exit 1
fi

"${COMPOSE[@]}" ps
echo
echo "Collector status API:"
curl -fsS "http://127.0.0.1:${COLLECTOR_HOST_PORT:-9280}/api/status" || true
echo

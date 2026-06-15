#!/usr/bin/env bash
# Initialize / re-apply the mining-pool Postgres schema in the running
# postgres container.
#
# In docker compose the schema is auto-loaded by docker-entrypoint-initdb.d
# on first boot, so this script is mainly useful for:
#   - Re-applying the schema after upgrades (idempotent).
#   - Generating a strong POSTGRES_PASSWORD if .env still has the placeholder.
#   - Quick smoke-tests against the live DB.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
SCHEMA_FILE="${SCHEMA_FILE:-${ROOT_DIR}/sql/pool-schema.sql}"
SERVICE="${SERVICE:-postgres}"
PROJECT="${COMPOSE_PROJECT_NAME:-}"

if [[ ! -f "${SCHEMA_FILE}" ]]; then
  echo "Missing schema file: ${SCHEMA_FILE}" >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}; copy .env.cpu.example or .env.pool.example to .env first." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "${ENV_FILE}"
set +a

POSTGRES_USER="${POSTGRES_USER:-bdag_pool}"
POSTGRES_DB="${POSTGRES_DB:-bdagpool}"

if [[ -z "${POSTGRES_PASSWORD:-}" || "${POSTGRES_PASSWORD}" == "change_me_to_a_strong_secret" || "${POSTGRES_PASSWORD}" == "CHANGE_ME" ]]; then
  if ! command -v openssl >/dev/null 2>&1; then
    echo "POSTGRES_PASSWORD looks unset/placeholder and openssl is missing." >&2
    echo "Set a strong POSTGRES_PASSWORD in ${ENV_FILE} (example: openssl rand -hex 32)." >&2
    exit 1
  fi
  NEW_PW="$(openssl rand -hex 32)"
  echo "POSTGRES_PASSWORD looks like a placeholder; writing a new secret to ${ENV_FILE}."
  if grep -qE '^POSTGRES_PASSWORD=' "${ENV_FILE}"; then
    sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${NEW_PW}|" "${ENV_FILE}"
  else
    printf 'POSTGRES_PASSWORD=%s\n' "${NEW_PW}" >>"${ENV_FILE}"
  fi
  POSTGRES_PASSWORD="${NEW_PW}"
  echo "Restart the stack with: make down && make up"
fi

if ! docker compose ps --services --filter status=running 2>/dev/null | grep -qx "${SERVICE}"; then
  echo "${SERVICE} is not running. Start the stack first: make up" >&2
  exit 1
fi

echo "Applying ${SCHEMA_FILE} to ${SERVICE} (db=${POSTGRES_DB} user=${POSTGRES_USER})"
docker compose exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" \
  "${SERVICE}" psql -v ON_ERROR_STOP=1 \
  -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <"${SCHEMA_FILE}"

echo "Schema applied."

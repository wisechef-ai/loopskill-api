#!/usr/bin/env bash
# test-migrations-against-postgres.sh — one-shot local Postgres migration test runner.
#
# Spins pgvector/pgvector:pg16 in a throwaway container, runs the
# Postgres-only migration tests, tears the container down on exit.
#
# Usage:
#   bash scripts/test-migrations-against-postgres.sh
#   bash scripts/test-migrations-against-postgres.sh -k test_alembic   # filter
#
# Requires: docker, pytest in the active venv.
#
# Why this script exists
# ----------------------
# tests/migrations/test_chain_postgres.py runs alembic upgrade head against a
# real Postgres database — the actual production dialect. SQLite test fixtures
# can't exercise Postgres-only DDL (FK ALTER, JSONB casts, GIN indexes,
# information_schema reads, etc.), so without a real Postgres they were
# silently lying — which is exactly how the recipes_2005/G regression slipped
# through to production.
#
# Documented in the alembic-postgres-only-sql-discipline skill.

set -euo pipefail

PORT="${PORT:-5499}"
IMAGE="${IMAGE:-pgvector/pgvector:pg16}"
CONTAINER_NAME="recipes-api-migration-test-${RANDOM}"

cleanup() {
  echo "→ Tearing down container ${CONTAINER_NAME}..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Find a free port if 5499 is taken
while ss -ltn 2>/dev/null | grep -q ":${PORT} " || lsof -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; do
  PORT=$((PORT + 1))
done

echo "→ Spinning ${IMAGE} on port ${PORT} (container ${CONTAINER_NAME})..."
docker run -d \
  --name "${CONTAINER_NAME}" \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_USER=postgres \
  -p "${PORT}:5432" \
  "${IMAGE}" >/dev/null

# Wait for the DB to accept connections (max 60s).
#
# Postgres images go through TWO start phases during first boot:
#   1. initdb creates the cluster and starts Postgres briefly to run any
#      /docker-entrypoint-initdb.d scripts.
#   2. The init process shuts that instance down and restarts Postgres for
#      real with the final config.
#
# If we observe step 1's brief "ready" state and then proceed, the actual
# test connections race with step 2's shutdown-and-restart and fail. The fix
# is to require pg_isready to return success for THREE consecutive seconds
# AND for psql to successfully execute a SELECT 1 (round-trip through the
# real connection path the tests use).
echo -n "→ Waiting for Postgres to accept connections"
consecutive_ok=0
ready=0
for _ in $(seq 60); do
  if docker exec "${CONTAINER_NAME}" pg_isready -U postgres >/dev/null 2>&1 \
    && docker exec "${CONTAINER_NAME}" psql -U postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    consecutive_ok=$((consecutive_ok + 1))
    if [ "${consecutive_ok}" -ge 3 ]; then
      echo " — ready."
      ready=1
      break
    fi
  else
    consecutive_ok=0
  fi
  echo -n "."
  sleep 1
done

if [ "${ready}" -ne 1 ]; then
  echo
  echo "FATAL: Postgres did not become ready within 60s."
  docker logs "${CONTAINER_NAME}" | tail -30
  exit 1
fi

export WITH_POSTGRES=1
export POSTGRES_DSN="postgresql://postgres:test@127.0.0.1:${PORT}/postgres"

echo "→ Running migration tests against ${POSTGRES_DSN%/*}..."
pytest tests/migrations/test_chain_postgres.py -v --tb=short "$@"

echo "→ All migration tests passed."

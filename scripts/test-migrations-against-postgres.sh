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
# tests/migrations/ holds the Postgres-dialect gates (chain-from-baseline,
# model↔schema parity, trgm/GIN index proofs, downgrade reversibility), and
# tests/*_psycopg2.py holds deploy-safety regressions — SQLite fixtures
# can't exercise Postgres-only DDL (FK ALTER, JSONB casts, GIN indexes,
# CHECK constraints, triggers, information_schema reads, etc.), so without a
# real Postgres they were silently lying — which is exactly how the
# recipes_2005/G regression slipped through to production, and how F-4
# (2026-11-16: CI's advertised Postgres migration gate never actually ran)
# hid for months.
#
# The target is deliberately the WHOLE tests/migrations/ dir (not a single
# file) so any new gate added there is picked up automatically, plus the
# *_psycopg2 deploy-safety regressions. This mirrors the `migrations-postgres`
# job in .github/workflows/ci.yml — keep the two target sets in sync.
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
# DATABASE_URL opens the postgres_only-marked proofs (e.g. the issue282
# trgm/GIN index-scan tests use the app's own engine-selection contract,
# TEST_DATABASE_URL > DATABASE_URL, to decide whether a real Postgres is
# reachable). Without it those tests self-skip here and the local run would
# silently prove LESS than the CI migrations-postgres job — keep them equal.
export DATABASE_URL="${POSTGRES_DSN}"

echo "→ Running migration tests against ${POSTGRES_DSN%/*}..."
# Whole tests/migrations/ dir — new gates are picked up automatically
# (F-4: this line used to hardcode test_chain_postgres.py alone, so every
# gate added under tests/migrations/ after that never ran). Mirrors the
# migrations-postgres job in .github/workflows/ci.yml.
pytest tests/migrations/ \
  tests/test_catalog_invariant_trigger_psycopg2.py \
  tests/test_loopclose_3005_x_migration_psycopg2.py \
  -v --tb=short "$@"

echo "→ All migration tests passed."

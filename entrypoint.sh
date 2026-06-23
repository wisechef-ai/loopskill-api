#!/bin/sh
# LoopSkill container entrypoint — first-boot bootstrap + server start.
# Runs alembic (non-sqlite) or create_all (sqlite), seeds catalog, starts API.
set -eu

# Ensure the data directory exists and is writable by the runtime user.
# The Dockerfile chowns /data to appuser before the USER switch so a fresh
# named volume inherits the right ownership. If a pre-existing root-owned
# volume is mounted, fail LOUD with an actionable message instead of letting
# sqlite crash-loop with an opaque "unable to open database file" trace.
mkdir -p /data 2>/dev/null || true
if ! { : > /data/.write_test 2>/dev/null && rm -f /data/.write_test; }; then
    echo "[entrypoint] FATAL: /data is not writable by $(id -un) (uid $(id -u))." >&2
    echo "[entrypoint] The sqlite volume must be writable by the container user." >&2
    echo "[entrypoint] Fix: 'docker compose down -v' to reset the volume, or chown the" >&2
    echo "[entrypoint] host mount to uid 1001. See docs/SELF_HOST.md (Troubleshooting)." >&2
    exit 1
fi

echo "[entrypoint] running first-boot bootstrap..."
python scripts/bootstrap.py

echo "[entrypoint] starting API server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8200

# ── Builder stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed by some C-extension wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated venv so the runtime stage gets a clean copy.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt


# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Runtime deps for psycopg2-binary and curl-based healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built venv from the builder.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Non-root user for least-privilege execution.
RUN useradd --system --no-create-home --uid 1001 appuser

WORKDIR /app

# Copy application source.
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
# Seed + bootstrap scripts (required for first-boot entrypoint).
COPY seed.py .
COPY scripts/ ./scripts/
# Runtime config (tier price IDs, schema, marketing prose) — read at import time
# by app.subscription_service / marketing_routes. Without this the app crashes on
# boot: FileNotFoundError: /app/config/tiers.yaml.
COPY config/ ./config/
COPY entrypoint.sh .

RUN chmod +x /app/entrypoint.sh && chown -R appuser:appuser /app

# Create the sqlite data dir and give it to appuser BEFORE the USER switch.
# Docker seeds a fresh named volume from the image path's ownership on first
# mount, so /data must be appuser-owned here or the non-root process cannot
# create the sqlite file (OperationalError: unable to open database file).
RUN mkdir -p /data && chown appuser:appuser /data
VOLUME ["/data"]

USER appuser

EXPOSE 8200

# Health-check: /api/healthz (DB liveness probe).
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:8200/api/healthz || \
        python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/api/healthz')"

# Default: zero-config sqlite boot via entrypoint (can override for prod).
CMD ["/app/entrypoint.sh"]

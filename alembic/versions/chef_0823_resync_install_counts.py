"""install-integrity: re-sync Skill.install_count to organic installs

Revision ID: chef_0823_resync
Revises: bhint0823_client_ip
Create Date: 2026-08-23

CHEF-2026-08-23-A (t_4a38fed9). The denormalised ``Skill.install_count``
counter was bumped for EVERY install (including CI self-installs from the
on-host deploy runner, internal dogfood traffic, and self-registered
agent-probe keys), so public ranking surfaces ranked super-memory #1 with
366 "installs" of which only 12 were organic external installs. The write
paths now honour the shared organic predicate
(app/install_integrity.py:install_is_organic); this migration re-syncs the
counter to the organic event count in one pass so old traffic stops
polluting the number and the hourly drift probe (which compares counter to
organic event truth) sees no drift.

- Reversible: down() recomputes the counter from RAW (unfiltered) install
  event counts, restoring pre-fix semantics exactly.
- History preserved: install_events rows are never touched (task constraint
  #5) — only the denormalised counter changes.
- Fail-closed: a DB WITH install events refuses to re-sync without
  WR_SERVER_PUBLIC_IP (via app config or env) — re-syncing with an empty
  internal set would bake the polluted counts back in as "organic"
  (pitfall #24 posture). Fresh/empty databases skip the data pass entirely
  (vacuous — dev bootstrap and the CI matrix must not need prod config).
- Cross-dialect: the statements below run on BOTH PostgreSQL (prod) and
  SQLite (test suite runs full-chain up/downgrades through this revision),
  so they use correlated subqueries instead of UPDATE...FROM.
"""

from alembic import op
from sqlalchemy import bindparam, text

# revision identifiers, used by Alembic.
revision = "chef_0823_resync"
down_revision = "bhint0823_client_ip"
branch_labels = None
depends_on = None

# Organic predicate, mirrored in raw SQL (frozen here for reproducibility
# across app versions — the app-level definition lives in
# app/install_integrity.py). NULL/empty client_ip rows stay organic.
_ORGANIC_ROWS = """
    SELECT 1 FROM install_events ie
    LEFT JOIN api_keys ak ON ak.id = ie.api_key_id
    LEFT JOIN users u ON u.id = ak.user_id
    WHERE ie.skill_id = s.id
      AND NOT COALESCE(ak.is_test, false)
      AND NOT COALESCE(u.is_agent, false)
      AND (
            ie.client_ip IS NULL
         OR ie.client_ip = ''
         OR ie.client_ip NOT IN :ips
      )
"""


def _internal_ips() -> list[str]:
    """Resolve the internal IP set from app config at migration runtime.

    Falls back to env vars WR_SERVER_PUBLIC_IP / WR_KNOWN_INTERNAL_IPS when
    app.config cannot be imported (bare alembic invocation; the deploy
    pipeline loads .env via `set -a; source .env` before alembic runs).
    """
    server_ip = ""
    extra: list[str] = []
    try:
        from app.config import settings

        server_ip = settings.SERVER_PUBLIC_IP or ""
        extra = [i.strip() for i in settings.KNOWN_INTERNAL_IPS if i.strip()]
    except Exception:  # noqa: BLE001
        # Rationale: alembic may run outside the app venv/import context;
        # env vars carry the same values the app itself will read.
        import os

        server_ip = os.environ.get("WR_SERVER_PUBLIC_IP", "")
        raw_extra = os.environ.get("WR_KNOWN_INTERNAL_IPS", "")
        extra = [i.strip() for i in raw_extra.split(",") if i.strip()]
    if not server_ip:
        raise RuntimeError(
            "chef_0823_resync: WR_SERVER_PUBLIC_IP is not set — refusing to "
            "re-sync install counters without the internal-IP set (would "
            "bake polluted counts in as organic). Set it and re-run."
        )
    return sorted({server_ip, *extra})


def _count_table(bind, table: str) -> int:
    return int(bind.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def upgrade() -> None:
    bind = op.get_bind()

    # Fresh databases (dev bootstrap, CI matrix) have no install history —
    # the re-sync is vacuous; skip the data pass instead of demanding prod
    # config. Fail-closed still applies where it matters: a DB WITH install
    # events and no internal-IP config refuses rather than baking polluted
    # counts in as organic.
    if _count_table(bind, "install_events") == 0:
        return

    ips = _internal_ips()

    # Correlated UPDATE (works on Postgres AND SQLite — UPDATE...FROM is
    # Postgres-only and the test suite drives full-chain downgrades on
    # SQLite through this revision).
    op.execute(
        text(
            """
            UPDATE skills
            SET install_count = (
                SELECT COUNT(ie.id)
                FROM install_events ie
                LEFT JOIN api_keys ak ON ak.id = ie.api_key_id
                LEFT JOIN users u ON u.id = ak.user_id
                WHERE ie.skill_id = skills.id
                  AND NOT COALESCE(ak.is_test, false)
                  AND NOT COALESCE(u.is_agent, false)
                  AND (
                        ie.client_ip IS NULL
                     OR ie.client_ip = ''
                     OR ie.client_ip NOT IN :ips
                  )
            )
            """
        ).bindparams(bindparam("ips", ips, expanding=True))
    )


def downgrade() -> None:
    op.execute(
        text(
            """
            UPDATE skills
            SET install_count = (
                SELECT COUNT(ie.id)
                FROM install_events ie
                WHERE ie.skill_id = skills.id
            )
            """
        )
    )

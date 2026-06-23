"""Alembic environment configuration for WiseRecipes API.

Reads WR_DATABASE_URL from the environment; falls back to the sqlalchemy.url
in alembic.ini if the env-var is absent (useful for local dev).
"""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# -- Alembic config object ---------------------------------------------------
config = context.config

# Set up Python logging from alembic.ini.
# disable_existing_loggers=False is REQUIRED: fileConfig() defaults to disabling
# every logger not named in alembic.ini, which silently kills application loggers
# (e.g. wiserecipes.discord) for the rest of the process. That's harmless in a
# standalone `alembic` CLI invocation but corrupts in-process callers — the test
# suite (command.upgrade in migration tests) and the bootstrap.py upgrade path —
# by clobbering loggers other tests/code rely on. Keep app loggers intact.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# -- Override URL from environment variable ----------------------------------
_db_url = os.environ.get("WR_DATABASE_URL")
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)

# -- Import application metadata so autogenerate works -----------------------
from app.models import Base  # noqa: E402

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Migration runner helpers
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection needed)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (active DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite doesn't support ALTER TABLE for most DDL (ADD CONSTRAINT,
            # DROP COLUMN, ALTER COLUMN, etc.).  render_as_batch=True makes
            # Alembic use the copy-and-move strategy for such operations on
            # SQLite while remaining a no-op on Postgres.
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

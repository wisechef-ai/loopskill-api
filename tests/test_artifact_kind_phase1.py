"""Acceptance tests for feat/artifact-kind-phase1.

Tests the `kind` discriminator and `loop_spec` JSON payload on the Skill model,
the loop_spec validator, and the read-surface inclusion of both fields.

Fixture pattern follows tests/test_cookbook_routes.py (in-memory SQLite).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Skill
from app.services.composite_loop_validation import CompositeLoopValidationError
from app.services.loop_spec_validation import assert_kind_valid, validate_loop_spec


# ─────────────────────────── Fixtures ───────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _make_skill(db: Session, slug: str = "test-skill", **kwargs) -> Skill:
    now = datetime.now(timezone.utc)
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=f"Skill {slug}",
        description="test description",
        is_public=True,
        created_at=now,
        updated_at=now,
        **kwargs,
    )
    db.add(s)
    db.flush()
    return s


def _make_test_app(db_session: Session) -> FastAPI:
    from app.skill_routes import router as skill_router

    app = FastAPI()
    app.include_router(skill_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db_session
    return app


# ─────────────────────────── Test 1: Default kind ───────────────────────


def test_skill_defaults_kind_skill_and_loop_spec_none(db_session: Session) -> None:
    """A freshly-created Skill defaults to kind='skill' and loop_spec=None."""
    skill = _make_skill(db_session, "default-kind-skill")
    assert skill.kind == "skill"
    assert skill.loop_spec is None


# ─────────────────────────── Test 2: kind=loop round-trip ───────────────


def test_skill_kind_loop_roundtrip(db_session: Session) -> None:
    """A Skill can be created with kind='loop' + a valid loop_spec and round-trips both fields."""
    spec = {
        "schedule": "24h",
        "subagents_config": {"maker": {"model_tier": "sonnet", "toolsets": []}},
        "verifier_slug": "test-green-loop",
        "budget_usd": 2.0,
    }
    _make_skill(db_session, "loop-kind-skill", kind="loop", loop_spec=spec)
    db_session.flush()

    fetched = db_session.query(Skill).filter(Skill.slug == "loop-kind-skill").one()
    assert fetched.kind == "loop"
    assert fetched.loop_spec is not None
    assert fetched.loop_spec["schedule"] == "24h"
    assert fetched.loop_spec["verifier_slug"] == "test-green-loop"
    assert fetched.loop_spec["budget_usd"] == 2.0


# ─────────────────────────── Test 3: API response includes kind + loop_spec ─


def test_skill_detail_api_includes_kind_and_loop_spec(db_session: Session) -> None:
    """The skill detail API response includes `kind` and `loop_spec` fields."""
    _make_skill(db_session, "api-kind-skill", kind="loop", loop_spec={"schedule": "30m"})
    db_session.flush()

    app = _make_test_app(db_session)
    client = TestClient(app)

    response = client.get("/api/skills/api-kind-skill")
    assert response.status_code == 200
    data = response.json()
    assert "kind" in data, f"'kind' missing from response keys: {list(data.keys())}"
    assert "loop_spec" in data, f"'loop_spec' missing from response keys: {list(data.keys())}"
    assert data["kind"] == "loop"
    assert data["loop_spec"]["schedule"] == "30m"


# ─────────────────────────── Test 4: validate_loop_spec accepts good spec ──


def test_validate_loop_spec_accepts_valid(db_session: Session) -> None:
    """validate_loop_spec accepts a valid spec without raising."""
    spec = {
        "schedule": "24h",
        "subagents_config": {"maker": {"model_tier": "sonnet", "toolsets": []}},
        "verifier_slug": "test-green-loop",
        "budget_usd": 2.0,
    }
    validate_loop_spec(db_session, spec)


# ─────────────────────────── Test 5: validate_loop_spec rejections ─────────


def test_validate_loop_spec_rejects_wildcard_cron(db_session: Session) -> None:
    """validate_loop_spec rejects a '*'-cron schedule like '0 22 * * *'."""
    spec = {
        "schedule": "0 22 * * *",
        "subagents_config": {"maker": {"model_tier": "sonnet", "toolsets": []}},
        "verifier_slug": "test-green-loop",
    }
    with pytest.raises(CompositeLoopValidationError) as exc_info:
        validate_loop_spec(db_session, spec)
    assert exc_info.value.field == "schedule"


def test_validate_loop_spec_rejects_missing_maker(db_session: Session) -> None:
    """validate_loop_spec rejects subagents_config missing the 'maker' key."""
    spec = {
        "schedule": "24h",
        "subagents_config": {"checker": {"model_tier": "sonnet", "toolsets": []}},
        "verifier_slug": "test-green-loop",
    }
    with pytest.raises(CompositeLoopValidationError) as exc_info:
        validate_loop_spec(db_session, spec)
    assert exc_info.value.field == "subagents_config"


def test_validate_loop_spec_rejects_negative_budget(db_session: Session) -> None:
    """validate_loop_spec rejects a negative budget_usd."""
    spec = {
        "schedule": "24h",
        "subagents_config": {"maker": {"model_tier": "sonnet", "toolsets": []}},
        "verifier_slug": "test-green-loop",
        "budget_usd": -1.0,
    }
    with pytest.raises(CompositeLoopValidationError) as exc_info:
        validate_loop_spec(db_session, spec)
    assert exc_info.value.field == "budget_usd"


# ─────────────────────────── Test 6: assert_kind_valid ─────────────────────


def test_assert_kind_valid_rejects_invalid() -> None:
    """assert_kind_valid raises ValueError for unknown kind 'personality'."""
    with pytest.raises(ValueError, match="personality"):
        assert_kind_valid("personality")


def test_assert_kind_valid_accepts_loop() -> None:
    """assert_kind_valid passes silently for kind='loop'."""
    assert_kind_valid("loop")


# ───────── Test 7: migration backfills kind='skill' on PRE-EXISTING rows ─────
# (Codex review 2026-07-06: the ORM-default tests prove NEW rows default to
# 'skill', but the core NON-BREAKING claim is that a row which existed BEFORE
# the migration survives the upgrade with kind='skill' populated by the
# server_default. This locks that claim end-to-end via a real alembic upgrade.)


def test_migration_backfills_existing_rows_kind_skill(tmp_path) -> None:
    """A row that existed BEFORE am0706 gets kind='skill' after the migration.

    Codex review 2026-07-06: the ORM-default tests prove NEW rows default to
    'skill', but the core NON-BREAKING claim is that a row which existed BEFORE
    the migration survives with kind='skill' (server_default backfill). This
    locks that claim by running am0706's upgrade() DDL against a pre-migration
    skills table. A focused table is used rather than the full historical chain,
    which — like the repo's own migration tests — is not replayable from base on
    sqlite.
    """
    import importlib.util
    import uuid

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    row_id = str(uuid.uuid4())
    with engine.begin() as conn:
        # Pre-migration skills table: columns the legacy INSERT needs, NO kind.
        conn.execute(
            text(
                "CREATE TABLE skills (id TEXT PRIMARY KEY, slug TEXT, title TEXT, "
                "description TEXT, is_public INTEGER)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO skills (id, slug, title, description, is_public) VALUES (:id, :slug, :t, :d, 1)"
            ),
            {"id": row_id, "slug": "pre-existing-skill", "t": "Pre-existing", "d": "legacy"},
        )
        cols_before = {r[1] for r in conn.execute(text("PRAGMA table_info(skills)"))}
        assert "kind" not in cols_before

    # Load the migration module and run ONLY its upgrade() via a real Operations
    # context bound to this engine (alembic.op proxies to the active context).
    spec = importlib.util.spec_from_file_location(
        "am0706_skill_kind", "alembic/versions/am0706_skill_kind.py"
    )
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()

    with engine.begin() as conn:
        cols_after = {r[1] for r in conn.execute(text("PRAGMA table_info(skills)"))}
        assert "kind" in cols_after
        assert "loop_spec" in cols_after
        kind, loop_spec = conn.execute(
            text("SELECT kind, loop_spec FROM skills WHERE id = :id"), {"id": row_id}
        ).fetchone()
    assert kind == "skill", f"pre-existing row should backfill to 'skill', got {kind!r}"
    assert loop_spec is None

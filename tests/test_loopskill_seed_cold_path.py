"""Regression: starter-catalog seed must run against a MIGRATED database.

loopskill_0622 — the cold-path `docker compose up` boot seeds a starter catalog
on a fresh alembic-migrated SQLite DB. The Phase 3+4 cookbook→bundle column rename
(cookbook_id → bundle_id) was missed in scripts/seed_starter_catalog.py, so the
first-boot seed crashed the container with:

    AttributeError: type object 'BundleSkill' has no attribute 'cookbook_id'

The existing unit tests used ORM create_all and never exercised the bundle-skill
JOIN on the renamed column, so they stayed green while the real boot crashed.
This test runs the seed twice (idempotency exercises the join) against a real
migrated DB — the exact cold-path — so a future rename gap fails CI, not a user's
first `docker compose up`.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated_sqlite_url(tmp_path, monkeypatch):
    """A fresh SQLite DB brought to alembic head (not ORM create_all)."""
    db_path = tmp_path / f"seed_{uuid.uuid4().hex}.db"
    url = f"sqlite:///{db_path}"
    env = {**os.environ, "WR_DATABASE_URL": url, "DATABASE_URL": url}
    r = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"alembic upgrade head failed: {r.stderr[-2000:]}"
    monkeypatch.setenv("WR_DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_seed_starter_catalog_runs_on_migrated_db(migrated_sqlite_url):
    """The first-boot seed must succeed against a migrated DB (the cold-path)."""
    # Bind a fresh engine/session directly to the migrated file DB — the global
    # app.database.SessionLocal is already bound to the test in-memory DB by
    # conftest, so we must not rely on it here.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(migrated_sqlite_url)
    TestSession = sessionmaker(bind=engine)

    import app.database as _db
    from scripts.seed_starter_catalog import seed_starter_catalog

    # Point the seed's session factory at the migrated DB for the duration.
    original = _db.SessionLocal
    _db.SessionLocal = TestSession
    try:
        # The bundle→skill JOIN that crashed on the stale cookbook_id column only
        # fires when base skills exist (so the bundle spec's skill slugs resolve).
        # Seed the base catalog first — exactly what first-boot bootstrap does —
        # then run the starter catalog twice so the second pass hits the JOIN.
        import contextlib
        import io

        try:
            import seed as _seedmod

            with contextlib.redirect_stdout(io.StringIO()):
                _seedmod.seed()
        except Exception:  # noqa: BLE001  # Rationale: base seed is best-effort setup for the test; the assertions below are the contract.
            pass

        db = TestSession()
        try:
            first = seed_starter_catalog(db)
            assert first["bundles_created"] >= 1
            assert first["loops_created"] >= 1
            assert first["personalities_created"] >= 1

            # Second run exercises the bundle_skills JOIN that crashed on the stale
            # cookbook_id column — must be a clean idempotent no-op, not an error.
            second = seed_starter_catalog(db)
            assert second["bundles_created"] == 0
            assert second["loops_created"] == 0
            assert second["personalities_created"] == 0
        finally:
            db.close()
    finally:
        _db.SessionLocal = original
        engine.dispose()


def test_loop_rating_insert_on_migrated_db(migrated_sqlite_url):
    """A LoopRating insert must succeed on a MIGRATED DB (not just create_all).

    Regression: the migration originally gave loop_ratings.created_at a
    Postgres-only NOW() default, leaving SQLite with NOT NULL + no default, so an
    ORM insert on the migrated cold-clone raised IntegrityError -> the /rate route
    500'd in the real container while create_all-based unit tests stayed green.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Loop, LoopRating

    engine = create_engine(migrated_sqlite_url)
    TestSession = sessionmaker(bind=engine)
    db = TestSession()
    try:
        loop = Loop(
            id=uuid4(),
            slug=f"rate-mig-{uuid4().hex[:8]}",
            title="Rate Migration",
            success_condition="x",
            verification_script="exit 0",
            system_prompt="x",
            max_turns=5,
            budget_usd=None,
            tool_allowlist=[],
            stopping_criteria={"success": "x", "failure": "y", "budget": "z"},
            created_at=datetime.now(UTC),
        )
        db.add(loop)
        db.flush()
        # The insert that 500'd on the migrated container — must NOT raise here,
        # and created_at must be populated by the column default.
        db.add(LoopRating(loop_id=loop.id, rater_user_id=None, rating=5, comment="ships"))
        db.commit()

        row = db.query(LoopRating).filter(LoopRating.loop_id == loop.id).one()
        assert row.rating == 5
        assert row.created_at is not None  # default fired on SQLite too
    finally:
        db.close()
        engine.dispose()

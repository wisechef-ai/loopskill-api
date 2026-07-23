"""atomic-habits 2026-07-23 rank-8 REVENUE/CATALOG — regression: atomic-habits
and dreaming composite loops must resolve tags after `alembic upgrade head`,
and untagged loops must resolve to [] (never a hard failure).

Mirrors ah0721's migrated-sqlite-fixture pattern. Guards against the
composite_loops.tags column silently disappearing or the backfill drifting.
"""

from __future__ import annotations

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def migrated_sqlite_url(tmp_path, monkeypatch):
    db_path = tmp_path / "ah0723_test.db"
    url = f"sqlite:///{db_path}"
    env = {**os.environ, "WR_DATABASE_URL": url, "DATABASE_URL": url}
    r = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"alembic upgrade head failed: {r.stderr[-2000:]}"
    monkeypatch.setenv("WR_DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_composite_loops_tags_column_exists(migrated_sqlite_url):
    """Sanity: a bare migrated DB has the tags column (no data yet)."""
    from sqlalchemy import create_engine, text

    engine = create_engine(migrated_sqlite_url)
    with engine.connect() as conn:
        # SQLite raises OperationalError if the column doesn't exist —
        # a clean SELECT (even with zero rows) is the assertion.
        conn.execute(text("SELECT tags FROM composite_loops"))
    engine.dispose()


@pytest.mark.parametrize(
    "slug,expected_tags",
    [
        ("atomic-habits", ["self-improvement", "agent-ops", "daily", "compounding", "scheduled"]),
        ("dreaming", ["self-improvement", "memory", "consolidation", "agent-ops", "scheduled"]),
    ],
)
def test_ah0723_backfills_tags_for_flagship_loops(migrated_sqlite_url, slug, expected_tags):
    """Simulate the prod scenario: the loop pre-exists (published directly,
    before this migration ran), THEN re-run migration to head — must
    backfill exactly the expected tags and be idempotent on replay."""
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(migrated_sqlite_url)
    TestSession = sessionmaker(bind=engine)

    import app.database as _db

    original = _db.SessionLocal
    _db.SessionLocal = TestSession
    try:
        db = TestSession()
        try:
            from app.models import CompositeLoop

            loop = CompositeLoop(
                id=uuid.uuid4(),
                slug=slug,
                title=f"{slug} title",
                schedule="24h",
                skills=[],
                connectors=[],
                subagents_config={},
                verifier_slug="test-green-loop",
                state_seed={},
                prompt="x",
                is_public=True,
                tier="free",
            )
            db.add(loop)
            db.commit()
        finally:
            db.close()
    finally:
        _db.SessionLocal = original
        engine.dispose()

    env = {**os.environ, "WR_DATABASE_URL": migrated_sqlite_url, "DATABASE_URL": migrated_sqlite_url}
    r2 = subprocess.run(
        ["alembic", "downgrade", "ah0721_composite_loop_ver"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r2.returncode == 0, f"downgrade failed: {r2.stderr[-2000:]}"
    r3 = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r3.returncode == 0, f"re-upgrade failed: {r3.stderr[-2000:]}"

    import json as _json

    engine2 = create_engine(migrated_sqlite_url)
    with engine2.connect() as conn:
        from sqlalchemy import text as sqltext

        row = conn.execute(
            sqltext("SELECT tags FROM composite_loops WHERE slug = :slug"), {"slug": slug}
        ).first()
    engine2.dispose()

    assert row is not None
    tags = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert tags == expected_tags


def test_untagged_composite_loop_resolves_to_empty_list(migrated_sqlite_url):
    """A composite loop not in the backfill map must resolve tags to [] via
    the API layer, never raise — mirrors the loops.tags NULL-safety contract."""
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(migrated_sqlite_url)
    TestSession = sessionmaker(bind=engine)

    import app.database as _db

    original = _db.SessionLocal
    _db.SessionLocal = TestSession
    try:
        db = TestSession()
        try:
            from app.composite_loop_routes import _composite_loop_to_out
            from app.models import CompositeLoop

            loop = CompositeLoop(
                id=uuid.uuid4(),
                slug="untagged-loop-ah0723",
                title="untagged",
                schedule="24h",
                skills=[],
                connectors=[],
                subagents_config={},
                verifier_slug="test-green-loop",
                state_seed={},
                prompt="x",
                is_public=True,
                tier="free",
            )
            db.add(loop)
            db.commit()
            db.refresh(loop)
            out = _composite_loop_to_out(loop)
            assert out.tags == []
        finally:
            db.close()
    finally:
        _db.SessionLocal = original
        engine.dispose()

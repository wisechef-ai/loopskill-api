"""atomic-habits 2026-07-21 rank-1 + rank-8 — regression: atomic-habits and
dreaming composite loops must resolve latest_version after
`alembic upgrade head`.

Root cause covered: neither loop ever received the v1.0.0
CompositeLoopVersion row that POST /api/composite-loops/{slug}/versions
would normally mint, because both were published directly against the live
DB and there is no seed-script backfill path for composite_loops (unlike the
old `loops` table's scripts/seed_starter_catalog.py). Migration
ah0721_composite_loop_ver backfills the missing rows using a manifest shape
sourced verbatim from the live GET /api/composite-loops/{slug} response, so
this test also guards against manifest drift.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def migrated_sqlite_url(tmp_path, monkeypatch):
    db_path = tmp_path / "ah0721_test.db"
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


def test_atomic_habits_has_no_version_before_publish(migrated_sqlite_url):
    """Sanity: a bare migrated DB has no `composite_loops` row at all yet —
    the migration must be a safe no-op here."""
    from sqlalchemy import create_engine, text

    engine = create_engine(migrated_sqlite_url)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM composite_loops WHERE slug = 'atomic-habits'")).first()
    assert row is None  # migration no-ops cleanly when the loop doesn't exist yet
    engine.dispose()


@pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
def test_ah0721_backfills_version_when_loop_preexists(migrated_sqlite_url, slug):
    """Simulate the prod scenario: the composite loop was published directly
    (no CompositeLoopVersion), THEN re-run the migration idempotently — must
    mint exactly one v1.0.0 row and be a no-op on a second run."""
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
    # Re-run downgrade-then-upgrade as an idempotent replay smoke (matches the
    # ah0720 pattern — we're already at head, so this exercises both paths).
    #
    # ah0723_composite_loop_tags (2026-07-23): a new migration chained on top
    # of ah0721 means a relative "downgrade -1" no longer replays ah0721's
    # backfill logic — it only undoes ah0723's tags column. Target ah0721's
    # OWN down_revision explicitly so this test keeps exercising the exact
    # upgrade() this test is named for, regardless of how many migrations
    # get chained on afterward.
    r2 = subprocess.run(
        ["alembic", "downgrade", "ah0720_repo_steward_ver"],
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

    engine2 = create_engine(migrated_sqlite_url)
    with engine2.connect() as conn:
        from sqlalchemy import text as sqltext

        rows = conn.execute(
            sqltext(
                "SELECT semver, manifest FROM composite_loop_versions cv "
                "JOIN composite_loops cl ON cl.id = cv.composite_loop_id WHERE cl.slug = :slug"
            ),
            {"slug": slug},
        ).fetchall()
    engine2.dispose()

    assert len(rows) == 1, f"expected exactly one v1.0.0 CompositeLoopVersion for {slug}, got {rows}"
    assert rows[0][0] == "1.0.0"
    manifest = json.loads(rows[0][1])
    assert manifest["slug"] == slug
    assert manifest["verifier_slug"] == "test-green-loop"
    assert manifest["prompt"]

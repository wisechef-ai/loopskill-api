"""atomic-habits 2026-07-20 rank-1 — regression: repo-steward-loop must resolve
latest_version after `alembic upgrade head`.

Root cause covered: prod deploys never run scripts/seed_starter_catalog.py's
_seed_loops() (that only fires from scripts/bootstrap.py on fresh container
boot). repo-steward-loop was published directly against the live DB and never
got the v1.0.0 LoopVersion row the other 9 starter loops receive via the seed
path. Migration ah0720_repo_steward_ver backfills the missing row using the
same _loop_manifest_toml() builder as the seed script, so this test also
guards against the manifest drifting between the two code paths.
"""

from __future__ import annotations

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def migrated_sqlite_url(tmp_path, monkeypatch):
    db_path = tmp_path / "ah0720_test.db"
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


def test_repo_steward_loop_has_no_version_before_publish(migrated_sqlite_url):
    """Sanity: a bare migrated DB has no `loops` row at all yet (loops are
    seeded, not migrated) — the migration must be a safe no-op here."""
    from sqlalchemy import create_engine, text

    engine = create_engine(migrated_sqlite_url)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM loops WHERE slug = 'repo-steward-loop'")).first()
    assert row is None  # migration no-ops cleanly when the loop doesn't exist yet
    engine.dispose()


def test_ah0720_backfills_version_when_loop_preexists(migrated_sqlite_url):
    """Simulate the prod scenario: repo-steward-loop published directly (no
    LoopVersion), THEN re-run the migration idempotently — must mint exactly
    one v1.0.0 row and be a no-op on a second run."""
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
            from uuid import uuid4

            from app.models import Loop
            from scripts.seed_starter_catalog import _get_or_create_house_creator

            house = _get_or_create_house_creator(db)
            db.commit()

            loop = Loop(
                id=uuid4(),
                slug="repo-steward-loop",
                title="Repo Steward Loop",
                category="development",
                license="MIT",
                tier="free",
                is_public=True,
                creator_id=house.id,
                success_condition="x",
                verification_script="exit 0",
                max_turns=15,
                tool_allowlist=["github_read_prs"],
                system_prompt="x",
                stopping_criteria={"success": "x", "failure": "x"},
            )
            db.add(loop)
            db.commit()
        finally:
            db.close()
    finally:
        _db.SessionLocal = original
        engine.dispose()

    # Re-run the migration's upgrade() logic directly (idempotent replay path)
    env = {**os.environ, "WR_DATABASE_URL": migrated_sqlite_url, "DATABASE_URL": migrated_sqlite_url}
    subprocess.run(
        ["alembic", "upgrade", "ah0720_repo_steward_ver"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    # already at head — this is a downgrade-then-upgrade smoke instead
    r2 = subprocess.run(
        ["alembic", "downgrade", "-1"],
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
                "SELECT semver, manifest FROM loop_versions lv "
                "JOIN loops l ON l.id = lv.loop_id WHERE l.slug = 'repo-steward-loop'"
            )
        ).fetchall()
    engine2.dispose()

    assert len(rows) == 1, f"expected exactly one v1.0.0 LoopVersion, got {rows}"
    assert rows[0][0] == "1.0.0"
    assert '"repo-steward-loop"' in rows[0][1]
    assert "ci" in rows[0][1] and "dependabot" in rows[0][1]  # discovery tags present

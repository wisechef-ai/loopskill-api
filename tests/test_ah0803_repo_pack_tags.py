"""atomic-habits 2026-08-03 rank-8 REVENUE/CATALOG — regression:
repo-stewardship-pack must resolve discovery tags after `alembic upgrade
head`, and the tag-filtered browse view must include it.

Mirrors test_ah0723_composite_loop_tags.py's migrated-sqlite-fixture
pattern exactly, applied to the repo-stewardship-pack backfill
(ah0803_repo_pack_tags).
"""

from __future__ import annotations

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def migrated_sqlite_url(tmp_path, monkeypatch):
    db_path = tmp_path / "ah0803_test.db"
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


def _mk_composite_loop(db, slug):
    import uuid

    from app.models import CompositeLoop

    loop = CompositeLoop(
        id=uuid.uuid4(),
        slug=slug,
        title=f"{slug} title",
        schedule="30m",
        skills=[],
        connectors=[],
        subagents_config={},
        verifier_slug="repo-steward-loop",
        state_seed={},
        prompt="x",
        is_public=True,
        tier="pro",
    )
    db.add(loop)
    db.commit()
    return loop


def test_ah0803_backfills_tags_for_repo_stewardship_pack(migrated_sqlite_url):
    """Simulate the prod scenario: the pack pre-exists (published before this
    migration ran), THEN re-run migration to head — must backfill the
    expected discovery tags and be idempotent on replay."""
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
            _mk_composite_loop(db, "repo-stewardship-pack")
        finally:
            db.close()
    finally:
        _db.SessionLocal = original
        engine.dispose()

    env = {**os.environ, "WR_DATABASE_URL": migrated_sqlite_url, "DATABASE_URL": migrated_sqlite_url}
    r2 = subprocess.run(
        ["alembic", "downgrade", "c0208_p1_pin_intent"],
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
    from sqlalchemy import text as sqltext

    with engine2.connect() as conn:
        row = conn.execute(
            sqltext("SELECT tags FROM composite_loops WHERE slug = :slug"),
            {"slug": "repo-stewardship-pack"},
        ).first()
    engine2.dispose()

    assert row is not None
    tags = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert tags == ["agent-ops", "ci", "github", "code-quality", "scheduled"]


def test_ah0803_tagged_pack_survives_tag_filter(migrated_sqlite_url):
    """The whole point of the backfill: the pack must now show up under the
    same tag-filtered surfaces as its already-tagged siblings (composite
    tag filter is server-side, composite_loop_routes.py:97-98)."""
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
            from app.composite_loop_routes import list_composite_loops
            from app.models import CompositeLoop

            loop = CompositeLoop(
                id=__import__("uuid").uuid4(),
                slug="repo-stewardship-pack",
                title="repo-stewardship-pack",
                schedule="30m",
                skills=[],
                connectors=[],
                subagents_config={},
                verifier_slug="repo-steward-loop",
                state_seed={},
                prompt="x",
                is_public=True,
                tier="pro",
                tags=["agent-ops", "ci", "github", "code-quality", "scheduled"],
            )
            db.add(loop)
            db.commit()

            rows = list_composite_loops(q=None, tag="agent-ops", limit=100, db=db)
            slugs = {r.slug for r in rows}
            assert "repo-stewardship-pack" in slugs
        finally:
            db.close()
    finally:
        _db.SessionLocal = original
        engine.dispose()

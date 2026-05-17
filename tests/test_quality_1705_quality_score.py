"""tests/test_quality_1705_quality_score.py — Phase C scoring gates."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.models import Base, Skill  # noqa: E402
from scripts import quality_1705_compute_quality_score as scorer  # noqa: E402


@pytest.fixture()
def db_engine(tmp_path):
    db_path = tmp_path / "qs.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(db_engine):
    return sessionmaker(bind=db_engine, future=True)


def test_quality_score_column_exists(db_engine):
    with db_engine.connect() as conn:
        ddl = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='skills'"
        )).scalar()
    assert "quality_score" in ddl


def test_install_score_zero_for_no_installs():
    assert scorer._install_score(0, [0, 5, 10, 100]) == 0.0


def test_install_score_full_for_top_installer():
    s = scorer._install_score(100, [0, 5, 10, 100])
    assert s == 10.0


def test_freshness_score_full_for_recent():
    now = datetime.now(timezone.utc)
    assert scorer._freshness_score(now, now) == 10.0
    assert scorer._freshness_score(now - timedelta(days=29), now) == 10.0


def test_freshness_score_zero_for_year_old():
    now = datetime.now(timezone.utc)
    assert scorer._freshness_score(now - timedelta(days=365), now) == 0.0


def test_freshness_score_decays_mid_range():
    now = datetime.now(timezone.utc)
    s = scorer._freshness_score(now - timedelta(days=180), now)
    assert 3 < s < 7  # roughly midway


def test_freshness_score_none_returns_zero():
    assert scorer._freshness_score(None, datetime.now(timezone.utc)) == 0.0


def test_description_score_with_outcome_verb():
    desc = "Generates a PDF report from GA4 and Stripe data, branded and white-label-ready, replacing 4h/week of manual work."
    assert scorer._description_score(desc) == 10.0


def test_description_score_long_without_verb():
    desc = (
        "An interesting tool that helps users do something useful with their "
        "data — useful for many scenarios in modern software development."
    )
    # Does not start with an outcome verb
    assert scorer._description_score(desc) == 5.0


def test_description_score_too_short():
    assert scorer._description_score("short desc") == 0.0


def test_description_score_none():
    assert scorer._description_score(None) == 0.0


def test_compute_score_new_skill_capped_at_8_5():
    """F8 mitigation: skills < 14 days old cap at 8.5."""
    now = datetime.now(timezone.utc)
    score = scorer.compute_score(
        install_count=100,
        last_verified=now,
        description="Generates excellent things, with care, for everyone who uses it.",
        created_at=now - timedelta(days=3),  # brand new
        all_install_counts=[0, 0, 100],
        now=now,
    )
    assert score <= 8.5


def test_compute_score_mature_skill_can_reach_high():
    """A mature skill with all signals strong should score > 8.5."""
    now = datetime.now(timezone.utc)
    score = scorer.compute_score(
        install_count=500,
        last_verified=now,
        description="Generates a comprehensive PDF report from analytics data, branded and white-label-ready, saving hours per week.",
        created_at=now - timedelta(days=60),
        all_install_counts=[0, 10, 50, 500],
        now=now,
    )
    assert score >= 8.0


def test_main_dry_run_writes_nothing(session_factory, monkeypatch, capsys):
    """Dry-run must not mutate."""
    session = session_factory()
    now = datetime.now(timezone.utc)
    session.add(
        Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates good things for testing purposes.",
            is_public=True,
            is_archived=False,
            install_count=10,
            last_verified=now,
        )
    )
    session.commit()
    db_url = str(session.bind.engine.url)
    session.close()

    sys.argv = ["scorer", "--db-url", db_url]
    scorer.main()

    SessionLocal = sessionmaker(bind=session.bind.engine, future=True)
    with SessionLocal() as s:
        score = s.execute(text("SELECT quality_score FROM skills WHERE slug='test-skill'")).scalar()
    assert score is None, "Dry-run should not write quality_score"


def test_main_commit_writes_scores(session_factory):
    session = session_factory()
    now = datetime.now(timezone.utc)
    session.add(
        Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates good things for testing purposes that really matters.",
            is_public=True,
            is_archived=False,
            install_count=10,
            last_verified=now,
        )
    )
    session.commit()
    db_url = str(session.bind.engine.url)
    session.close()

    sys.argv = ["scorer", "--commit", "--db-url", db_url]
    scorer.main()

    SessionLocal = sessionmaker(bind=session.bind.engine, future=True)
    with SessionLocal() as s:
        score = s.execute(text("SELECT quality_score FROM skills WHERE slug='test-skill'")).scalar()
    assert score is not None
    assert 0 <= score <= 10


def test_main_is_idempotent(session_factory):
    """Running --commit twice produces zero updates on the second run."""
    session = session_factory()
    now = datetime.now(timezone.utc)
    session.add(
        Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates good things for testing purposes that really matters.",
            is_public=True,
            is_archived=False,
            install_count=10,
            last_verified=now,
        )
    )
    session.commit()
    db_url = str(session.bind.engine.url)
    session.close()

    import io
    sys.argv = ["scorer", "--commit", "--db-url", db_url]
    scorer.main()

    # Second run: capture stdout, parse updated_count
    sys.argv = ["scorer", "--commit", "--db-url", db_url]
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        scorer.main()
    out = buf.getvalue()
    import json
    payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
    assert payload["updated_count"] == 0, "Idempotent: second --commit produces zero diffs"

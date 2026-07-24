"""Tests for app.services.category_infer and the category-backfill migration.

atomic-habits 2026-07-16 rank-1: category backfill on the 48 uncategorized
skills. Mirrors the pattern in tests/test_taxonomy_migration.py — replay the
migration's SQL logic against a fresh SQLite DB and assert on real behavior.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Skill
from app.services.category_infer import (
    CANONICAL_CATEGORIES,
    classify_category,
)


def _load_migration():
    path = (
        Path(__file__).parent.parent
        / "alembic" / "versions" / "f8ade9aa1b68_category_backfill_null.py"
    )
    spec = importlib.util.spec_from_file_location("category_backfill_null", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mig = _load_migration()


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _mk(session, **kw):
    s = Skill(id=uuid4(), slug=kw.pop("slug", f"slug-{uuid4().hex[:8]}"), title=kw.pop("title", "t"), **kw)
    session.add(s)
    session.commit()
    return s


# ---------------------------------------------------------------------------
# classify_category unit tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,slug,description,expected",
    [
        ("Web Scraper Pro", "web-scraper-pro", "Scrape any site", "data"),
        ("Email Composer", "email-composer", "Draft marketing emails", "marketing"),
        ("Client Reporter", "client-reporter", "Client reporting for agencies", "agency"),
        ("LoopSkill CLI", "loopskill", "Skill marketplace CLI", "dev-tools"),
        ("Super Memory", "super-memory", "Agent memory recall and knowledge graph", "research"),
        ("Musk 5-Step Algorithm", "musk-5-step-algorithm", "Engineering algorithm for bottlenecks", "productivity"),
        ("Ruthless Mentor", "ruthless-mentor", "Stress-test plans and ideas", "productivity"),
        ("Hundred Million Offers", "hundred-million-offers", "Craft irresistible offers", "marketing"),
        ("Hub Search Claude Code", "hub-search-claude-code", "Search the skill hub", "dev-tools"),
        ("Plan For Goal", "plan-for-goal", "Write a goal execution plan", "productivity"),
        ("Cron Fleet SSH Pull", "cron-fleet-ssh-pull", "Watchdog for cron pulls", "ops"),
        ("Code Reviewer", "code-reviewer", "Lint and review pull requests", "code-review"),
    ],
)
def test_classify_category_known_skills(title, slug, description, expected):
    assert classify_category(title=title, slug=slug, description=description) == expected


def test_classify_category_falls_back_to_productivity_on_no_signal():
    assert classify_category() == "productivity"
    assert classify_category(title="", slug="", description="") == "productivity"


def test_classify_category_never_returns_outside_canonical_set():
    samples = [
        ("xyz123", "xyz123", "totally unrecognizable gibberish qwerty"),
        ("Blockchain NFT Minter", "nft-minter", "mint tokens on chain"),
    ]
    for title, slug, desc in samples:
        result = classify_category(title=title, slug=slug, description=desc)
        assert result in CANONICAL_CATEGORIES


# ---------------------------------------------------------------------------
# Migration behavior tests
# ---------------------------------------------------------------------------

def test_migration_backfills_all_null_categories(engine, session):
    _mk(session, slug="web-scraper-pro", title="Web Scraper Pro",
        description="Scrape any website", category=None)
    _mk(session, slug="email-composer", title="Email Composer",
        description="Draft marketing emails", category=None)
    _mk(session, slug="already-set", title="Already Categorized",
        description="n/a", category="ops")
    _mk(session, slug="no-signal-at-all", title="", description="", category=None)

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, title, slug, description, readme FROM skills WHERE category IS NULL")
        ).fetchall()
        for row in rows:
            inferred = _mig.classify_category(
                title=row.title, description=row.description,
                slug=row.slug, readme=row.readme,
            )
            conn.execute(
                text("UPDATE skills SET category = :cat WHERE id = :id"),
                {"cat": inferred, "id": row.id},
            )

    session.expire_all()
    skills = {s.slug: s.category for s in session.query(Skill).all()}

    assert skills["web-scraper-pro"] == "data"
    assert skills["email-composer"] == "marketing"
    assert skills["already-set"] == "ops"  # untouched — was not NULL
    assert skills["no-signal-at-all"] == "productivity"  # fallback, never NULL

    # The whole point of the migration: zero NULLs survive.
    assert all(cat is not None for cat in skills.values())


def test_migration_is_idempotent_on_replay(engine, session):
    _mk(session, slug="a", title="Web Scraper", description="scrape", category=None)

    def _replay():
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, title, slug, description, readme FROM skills WHERE category IS NULL")
            ).fetchall()
            for row in rows:
                inferred = _mig.classify_category(
                    title=row.title, description=row.description,
                    slug=row.slug, readme=row.readme,
                )
                conn.execute(
                    text("UPDATE skills SET category = :cat WHERE id = :id"),
                    {"cat": inferred, "id": row.id},
                )

    _replay()
    session.expire_all()
    first_pass = session.query(Skill).filter_by(slug="a").one().category

    _replay()  # second run should be a no-op (no NULL rows left to touch)
    session.expire_all()
    second_pass = session.query(Skill).filter_by(slug="a").one().category

    assert first_pass == second_pass == "data"

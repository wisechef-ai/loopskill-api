"""Tests for spotify_0608 Ph H — editorial cookbook seed.

Validates scripts/seed_editorial_cookbooks.py against an in-memory DB:
  - creates a SYSTEM user (editorial@wisechef.ai), never is_base
  - seeds public cookbooks owned by that user
  - HERO 'the-awakened-agent' composes summarize-cli + super-memory + chef
  - idempotent (re-run upserts, no duplicate cookbooks/memberships)
  - attaches only REAL catalog skills; missing slugs skipped (not fabricated)
  - never mutates the is_base catalog cookbook
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as appdb
from app.models import Base, Cookbook, CookbookSkill, Skill, User

# Load the seed script as a module (scripts/ isn't a package).
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "seed_editorial_cookbooks.py"
_spec = importlib.util.spec_from_file_location("seed_editorial_cookbooks", _SCRIPT)
seed_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_mod)


@pytest.fixture()
def db(monkeypatch) -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # The script calls app.database.SessionLocal() — point it at our test engine.
    monkeypatch.setattr(appdb, "SessionLocal", TestSession)
    monkeypatch.setattr(seed_mod, "SessionLocal", TestSession, raising=False)

    s = TestSession()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(bind=engine)


def _mk_skill(db, slug):
    db.add(Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, install_count=0))
    db.commit()


def _seed_real_catalog(db):
    """Create every skill the editorial specs reference so attachments resolve."""
    slugs = set()
    for spec in seed_mod.EDITORIAL_COOKBOOKS:
        slugs.update(spec["skills"])
    for s in slugs:
        _mk_skill(db, s)


def test_seed_creates_system_user_and_public_cookbooks(db):
    _seed_real_catalog(db)
    rc = seed_mod.seed(dry_run=False)
    assert rc == 0

    system = db.query(User).filter(User.email == seed_mod.SYSTEM_EMAIL).first()
    assert system is not None
    cbs = db.query(Cookbook).filter(Cookbook.cookbook_owner == system.id).all()
    assert len(cbs) == len(seed_mod.EDITORIAL_COOKBOOKS) == 10
    for cb in cbs:
        assert cb.visibility == "public"
        assert cb.slug is not None
        assert cb.is_base is False


def test_hero_cookbook_composition(db):
    _seed_real_catalog(db)
    seed_mod.seed(dry_run=False)
    hero = db.query(Cookbook).filter(Cookbook.slug == "the-awakened-agent").first()
    assert hero is not None
    assert hero.is_verified is True
    skills = (
        db.query(Skill.slug)
        .join(CookbookSkill, CookbookSkill.skill_id == Skill.id)
        .filter(CookbookSkill.cookbook_id == hero.id)
        .all()
    )
    slugs = {s[0] for s in skills}
    assert slugs == {"summarize-cli", "super-memory", "chef"}


def test_seed_is_idempotent(db):
    _seed_real_catalog(db)
    seed_mod.seed(dry_run=False)
    n_cb_1 = db.query(Cookbook).count()
    n_link_1 = db.query(CookbookSkill).count()
    # Re-run — must not duplicate.
    seed_mod.seed(dry_run=False)
    assert db.query(Cookbook).count() == n_cb_1
    assert db.query(CookbookSkill).count() == n_link_1


def test_missing_slug_is_skipped_not_fabricated(db):
    # Seed catalog MINUS one hero skill → that attachment must be skipped, the
    # cookbook still created, and no phantom Skill row invented.
    all_slugs = set()
    for spec in seed_mod.EDITORIAL_COOKBOOKS:
        all_slugs.update(spec["skills"])
    all_slugs.discard("chef")  # drop one
    for s in all_slugs:
        _mk_skill(db, s)

    seed_mod.seed(dry_run=False)
    # chef was never created → no Skill row, no membership referencing it.
    assert db.query(Skill).filter(Skill.slug == "chef").first() is None
    hero = db.query(Cookbook).filter(Cookbook.slug == "the-awakened-agent").first()
    assert hero is not None  # cookbook still seeded
    hero_slugs = {
        s[0]
        for s in db.query(Skill.slug)
        .join(CookbookSkill, CookbookSkill.skill_id == Skill.id)
        .filter(CookbookSkill.cookbook_id == hero.id)
        .all()
    }
    assert "chef" not in hero_slugs
    assert "summarize-cli" in hero_slugs  # the present ones still attached


def test_never_mutates_is_base_cookbook(db):
    _seed_real_catalog(db)
    # A pre-existing is_base catalog cookbook must be untouched by the seed.
    base = Cookbook(id=uuid.uuid4(), name="WiseChef Recipes Catalog", is_base=True, visibility="private")
    db.add(base)
    db.commit()
    base_id = base.id

    seed_mod.seed(dry_run=False)

    base_after = db.query(Cookbook).filter(Cookbook.id == base_id).first()
    assert base_after.is_base is True
    assert base_after.visibility == "private"  # not flipped to public
    assert base_after.name == "WiseChef Recipes Catalog"


def test_dry_run_writes_nothing(db):
    _seed_real_catalog(db)
    before = db.query(Cookbook).count()
    seed_mod.seed(dry_run=True)
    assert db.query(Cookbook).count() == before  # rolled back

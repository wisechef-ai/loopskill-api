"""Regression: the free tier is an allowlist, not a default.

Guards against the 2026-06-01 / 2026-06-05 paywall leaks where skills published
without ``tier:`` frontmatter (or via a harvester) landed as free. The enforcer
demotes anything free that is NOT on FREE_ALLOWLIST and keeps the ``is_free``
boolean in lockstep with ``tier``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Skill

# Load the enforcer module by path (scripts/ is not a package).
_SPEC = importlib.util.spec_from_file_location(
    "enforce_free_allowlist",
    Path(__file__).resolve().parent.parent / "scripts" / "enforce_free_allowlist.py",
)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

FREE_ALLOWLIST = _MOD.FREE_ALLOWLIST


def _mk(db, slug, tier, is_free=None, is_public=True, is_archived=False):
    sk = Skill(
        slug=slug,
        title=slug,
        description="t",
        tier=tier,
        is_free=is_free,
        is_public=is_public,
        is_archived=is_archived,
    )
    db.add(sk)
    return sk


_TEST_SLUGS = [
    "super-memory",
    "recipes-cookbook-reconcile",
    "leaked-paid-skill",
]


@pytest.fixture
def db():
    # This test exercises the real file-engine (app.database.SessionLocal),
    # not conftest's in-memory engine_fixture. On a cold checkout the gitignored
    # sqlite file (test_dev.db) has no schema, so the `skills` table may not
    # exist yet — create it idempotently here so the suite is collection-order
    # independent. (Pre-existing latent bug surfaced by a fresh worktree.)
    from app.database import engine
    from app.models import Base

    Base.metadata.create_all(bind=engine)

    s = SessionLocal()
    # Pre-clean any rows a prior (committed) run left behind — main() commits.
    s.query(Skill).filter(Skill.slug.in_(_TEST_SLUGS)).delete(synchronize_session=False)
    s.commit()
    yield s
    s.rollback()
    s.query(Skill).filter(Skill.slug.in_(_TEST_SLUGS)).delete(synchronize_session=False)
    s.commit()
    s.close()


def test_allowlist_has_exactly_the_two_seeds():
    assert FREE_ALLOWLIST == {"super-memory", "recipes-cookbook-reconcile"}


def test_leaked_free_skill_is_demoted(db, monkeypatch):
    # Arrange: two allowlisted seeds (free) + a leaked non-allowlisted free skill.
    _mk(db, "super-memory", "free", is_free=True)
    _mk(db, "recipes-cookbook-reconcile", "free", is_free=True)
    leaked = _mk(db, "leaked-paid-skill", "free", is_free=True)
    db.flush()

    monkeypatch.setattr(_MOD, "SessionLocal", lambda: db)
    monkeypatch.setattr(_MOD.sys, "argv", ["enforce_free_allowlist.py"])
    # Don't let the helper close our fixture session.
    monkeypatch.setattr(db, "close", lambda: None)

    rc = _MOD.main()

    assert rc == 0
    db.refresh(leaked)
    assert leaked.tier == "pro"
    assert leaked.is_free is False


def test_seed_is_protected_and_isfree_reconciled(db, monkeypatch):
    _mk(db, "super-memory", "free", is_free=None)  # drifted is_free
    _mk(db, "recipes-cookbook-reconcile", "free", is_free=True)
    db.flush()

    monkeypatch.setattr(_MOD, "SessionLocal", lambda: db)
    monkeypatch.setattr(_MOD.sys, "argv", ["enforce_free_allowlist.py"])
    monkeypatch.setattr(db, "close", lambda: None)

    rc = _MOD.main()

    assert rc == 0
    seed = db.query(Skill).filter(Skill.slug == "super-memory").first()
    assert seed.tier == "free"
    assert seed.is_free is True  # reconciled from None


def test_missing_seed_aborts(db, monkeypatch):
    # Only one of the two seeds present -> hard invariant break -> exit 2.
    _mk(db, "super-memory", "free", is_free=True)
    db.flush()

    monkeypatch.setattr(_MOD, "SessionLocal", lambda: db)
    monkeypatch.setattr(_MOD.sys, "argv", ["enforce_free_allowlist.py", "--dry-run"])
    monkeypatch.setattr(db, "close", lambda: None)

    rc = _MOD.main()
    assert rc == 2

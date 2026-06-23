"""Phase 1 — self-host smoke test (TDD: RED → GREEN).

Zero-config SQLite boot verification:
  1. /healthz returns HTTP 200.
  2. seed_starter_catalog() seeds ≥1 of each catalog type:
     skill · bundle · loop · personality.

This test is the DoD gatekeeper for Phase 1. It fails RED until
scripts/seed_starter_catalog.py exists and the starter catalog is seeded.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from uuid import uuid4

from app.models import Cookbook, Loop, Personality, Skill
from scripts.seed_starter_catalog import seed_starter_catalog
from tests._app_factory import build_test_app


# ── /healthz smoke ────────────────────────────────────────────────────────────


def test_healthz_returns_200(db_session, monkeypatch):
    """SQLite-backed app must respond 200 on /api/healthz with no secrets/config."""
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/api/healthz")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


# ── starter catalog seed ──────────────────────────────────────────────────────


def _add_seed_skill(db, slug: str = "smoke-test-skill") -> Skill:
    """Insert one public Skill row so bundle-attachment tests have a real target."""
    skill = Skill(
        id=uuid4(),
        slug=slug,
        title="Smoke Test Skill",
        description="Created by the selfhost smoke test fixture.",
        category="testing",
        is_public=True,
    )
    db.add(skill)
    db.flush()
    return skill


@pytest.fixture()
def seeded(db_session):
    """Populate the in-memory test DB with a starter catalog, return the session."""
    _add_seed_skill(db_session)
    seed_starter_catalog(db_session)
    return db_session


def test_seed_produces_at_least_one_skill(seeded):
    count = seeded.query(Skill).filter(Skill.is_public.is_(True)).count()
    assert count >= 1, f"Expected ≥1 public skill after seed, got {count}"


def test_seed_produces_at_least_one_bundle(seeded):
    count = seeded.query(Cookbook).count()
    assert count >= 1, f"Expected ≥1 bundle (Cookbook) after seed, got {count}"


def test_seed_produces_at_least_one_loop(seeded):
    count = seeded.query(Loop).filter(Loop.is_public.is_(True)).count()
    assert count >= 1, f"Expected ≥1 public loop after seed, got {count}"


def test_seed_produces_at_least_one_personality(seeded):
    count = seeded.query(Personality).filter(Personality.is_public.is_(True)).count()
    assert count >= 1, f"Expected ≥1 public personality after seed, got {count}"


def test_seed_is_idempotent(db_session):
    """Running seed twice must not double-insert rows."""
    seed_starter_catalog(db_session)
    c1 = db_session.query(Loop).count()
    seed_starter_catalog(db_session)
    c2 = db_session.query(Loop).count()
    assert c1 == c2, f"Second seed run changed loop count: {c1} → {c2}"

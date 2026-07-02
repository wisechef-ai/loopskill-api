"""Tests for GET /api/carousel/today and GET /api/carousel/{date}.

Covers:
  - happy path: entries returned in correct wire format
  - date param validation: invalid format → 422
  - missing data: no entries for date → 404
"""
from __future__ import annotations

from datetime import datetime, timezone, date
from uuid import uuid4

import pytest

from app.models import CarouselEntry, Skill


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_skill(db, slug="carousel-skill", title="Carousel Skill", category="devops"):
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=title,
        description="A carousel test skill with a good description.",
        category=category,
        tier="operator",
        is_public=True,
        install_count=100,
        rating_avg=4.0,
    )
    db.add(s)
    db.flush()
    return s


def _make_entry(db, skill: Skill, featured_date: datetime, slot: int = 1):
    entry = CarouselEntry(
        id=uuid4(),
        skill_id=skill.id,
        featured_date=featured_date,
        position=slot - 1,
        slot=slot,
        role="new-capability",
        tagline="Test tagline",
        score=7.5,
    )
    db.add(entry)
    db.flush()
    return entry


def _today_utc() -> datetime:
    t = datetime.now(timezone.utc)
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


# ── Endpoint tests ─────────────────────────────────────────────────────────

class TestCarouselToday:
    def test_happy_path_returns_entries(self, client, db_session):
        skill = _make_skill(db_session)
        _make_entry(db_session, skill, _today_utc(), slot=1)
        db_session.commit()

        resp = client.get("/api/carousel/today")
        assert resp.status_code == 200
        data = resp.json()
        assert "date" in data
        assert "entries" in data
        assert len(data["entries"]) >= 1

    def test_response_shape_matches_contract(self, client, db_session):
        """Wire format check — every entry has slot, skill, role, tagline, score."""
        skill = _make_skill(db_session, slug="shape-skill")
        _make_entry(db_session, skill, _today_utc(), slot=1)
        db_session.commit()

        resp = client.get("/api/carousel/today")
        assert resp.status_code == 200
        entry = resp.json()["entries"][0]
        assert "slot" in entry
        assert "skill" in entry
        assert "role" in entry
        assert "tagline" in entry
        assert "score" in entry
        assert "slug" in entry["skill"]
        assert "title" in entry["skill"]

    def test_skill_brief_fields(self, client, db_session):
        """skill object includes category, tier, is_free, vertical."""
        from app.models import Skill
        s = Skill(
            id=uuid4(),
            slug="brief-check",
            title="Brief Check Skill",
            category="seo",
            tier="cook",
            is_public=True,
            is_free=True,
            vertical="agency",
        )
        db_session.add(s)
        db_session.flush()
        _make_entry(db_session, s, _today_utc(), slot=1)
        db_session.commit()

        resp = client.get("/api/carousel/today")
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        skill_briefs = {e["skill"]["slug"]: e["skill"] for e in entries}
        assert "brief-check" in skill_briefs
        brief = skill_briefs["brief-check"]
        assert brief["category"] == "seo"
        assert brief["tier"] == "cook"
        assert brief["is_free"] is True
        assert brief["vertical"] == "agency"

    def test_today_returns_404_when_no_entries(self, client, db_session):
        """No entries in DB → 404 (F11: SAVEPOINT isolation guarantees clean state)."""
        # With SAVEPOINT isolation, this test always has a fresh empty DB
        # (no entries inserted by this test), so we expect exactly 404.
        resp = client.get("/api/carousel/today")
        assert resp.status_code == 404


class TestCarouselByDate:
    def test_happy_path_specific_date(self, client, db_session):
        featured = datetime(2026, 3, 15, 0, 0, 0, tzinfo=timezone.utc)
        skill = _make_skill(db_session, slug="march-skill")
        _make_entry(db_session, skill, featured, slot=1)
        db_session.commit()

        resp = client.get("/api/carousel/2026-03-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["date"] == "2026-03-15"
        assert len(data["entries"]) == 1
        assert data["entries"][0]["skill"]["slug"] == "march-skill"

    def test_404_for_date_with_no_entries(self, client, db_session):
        resp = client.get("/api/carousel/1999-01-01")
        assert resp.status_code == 404

    def test_invalid_date_format_returns_422(self, client, db_session):
        """Non-YYYY-MM-DD strings must be rejected with 422."""
        resp = client.get("/api/carousel/not-a-date")
        assert resp.status_code == 422

    def test_path_traversal_rejected(self, client, db_session):
        """Attempts like ../../../etc/passwd must be rejected."""
        resp = client.get("/api/carousel/../../../etc/passwd")
        # FastAPI URL parsing normalises this; the date pattern won't match
        assert resp.status_code in (422, 404)

    def test_malformed_date_digits_rejected(self, client, db_session):
        """Only valid calendar dates allowed — 2026-13-99 fails."""
        resp = client.get("/api/carousel/2026-13-99")
        assert resp.status_code == 422

    def test_slot_ordering(self, client, db_session):
        """Entries should be returned ordered by slot ascending."""
        featured = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(3):
            s = _make_skill(db_session, slug=f"order-skill-{i}")
            _make_entry(db_session, s, featured, slot=3 - i)  # insert in reverse
        db_session.commit()

        resp = client.get("/api/carousel/2026-05-01")
        assert resp.status_code == 200
        slots = [e["slot"] for e in resp.json()["entries"]]
        assert slots == sorted(slots)

    def test_date_in_response_matches_request(self, client, db_session):
        featured = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
        skill = _make_skill(db_session, slug="june-skill")
        _make_entry(db_session, skill, featured, slot=1)
        db_session.commit()

        resp = client.get("/api/carousel/2026-06-10")
        assert resp.status_code == 200
        assert resp.json()["date"] == "2026-06-10"


class TestCarouselPublicAccess:
    """F4 regression: carousel endpoints must be publicly accessible without API key."""

    def test_carousel_today_no_api_key_not_401(self, db_session):
        """GET /api/carousel/today without API key must NOT return 401 (F4)."""
        from app.config import settings
        from app.carousel.routes import router as carousel_router
        from app.database import get_db
        from app.middleware import APIKeyMiddleware
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        test_app = FastAPI()
        test_app.add_middleware(APIKeyMiddleware)

        def override_get_db():
            yield db_session

        test_app.include_router(carousel_router, prefix="/api")
        test_app.dependency_overrides[get_db] = override_get_db

        # No API key header
        with TestClient(test_app, raise_server_exceptions=True) as anon_client:
            resp = anon_client.get("/api/carousel/today")
        # Must be 200 or 404 (no entries), never 401
        assert resp.status_code != 401, (
            f"Carousel endpoint blocked anonymous access with 401 — F4 regression"
        )
        assert resp.status_code in (200, 404)

    def test_carousel_by_date_no_api_key_not_401(self, db_session):
        """GET /api/carousel/{date} without API key must NOT return 401 (F4)."""
        from app.carousel.routes import router as carousel_router
        from app.database import get_db
        from app.middleware import APIKeyMiddleware
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        test_app = FastAPI()
        test_app.add_middleware(APIKeyMiddleware)

        def override_get_db():
            yield db_session

        test_app.include_router(carousel_router, prefix="/api")
        test_app.dependency_overrides[get_db] = override_get_db

        with TestClient(test_app, raise_server_exceptions=True) as anon_client:
            resp = anon_client.get("/api/carousel/1999-01-01")
        assert resp.status_code != 401, (
            f"Carousel by-date endpoint blocked anonymous access with 401 — F4 regression"
        )
        assert resp.status_code in (200, 404)

"""polish_1805 item 1 — free-skill install must NOT require an x-api-key.

This is the Smithery-parity test. Anonymous GETs to ``/api/skills/install``
with ``slug=<free-tier-skill>`` must return the install payload without
authentication. Pro / Pro+ slugs must still 401.

Security invariants pinned:
1. tier=free public skill → 200 OK without ``x-api-key`` header.
2. tier=cook (paid) public skill → 401 without header.
3. private skill → 401/404 without header, regardless of tier (anonymous callers
   must NEVER trip the ``api_key_user_id is None`` admin codepath).
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app_with_middleware(db_session):
    """Build a minimal FastAPI with the real APIKeyMiddleware wired in.

    The middleware calls ``SessionLocal()`` directly to look up the tier of
    the requested skill (free vs paid gating). We monkey-patch that import
    to return the test session instead, so the middleware sees the same
    rows the test created.
    """
    from app.middleware import APIKeyMiddleware
    from app.routes import router as core_router

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)
    app.include_router(core_router)

    from app.database import get_db

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return app


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    """TestClient with the real middleware AND middleware-side SessionLocal patched."""
    # Patch the SessionLocal that the middleware imports inline. The middleware
    # imports ``from app.database import SessionLocal`` at request time, so we
    # replace it with a factory that returns the test session (without closing it).

    class _TestSessionFactory:
        def __call__(self):
            class _Wrap:
                def query(self_inner, *a, **kw):
                    return db_session.query(*a, **kw)
                def close(self_inner):
                    pass
            return _Wrap()

    import app.database as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal", _TestSessionFactory(), raising=True)

    app = _make_app_with_middleware(db_session)
    return TestClient(app)


def _seed_skill(db, *, slug: str, tier: str, is_public: bool = True):
    """Create a minimal Skill row with one version so /install can resolve it."""
    from app.models import Skill, SkillVersion
    from datetime import datetime, timezone
    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug.replace("-", " ").title(),
        description=f"Test skill {slug}",
        tier=tier,
        is_public=is_public,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=sk.id,
        semver="1.0.0",
        tarball_size_bytes=1024,
        checksum_sha256="deadbeef" * 8,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return sk


def test_free_skill_install_no_auth_returns_non_401(middleware_client, db_session):
    """polish_1805 item 1 — tier=free skills install without any x-api-key header."""
    _seed_skill(db_session, slug="free-install-probe", tier="free", is_public=True)

    resp = middleware_client.get("/api/skills/install?slug=free-install-probe")

    assert resp.status_code != 401, (
        f"Free skills MUST be installable without auth — got {resp.status_code}: {resp.text[:200]}"
    )


def test_pro_skill_install_no_auth_returns_401(middleware_client, db_session):
    """Paid (cook tier) skills MUST still require auth — no free-rider attack."""
    _seed_skill(db_session, slug="paid-install-probe", tier="cook", is_public=True)

    resp = middleware_client.get("/api/skills/install?slug=paid-install-probe")

    assert resp.status_code == 401, (
        f"Pro skills MUST 401 without auth, got {resp.status_code}: {resp.text[:200]}"
    )


def test_private_skill_install_no_auth_does_not_leak(middleware_client, db_session):
    """Critical security check — anonymous callers must NOT be treated as admin.

    Before polish_1805 item 1, ``api_key_user_id is None`` meant "master/admin
    key". Adding the anonymous-free-install path uses the same None sentinel,
    which would have granted anonymous callers admin rights to install private
    skills. The route now checks ``is_anonymous_free_install`` and excludes
    anonymous callers from the admin branch. This test pins that.
    """
    # Make the skill PRIVATE but FREE — worst-case overlap.
    _seed_skill(
        db_session,
        slug="private-free-skill-probe",
        tier="free",
        is_public=False,
    )

    resp = middleware_client.get("/api/skills/install?slug=private-free-skill-probe")

    # Either 401 (middleware rejects) or 404 (route refuses to leak existence) — but
    # NEVER 200. Critical security property.
    assert resp.status_code != 200, (
        f"PRIVATE skill leaked to anonymous caller! Got {resp.status_code}: {resp.text[:200]}"
    )
    assert resp.status_code in (401, 404)


def test_search_endpoint_remains_public(middleware_client, db_session):
    """Regression: polish_1805 must not affect the existing public /search endpoint."""
    resp = middleware_client.get("/api/skills/search?page_size=5")
    assert resp.status_code == 200


def test_unknown_slug_install_no_auth_returns_404(middleware_client, db_session):
    """Slug that doesn't exist — must 404 (no leak), never 200."""
    resp = middleware_client.get("/api/skills/install?slug=nonexistent-slug-12345")
    # With the route-level enforcement, unknown slugs 404 before reaching the
    # tier check. The critical security property is "never 200".
    assert resp.status_code == 404, (
        f"Unknown slug must 404, got {resp.status_code}: {resp.text[:200]}"
    )

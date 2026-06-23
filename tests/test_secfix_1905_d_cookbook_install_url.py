"""Tests for Issue #27: Cookbook install URL fix.

_make_install_url in bundle_routes.py must now produce a signed
/api/skills/_download?token=... URL that can actually be followed
to get the tarball bytes.

Tests:
  - _make_install_url returns a URL with /api/skills/_download?token=
  - The token is a valid URLSafeTimedSerializer payload containing slug + version_id
  - GET /api/cookbooks/<id>/install with a seeded skill returns
    tarball_url that begins with /api/skills/_download?token=
"""

import pytest
from uuid import uuid4
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Skill, SkillVersion, Cookbook, CookbookSkill, User, APIKey
from app.database import get_db
from app.bundle_routes import router as cookbook_router, _make_install_url
from app.config import settings


@pytest.fixture(scope="module")
def engine_cb():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture(scope="module")
def session_cb(engine_cb):
    SessionLocal = sessionmaker(bind=engine_cb, autocommit=False, autoflush=False)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture(scope="module")
def cb_client(engine_cb, session_cb):
    """Seed a cookbook with one skill+version and return (client, cookbook_id)."""
    from app.routes import router as routes_router
    from starlette.middleware.base import BaseHTTPMiddleware

    user = User(
        id=uuid4(),
        email="cbtest@test.com",
        display_name="CB Test User",
        created_at=datetime.now(timezone.utc),
    )
    session_cb.add(user)
    session_cb.flush()

    api_key = APIKey(
        id=uuid4(),
        key_prefix="rec_cbtest",
        key_hash="cbtest_hash",
        user_id=user.id,
        name="cbtest-key",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    session_cb.add(api_key)

    skill = Skill(
        id=uuid4(),
        slug="cb-test-skill",
        title="CB Test Skill",
        category="devops",
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    )
    session_cb.add(skill)
    session_cb.flush()

    version = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver="1.0.0",
        tarball_path="fake/path/cb-test-skill.tar.gz",
        checksum_sha256="abc123",
        created_at=datetime.now(timezone.utc),
    )
    session_cb.add(version)

    cookbook = Cookbook(
        id=uuid4(),
        name="Test Cookbook",
        bundle_owner=user.id,
        created_at=datetime.now(timezone.utc),
    )
    session_cb.add(cookbook)
    session_cb.flush()

    cs = CookbookSkill(
        bundle_id=cookbook.id,
        skill_id=skill.id,
        source="marketplace",
    )
    session_cb.add(cs)
    session_cb.commit()

    SessionLocal = sessionmaker(bind=engine_cb, autocommit=False, autoflush=False)
    app = FastAPI()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    # Inject master-key auth state so require_cookbook_tier sees None (master)
    class InjectMasterAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = None  # None = master key sentinel
            request.state.api_key_id = None
            request.state.is_cbt_token = False
            return await call_next(request)

    app.add_middleware(InjectMasterAuth)
    app.include_router(cookbook_router)  # router already has /api/cookbooks prefix
    app.include_router(routes_router)
    from app.install_routes import router as install_router  # Phase E: _download moved

    app.include_router(install_router, prefix="/api")  # Phase E: /skills/_download
    app.dependency_overrides[get_db] = override_db

    with TestClient(app, headers={"x-api-key": settings.API_KEY}) as tc:
        yield tc, cookbook.id, version.id, skill.slug


# ── Unit test: _make_install_url returns correct URL ──────────────────────────


def test_make_install_url_produces_download_url():
    """_make_install_url must return a /api/skills/_download?token=... URL."""
    skill_slug = "test-skill"
    version_id = uuid4()
    url = _make_install_url(skill_slug, version_id, "1.0.0")

    assert "/api/skills/_download" in url, f"URL must point to /api/skills/_download, got: {url}"
    assert "token=" in url, f"URL must contain a signed token, got: {url}"
    assert f"/api/skills/{version_id}" not in url, f"Old /tarball URL pattern must not be present, got: {url}"


def test_make_install_url_token_is_valid():
    """The token in the URL must be a valid URLSafeTimedSerializer payload."""
    from itsdangerous import URLSafeTimedSerializer

    skill_slug = "some-skill"
    version_id = uuid4()
    url = _make_install_url(skill_slug, version_id, "2.0.0")

    token = url.split("token=", 1)[1]
    # Phase 3+4: canonical salt is now "loopskill-install" (renamed from
    # "recipes-skill-install"). See test_install_url_salt_consistency.
    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    data = serializer.loads(token, max_age=3600)

    assert data["slug"] == skill_slug
    assert data["version_id"] == str(version_id)
    assert data["mode"] == "install"


def test_cookbook_install_returns_signed_download_urls(cb_client):
    """POST /api/cookbooks/<id>/install returns tarball_url pointing to _download."""
    tc, cookbook_id, version_id, slug = cb_client
    resp = tc.post(f"/api/cookbooks/{cookbook_id}/install")

    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    data = resp.json()

    assert "skills" in data
    assert len(data["skills"]) >= 1

    skill_entry = next((s for s in data["skills"] if s["slug"] == slug), None)
    assert skill_entry is not None

    url = skill_entry.get("tarball_url", "")
    assert "/api/skills/_download" in url, f"tarball_url must point to /api/skills/_download, got: {url}"
    assert "token=" in url

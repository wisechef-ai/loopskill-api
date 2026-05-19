"""tests/test_quality_1705_skill_body_paywall.py — Phase B paywall gates.

3 fixture-user × 1 endpoint = 3 acceptance gates per plan §3 Phase B:
  - Anonymous (no x-api-key) → readme=null, external_resources=null
  - Free user (api-key but subscription_tier=null) → readme=null
  - Pro user (subscription_tier=cook AND status='active') → readme has content

Plus reachability for master key (admin) and tier=pro_plus.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, APIKey, Skill, User
from app.routes import router as api_router
from app.skill_routes import router as skill_router  # Phase E: /skills/{slug} moved


SKILL_README = "# Test Skill\n\n" + ("This is the body content. " * 100)


@pytest.fixture()
def db_engine(tmp_path):
    db_path = tmp_path / "paywall.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def seeded_app(db_engine, monkeypatch):
    """Seed users + api keys for anon, free, pro, pro_plus, master."""
    SessionLocal = sessionmaker(bind=db_engine, future=True)
    app = FastAPI()
    app.include_router(api_router)
    app.include_router(skill_router, prefix="/api")  # Phase E: /skills/{slug}

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db

    now = datetime.now(timezone.utc)
    keys = {}

    with SessionLocal() as session:
        # Skill row
        session.add(
            Skill(
                id=uuid.uuid4(),
                slug="clean-architecture",
                title="Test",
                description="desc",
                readme=SKILL_README,
                is_public=True,
                is_archived=False,
                install_count=0,
            )
        )

        user_specs = [
            ("free", None, None),
            ("pro", "cook", "active"),
            ("pro_plus", "operator", "active"),
        ]
        for label, tier, status in user_specs:
            uid = uuid.uuid4()
            session.add(
                User(
                    id=uid,
                    email=f"{label}@test.local",
                    display_name=label,
                    github_id=int(uuid.uuid4().int) % 9_000_000 + 1_000_000,
                    subscription_tier=tier,
                    subscription_status=status,
                )
            )
            plaintext = f"rec_test_{label}_{uuid.uuid4().hex[:8]}"
            hashed = hashlib.sha256(plaintext.encode()).hexdigest()
            session.add(
                APIKey(
                    id=uuid.uuid4(),
                    user_id=uid,
                    key_hash=hashed,
                    key_prefix=plaintext[:9],
                    is_active=True,
                )
            )
            keys[label] = plaintext
        session.commit()

    from app.config import settings
    monkeypatch.setattr(settings, "API_KEY", "rec_admin_master_xyz_1234", raising=False)
    keys["admin"] = settings.API_KEY

    return app, keys


def test_anonymous_caller_gets_null_body(seeded_app):
    app, _ = seeded_app
    client = TestClient(app)
    resp = client.get("/api/skills/clean-architecture")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["readme"] is None, f"anon should see null body, got {len(body.get('readme') or '')} chars"


def test_free_user_gets_null_body(seeded_app):
    app, keys = seeded_app
    client = TestClient(app)
    resp = client.get("/api/skills/clean-architecture", headers={"x-api-key": keys["free"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["readme"] is None


def test_pro_user_gets_full_body(seeded_app):
    app, keys = seeded_app
    client = TestClient(app)
    resp = client.get("/api/skills/clean-architecture", headers={"x-api-key": keys["pro"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["readme"] is not None, "Pro user should see body"
    assert len(body["readme"]) > 100
    assert "Test Skill" in body["readme"]


def test_pro_plus_user_gets_full_body(seeded_app):
    app, keys = seeded_app
    client = TestClient(app)
    resp = client.get("/api/skills/clean-architecture", headers={"x-api-key": keys["pro_plus"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["readme"] is not None
    assert "Test Skill" in body["readme"]


def test_admin_master_key_gets_full_body(seeded_app):
    app, keys = seeded_app
    client = TestClient(app)
    resp = client.get("/api/skills/clean-architecture", headers={"x-api-key": keys["admin"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["readme"] is not None, "master key bypasses paywall"


def test_invalid_key_gets_null_body(seeded_app):
    app, _ = seeded_app
    client = TestClient(app)
    resp = client.get(
        "/api/skills/clean-architecture",
        headers={"x-api-key": "rec_invalid_fake_key_xyz"},
    )
    assert resp.status_code == 200
    assert resp.json()["readme"] is None


def test_metadata_always_visible(seeded_app):
    """All callers see metadata; only body fields gated."""
    app, _ = seeded_app
    client = TestClient(app)
    resp = client.get("/api/skills/clean-architecture")
    body = resp.json()
    assert body["slug"] == "clean-architecture"
    assert body["title"] == "Test"
    assert body["description"] == "desc"
    assert "install_count_total" in body
    assert "latest_version" in body
    assert body["readme"] is None
    assert body["external_resources"] is None


# ─────────────────────────── RCP-PUB-2026-05-18 ──────────────────────────
# JWT-cookie auth for the body paywall — the portal browser case.
# _resolve_caller_tier() must accept the wr_jwt cookie too, not just
# x-api-key, so that authed Pro/Pro+ users browsing on recipes.wisechef.ai
# can see paywalled SKILL.md bodies. Previously this only worked for agents.
# ─────────────────────────────────────────────────────────────────────────


def _make_jwt(user_obj):
    """Build a signed JWT matching the verify_jwt() contract.

    Uses the production create_jwt helper so the token is signed with the
    same secret + algorithm the API will use to verify it.
    """
    from app.auth import create_jwt
    return create_jwt(user_obj)


def _resolve_user_for(seeded_app, label):
    """Look up the seeded User row under the given label, returning the
    detached ORM object (with id, email, etc. populated)."""
    app, _keys = seeded_app
    from app.database import get_db
    from app.models import User
    db_dep = app.dependency_overrides[get_db]
    db = next(db_dep())
    try:
        u = db.query(User).filter(User.email == f"{label}@test.local").first()
        assert u is not None, f"seeded user {label} not found"
        return u
    finally:
        db.close()


def test_pro_user_jwt_cookie_gets_full_body(seeded_app):
    """Pro user with no x-api-key but a valid wr_jwt cookie must see the body.

    Regression: pre-RCP-PUB-2026-05-18 the API only honored x-api-key, so
    Pro/Pro+ users browsing /skills/<slug> on the static portal saw
    readme=null and got the upsell wall even though they were paid customers.
    """
    app, _ = seeded_app
    client = TestClient(app)
    user = _resolve_user_for(seeded_app, "pro")
    token = _make_jwt(user)
    resp = client.get(
        "/api/skills/clean-architecture",
        cookies={"wr_jwt": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["readme"] is not None, "Pro user with wr_jwt cookie must see SKILL.md body"
    assert "Test Skill" in body["readme"]


def test_pro_user_bearer_token_gets_full_body(seeded_app):
    """Pro user passing JWT in Authorization: Bearer header — SPA pattern."""
    app, _ = seeded_app
    client = TestClient(app)
    user = _resolve_user_for(seeded_app, "pro")
    token = _make_jwt(user)
    resp = client.get(
        "/api/skills/clean-architecture",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["readme"] is not None


def test_invalid_jwt_cookie_does_not_unlock_body(seeded_app):
    """Tampered / expired / random JWT cookies must NOT leak the body."""
    app, _ = seeded_app
    client = TestClient(app)
    resp = client.get(
        "/api/skills/clean-architecture",
        cookies={"wr_jwt": "not.a.valid.jwt"},
    )
    assert resp.status_code == 200
    assert resp.json()["readme"] is None


def test_free_user_jwt_cookie_does_not_unlock_body(seeded_app):
    """A free-tier user's JWT cookie correctly resolves to tier=None — no body."""
    app, _ = seeded_app
    client = TestClient(app)
    user = _resolve_user_for(seeded_app, "free")
    token = _make_jwt(user)
    resp = client.get(
        "/api/skills/clean-architecture",
        cookies={"wr_jwt": token},
    )
    assert resp.status_code == 200
    assert resp.json()["readme"] is None, "free-tier JWT must NOT unlock the body"

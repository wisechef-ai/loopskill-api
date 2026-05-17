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

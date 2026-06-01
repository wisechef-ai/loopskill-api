"""Tests for v7 Phase B cookbook endpoints (app/cookbook_routes.py)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Generator
from uuid import UUID, uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import get_db
from app.models import Base, Cookbook, CookbookSkill, Skill, SkillVersion, User


# ─────────────────────────── Fixtures ───────────────────────────────────

@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ─────────────────────────── Helpers ────────────────────────────────────

def _make_user(db: Session, *, tier: str | None, status: str | None = "active") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(user)
    db.flush()
    return user


def _make_skill(db: Session, slug: str = "src-skill", with_version: bool = False) -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=f"Skill {slug}",
        description="x",
        is_public=True,
    )
    db.add(s)
    db.flush()
    if with_version:
        v = SkillVersion(
            id=uuid4(),
            skill_id=s.id,
            semver="0.1.0",
            tarball_path=f"/tmp/{slug}.tar.gz",
            tarball_size_bytes=42,
            checksum_sha256="a" * 64,
        )
        db.add(v)
        db.flush()
    return s


def _make_app(db: Session, *, api_key_user_id, is_admin: bool = False) -> FastAPI:
    from app.cookbook_routes import router as cookbook_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    _uid = api_key_user_id

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = None if is_admin else _uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)
    return app


# ─────────────────────────── Tier gates ─────────────────────────────────

class TestTierGates:
    def test_free_tier_blocked_with_401(self, db_session):
        user = _make_user(db_session, tier="free")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post("/api/cookbooks", json={"name": "Mine"})
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["needs_tier"] == "pro"

    def test_no_tier_blocked_with_401(self, db_session):
        user = _make_user(db_session, tier=None, status=None)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post("/api/cookbooks", json={"name": "Mine"})
        assert r.status_code == 401

    def test_pro_tier_can_create_first(self, db_session):
        user = _make_user(db_session, tier="pro")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post("/api/cookbooks", json={"name": "Cook's Book"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "Cook's Book"
        assert body["cookbook_owner"] == str(user.id)
        assert body["is_base"] is False

    def test_pro_tier_allows_up_to_ten(self, db_session):
        """Pro cap is 10 (loopclose_3005 SSOT). Cookbooks 1-10 succeed."""
        user = _make_user(db_session, tier="pro")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            for i in range(10):
                r = client.post("/api/cookbooks", json={"name": f"CB{i}"})
                assert r.status_code == 201, f"cookbook {i + 1} should succeed: {r.text}"

    def test_pro_tier_eleventh_cookbook_blocked_with_403(self, db_session):
        """The 11th Pro cookbook is rejected with max_cookbooks=10 (SSOT)."""
        user = _make_user(db_session, tier="pro")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            for i in range(10):
                assert client.post("/api/cookbooks", json={"name": f"CB{i}"}).status_code == 201
            r11 = client.post("/api/cookbooks", json={"name": "Eleventh"})
        assert r11.status_code == 403
        detail = r11.json()["detail"]
        assert detail["reason"] == "pro_tier_limit"
        assert detail["max_cookbooks"] == 10

    def test_cook_legacy_alias_shares_pro_cap_of_ten(self, db_session):
        """Legacy 'cook' slug resolves to Pro → same 10 cap (403 on 11th)."""
        user = _make_user(db_session, tier="cook")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            for i in range(10):
                assert client.post("/api/cookbooks", json={"name": f"CB{i}"}).status_code == 201
            r11 = client.post("/api/cookbooks", json={"name": "Eleventh"})
        assert r11.status_code == 403
        assert r11.json()["detail"]["max_cookbooks"] == 10

    def test_pro_plus_capped_at_two_hundred(self, db_session):
        """Pro+ cap is 200 (loopclose_3005 SSOT) — NOT unlimited.

        Seed 200 cookbooks directly (fast), then assert the 201st is rejected
        with max_cookbooks=200, and that the 200th would still be allowed.
        """
        user = _make_user(db_session, tier="pro_plus")
        # Seed 199 cookbooks directly so the next POST is the 200th (allowed)
        # and the one after is the 201st (blocked).
        for i in range(199):
            db_session.add(Cookbook(id=uuid4(), name=f"seed{i}", cookbook_owner=user.id))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r200 = client.post("/api/cookbooks", json={"name": "TwoHundredth"})
            assert r200.status_code == 201, f"200th should succeed: {r200.text}"
            r201 = client.post("/api/cookbooks", json={"name": "TwoOhOne"})
        assert r201.status_code == 403
        detail = r201.json()["detail"]
        assert detail["reason"] == "pro_tier_limit"
        assert detail["max_cookbooks"] == 200


# ─────────────────────────── List / detail ──────────────────────────────

class TestListDetail:
    def test_list_only_returns_mine(self, db_session):
        mine = _make_user(db_session, tier="pro_plus")
        other = _make_user(db_session, tier="pro_plus")
        cb_mine = Cookbook(id=uuid4(), name="Mine", cookbook_owner=mine.id)
        cb_other = Cookbook(id=uuid4(), name="Other", cookbook_owner=other.id)
        db_session.add_all([cb_mine, cb_other])
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=mine.id)
        with TestClient(app) as client:
            r = client.get("/api/cookbooks")
        assert r.status_code == 200
        ids = {c["id"] for c in r.json()["cookbooks"]}
        assert str(cb_mine.id) in ids
        assert str(cb_other.id) not in ids

    def test_get_detail_includes_skills(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        skill = _make_skill(db_session, slug="alpha")
        db_session.add(cb)
        db_session.flush()
        db_session.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Mine"
        assert len(body["skills"]) == 1
        assert body["skills"][0]["slug"] == "alpha"
        assert body["skills"][0]["source"] == "custom-added"

    def test_get_other_users_cookbook_returns_404(self, db_session):
        owner = _make_user(db_session, tier="pro_plus")
        intruder = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Private", cookbook_owner=owner.id)
        db_session.add(cb)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=intruder.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}")
        assert r.status_code == 404


# ─────────────────────────── Skill add/remove ───────────────────────────

class TestAddRemoveSkill:
    def test_add_skill_succeeds(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        _make_skill(db_session, slug="beta")
        db_session.add(cb)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/skills", json={"slug": "beta"})
        assert r.status_code == 201, r.text
        assert r.json()["slug"] == "beta"
        assert r.json()["source"] == "custom-added"

    def test_add_unknown_skill_returns_404(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/skills", json={"slug": "ghost"})
        assert r.status_code == 404

    def test_delete_skill_soft_deletes(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        skill = _make_skill(db_session, slug="gamma")
        db_session.add(cb)
        db_session.flush()
        db_session.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.delete(f"/api/cookbooks/{cb.id}/skills/gamma")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

        # Row remains, source flipped to disabled
        db_session.expire_all()
        cs = (
            db_session.query(CookbookSkill)
            .filter(CookbookSkill.cookbook_id == cb.id, CookbookSkill.skill_id == skill.id)
            .first()
        )
        assert cs is not None
        assert cs.source == "disabled"


# ─────────────────────────── Manifest ───────────────────────────────────

class TestManifest:
    def test_manifest_yaml_roundtrip(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Manifest CB", description="My desc",
                      cookbook_owner=user.id)
        skill = _make_skill(db_session, slug="delta")
        db_session.add(cb)
        db_session.flush()
        db_session.add(CookbookSkill(
            cookbook_id=cb.id, skill_id=skill.id,
            source="custom-added", pinned_version="1.2.3",
        ))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}/manifest")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-yaml")
        parsed = yaml.safe_load(r.text)
        assert parsed["name"] == "Manifest CB"
        assert parsed["description"] == "My desc"
        assert parsed["skills"][0]["slug"] == "delta"
        assert parsed["skills"][0]["pinned_version"] == "1.2.3"
        assert parsed["skills"][0]["source"] == "custom-added"


# ─────────────────────────── Install ────────────────────────────────────

class TestInstall:
    def test_install_idempotent(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        skill = _make_skill(db_session, slug="epsilon", with_version=True)
        db_session.add(cb)
        db_session.flush()
        db_session.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r1 = client.post(f"/api/cookbooks/{cb.id}/install")
            r2 = client.post(f"/api/cookbooks/{cb.id}/install")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json() == r2.json()
        body = r1.json()
        assert body["cookbook_id"] == str(cb.id)
        assert len(body["skills"]) == 1
        assert body["skills"][0]["slug"] == "epsilon"
        assert body["skills"][0]["version"] == "0.1.0"
        assert body["skills"][0]["tarball_url"]

    def test_install_skips_disabled(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        kept = _make_skill(db_session, slug="kept", with_version=True)
        gone = _make_skill(db_session, slug="gone", with_version=True)
        db_session.add(cb)
        db_session.flush()
        db_session.add_all([
            CookbookSkill(cookbook_id=cb.id, skill_id=kept.id, source="custom-added"),
            CookbookSkill(cookbook_id=cb.id, skill_id=gone.id, source="disabled"),
        ])
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/install")
        slugs = [s["slug"] for s in r.json()["skills"]]
        assert "kept" in slugs
        assert "gone" not in slugs


# ─────────────────────────── Sync ───────────────────────────────────────

class TestSync:
    def test_sync_since_filter(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        s1 = _make_skill(db_session, slug="t1")
        s2 = _make_skill(db_session, slug="t2")
        s3 = _make_skill(db_session, slug="t3")
        db_session.add(cb)
        db_session.flush()

        # Insert with three distinct timestamps.
        base = datetime(2026, 1, 1, 12, 0, 0)
        db_session.add(CookbookSkill(
            cookbook_id=cb.id, skill_id=s1.id, source="custom-added",
            added_at=base,
        ))
        db_session.add(CookbookSkill(
            cookbook_id=cb.id, skill_id=s2.id, source="custom-added",
            added_at=base + timedelta(hours=1),
        ))
        db_session.add(CookbookSkill(
            cookbook_id=cb.id, skill_id=s3.id, source="custom-added",
            added_at=base + timedelta(hours=2),
        ))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            since = (base + timedelta(minutes=30)).isoformat()
            r = client.get(f"/api/cookbooks/{cb.id}/sync", params={"since": since})
        assert r.status_code == 200, r.text
        body = r.json()
        slugs = {evt["slug"] for evt in body["added"]}
        assert "t1" not in slugs
        assert "t2" in slugs
        assert "t3" in slugs

    def test_sync_partitions_by_source(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        a = _make_skill(db_session, slug="add-me")
        u = _make_skill(db_session, slug="update-me")
        r_skill = _make_skill(db_session, slug="remove-me")
        db_session.add(cb)
        db_session.flush()
        db_session.add_all([
            CookbookSkill(cookbook_id=cb.id, skill_id=a.id, source="custom-added"),
            CookbookSkill(cookbook_id=cb.id, skill_id=u.id, source="overridden", pinned_version="2.0.0"),
            CookbookSkill(cookbook_id=cb.id, skill_id=r_skill.id, source="disabled"),
        ])
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}/sync")
        body = r.json()
        assert {e["slug"] for e in body["added"]} == {"add-me"}
        assert {e["slug"] for e in body["updated"]} == {"update-me"}
        assert {e["slug"] for e in body["removed"]} == {"remove-me"}

    def test_sync_invalid_since_returns_422(self, db_session):
        user = _make_user(db_session, tier="pro_plus")
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.get(f"/api/cookbooks/{cb.id}/sync", params={"since": "not-a-date"})
        assert r.status_code == 422

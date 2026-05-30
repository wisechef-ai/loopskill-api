"""Tests for Phase E (Studio buckets + white-label).

Coverage:
  - tier gates: Cook / Operator → 402, Studio → 201
  - add skill to bucket
  - add fork to bucket (mocked existence — fork tables live on sibling branch)
  - apply bucket → install_events get bucket_id annotation
  - manifest endpoint is public (no auth required)
  - BucketHostMiddleware: scoped catalog response on custom_domain hit
  - bucket_loader strips `_`-prefixed comment keys
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Bucket, BucketSkill, InstallEvent, Skill, User


# ── DB fixtures ──────────────────────────────────────────────────────────


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
def db(engine_fixture) -> Generator[Session, None, None]:
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


# ── Test users (one per tier) ────────────────────────────────────────────


def _make_user(db: Session, tier: str | None) -> User:
    user = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"e6-{tier or 'anon'}-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name=f"E6 {tier or 'anon'} user",
        subscription_tier=tier,
        subscription_status="active" if tier else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── Test app builder ─────────────────────────────────────────────────────


def _build_app(db: Session, user: User | None) -> FastAPI:
    """Build a minimal FastAPI app with just the buckets router and an
    overridden auth dependency that returns ``user`` (or None)."""
    from app import auth_routes
    from app.buckets_routes import router as buckets_router

    app = FastAPI()

    def _override_db():
        try:
            yield db
        finally:
            pass

    def _override_user():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[auth_routes.get_current_user_optional] = _override_user
    app.include_router(buckets_router)
    return app


# ── Tier gate tests ──────────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["free"])
def test_create_bucket_below_pro_returns_402(db, tier):
    """integrator_2905 W1: gate dropped from pro_plus to Pro.
    Free tier still rejected; cook (→pro) and operator (→pro_plus) now accepted.
    """
    user = _make_user(db, tier)
    client = TestClient(_build_app(db, user))
    resp = client.post("/api/buckets/create", json={"name": "Try", "visibility": "private"})
    assert resp.status_code == 402
    assert "studio_tier_required" in resp.json()["detail"]


def test_create_bucket_anonymous_returns_401(db):
    client = TestClient(_build_app(db, None))
    resp = client.post("/api/buckets/create", json={"name": "Try", "visibility": "private"})
    assert resp.status_code == 401


def test_create_bucket_pro_tier_returns_200(db):
    """integrator_2905 W1: Pro tier now accepted for bucket creation."""
    user = _make_user(db, "pro")
    client = TestClient(_build_app(db, user))
    resp = client.post("/api/buckets/create", json={
        "name": "My Pro Bucket",
        "description": "Test",
        "visibility": "private",
        "pin_mode": "latest-stable",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "created"
    assert body["bucket"]["slug"].startswith("my-pro-bucket")
    assert body["bucket"]["owner_id"] == str(user.id)


# ── Add skill / fork to bucket ───────────────────────────────────────────


def _create_bucket(client, name="My Bucket"):
    resp = client.post("/api/buckets/create", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["bucket"]


def test_add_skill_to_bucket(db):
    user = _make_user(db, "studio")
    skill = Skill(id=uuid.uuid4(), slug="adder-skill", title="Adder", is_public=True)
    db.add(skill)
    db.commit()

    client = TestClient(_build_app(db, user))
    bucket = _create_bucket(client)

    resp = client.post(
        f"/api/buckets/{bucket['id']}/skills/add",
        json={"skill_id": str(skill.id), "version_pin": "1.0.0", "install_order": 50},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "added"
    assert body["skill_id"] == str(skill.id)
    assert body["version_pin"] == "1.0.0"
    assert body["install_order"] == 50

    rows = db.query(BucketSkill).filter(BucketSkill.bucket_id == uuid.UUID(bucket["id"])).all()
    assert len(rows) == 1


def test_add_fork_to_bucket(db):
    """Adding a fork — fork_id existence is not enforced server-side because
    the skill_forks table is owned by the sibling branch. The API accepts
    any UUID; DB FK enforcement kicks in once both branches merge."""
    user = _make_user(db, "studio")
    client = TestClient(_build_app(db, user))
    bucket = _create_bucket(client)

    fake_fork_id = str(uuid.uuid4())
    resp = client.post(
        f"/api/buckets/{bucket['id']}/skills/add",
        json={"fork_id": fake_fork_id, "version_pin": "0.0.1", "install_order": 20},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "added"
    assert body["fork_id"] == fake_fork_id
    assert body["skill_id"] is None


def test_add_skill_or_fork_required(db):
    user = _make_user(db, "studio")
    client = TestClient(_build_app(db, user))
    bucket = _create_bucket(client)

    resp = client.post(f"/api/buckets/{bucket['id']}/skills/add", json={})
    assert resp.status_code == 400
    assert "skill_id_or_fork_id_required" in resp.json()["detail"]


# ── Apply bucket → install_events annotated with bucket_id ───────────────


def test_apply_bucket_writes_install_events_with_bucket_annotation(db):
    user = _make_user(db, "studio")
    s1 = Skill(id=uuid.uuid4(), slug="apply-s1", title="One", is_public=True)
    s2 = Skill(id=uuid.uuid4(), slug="apply-s2", title="Two", is_public=True)
    db.add_all([s1, s2])
    db.commit()

    client = TestClient(_build_app(db, user))
    bucket = _create_bucket(client, name="apply test")

    for sk in (s1, s2):
        client.post(f"/api/buckets/{bucket['id']}/skills/add",
                    json={"skill_id": str(sk.id), "version_pin": "latest"})

    resp = client.post(f"/api/buckets/{bucket['id']}/apply")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applying"
    assert body["skills"] == 2
    assert "job_id" in body

    expected_annotation = f"bucket:{bucket['id']}"
    events = db.query(InstallEvent).filter(InstallEvent.client_ip == expected_annotation).all()
    assert len(events) == 2
    assert {e.skill_slug for e in events} == {"apply-s1", "apply-s2"}


# ── Manifest is public (no auth) ─────────────────────────────────────────


def test_manifest_endpoint_is_public(db):
    """Manifest must work without an authenticated user — the route is the
    shareable URL embedded in white-label sites."""
    studio = _make_user(db, "studio")
    skill = Skill(id=uuid.uuid4(), slug="public-s1", title="Public Skill", is_public=True)
    db.add(skill)
    db.commit()

    # Create the bucket as the owner, then issue the manifest call as anon.
    owner_client = TestClient(_build_app(db, studio))
    bucket = _create_bucket(owner_client, name="Public Stack")

    # Public manifest requires non-private visibility; flip via DB so we
    # don't depend on an "update bucket" endpoint that isn't part of E.2.
    db_bucket = db.query(Bucket).filter(Bucket.id == uuid.UUID(bucket["id"])).first()
    db_bucket.visibility = "public"
    db.commit()

    owner_client.post(
        f"/api/buckets/{bucket['id']}/skills/add",
        json={"skill_id": str(skill.id), "version_pin": "latest"},
    )

    anon_client = TestClient(_build_app(db, None))
    resp = anon_client.get(f"/api/buckets/{bucket['slug']}/manifest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bucket"]["slug"] == bucket["slug"]
    assert len(body["skills"]) == 1
    assert body["skills"][0]["skill"]["slug"] == "public-s1"


def test_manifest_404_for_private_bucket(db):
    user = _make_user(db, "studio")
    client = TestClient(_build_app(db, user))
    bucket = _create_bucket(client, name="Locked")
    anon = TestClient(_build_app(db, None))
    resp = anon.get(f"/api/buckets/{bucket['slug']}/manifest")
    assert resp.status_code == 404


# ── BucketHostMiddleware ────────────────────────────────────────────────


def _patch_session_local(monkeypatch, db):
    """Replace `app.database.SessionLocal` (and `app.middleware.SessionLocal`,
    if the middleware imported it at module load) so calling it returns our
    test session. The `close()` is a no-op so the SAVEPOINT-based per-test
    session stays alive for assertions.
    """
    class _FakeSession:
        def __init__(self, real):
            self._real = real

        def query(self, *a, **kw):
            return self._real.query(*a, **kw)

        def close(self):
            pass

    def _factory():
        return _FakeSession(db)

    monkeypatch.setattr("app.database.SessionLocal", _factory, raising=False)
    # Middleware does `from app.database import SessionLocal` inside dispatch,
    # so the patch on app.database is what counts.


def test_bucket_host_middleware_scopes_request(db, monkeypatch):
    """A request whose Host header matches `custom_domain` should land with
    `request.state.bucket_id` populated."""
    from app.middleware import BucketHostMiddleware

    user = _make_user(db, "studio")
    bucket = Bucket(
        id=uuid.uuid4(),
        owner_id=user.id,
        name="ACME",
        slug="acme-stack",
        visibility="public",
        custom_domain="catalog.acme.com",
        is_white_label=True,
    )
    db.add(bucket)
    db.commit()

    _patch_session_local(monkeypatch, db)

    app = FastAPI()
    app.add_middleware(BucketHostMiddleware)
    captured: dict = {}

    @app.get("/scoped")
    def scoped(request: Request):
        captured["bucket_id"] = getattr(request.state, "bucket_id", None)
        captured["bucket_slug"] = getattr(request.state, "bucket_slug", None)
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/scoped", headers={"host": "catalog.acme.com"})
    assert resp.status_code == 200, resp.text
    assert captured["bucket_id"] == str(bucket.id)
    assert captured["bucket_slug"] == "acme-stack"


def test_bucket_host_middleware_no_match(db, monkeypatch):
    """Hosts that don't match a custom_domain leave request.state untouched."""
    from app.middleware import BucketHostMiddleware

    _patch_session_local(monkeypatch, db)

    app = FastAPI()
    app.add_middleware(BucketHostMiddleware)
    captured: dict = {}

    @app.get("/x")
    def x(request: Request):
        captured["bucket_id"] = getattr(request.state, "bucket_id", None)
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/x", headers={"host": "unknown.example.com"})
    assert resp.status_code == 200, resp.text
    assert captured["bucket_id"] is None


# ── bucket_loader strips comment keys ───────────────────────────────────


def test_bucket_loader_strips_underscore_keys(tmp_path):
    from app.bucket_loader import load_bucket_file, strip_comments

    sample = {
        "_comment": "top-level note",
        "name": "Sample",
        "skills": [
            {"_section": "first group", "slug": "a"},
            {"slug": "b"},
        ],
    }

    f = tmp_path / "sample.json"
    import json as _json
    f.write_text(_json.dumps(sample))
    loaded = load_bucket_file(f)
    assert "_comment" not in loaded
    assert loaded["name"] == "Sample"
    assert all("_section" not in s for s in loaded["skills"])
    assert [s["slug"] for s in loaded["skills"]] == ["a", "b"]

    # Also exercise strip_comments directly.
    assert strip_comments({"_x": 1, "y": 2}) == {"y": 2}


def test_wisechef_fleet_v1_loads_with_47_skills():
    """The dogfood bucket file must parse and yield 47 skills, 12 crons, 6 services."""
    from app.bucket_loader import load_bucket_file
    from pathlib import Path

    repo_root = Path(__file__).parent.parent
    data = load_bucket_file(repo_root / "internal" / "buckets" / "wisechef-fleet-v1.json")
    assert data["slug"] == "wisechef-fleet-v1"
    assert len(data["skills"]) == 47
    assert len(data["crons"]) == 12
    assert len(data["services"]) == 6


# ── Preflight aggregator ─────────────────────────────────────────────────


def test_preflight_returns_ok_for_empty_bucket(db):
    from app.bucket_preflight import run_preflight

    user = _make_user(db, "studio")
    bucket = Bucket(
        id=uuid.uuid4(), owner_id=user.id, name="Empty", slug="empty-stack",
        visibility="private",
    )
    db.add(bucket)
    db.commit()

    report = run_preflight(db, "empty-stack", host_fingerprint={"os": "linux", "arch": "x86_64"})
    assert report["ok"] is True
    assert report["skills_inspected"] == 0


def test_preflight_detects_missing_bucket(db):
    from app.bucket_preflight import run_preflight

    report = run_preflight(db, "no-such-bucket")
    assert report["ok"] is False
    assert any("bucket_not_found" in p for p in report["problems"])


def test_preflight_port_conflict_check_on_pure_recipes():
    """Port conflict detection is callable in isolation on a list of recipes."""
    from app.bucket_preflight import check_port_conflicts

    recipes = [
        {"slug": "a", "recipe": {"runtime": {"services": [{"port": 8100}]}}},
        {"slug": "b", "recipe": {"runtime": {"services": [{"port": 8100}]}}},
    ]
    problems = check_port_conflicts(recipes, [])
    assert any("port_conflict" in p and "8100" in p for p in problems)


def test_preflight_env_collision_check_on_pure_recipes():
    from app.bucket_preflight import check_env_collisions

    recipes = [
        {"slug": "a", "recipe": {"runtime": {"env": {"required": ["DB_URL"]}}}},
        {"slug": "b", "recipe": {"runtime": {"env": {"required": ["DB_URL"]}}}},
    ]
    problems = check_env_collisions(recipes, {})
    assert any("env_collision" in p and "DB_URL" in p for p in problems)


def test_preflight_arch_compat_rejects_mismatch():
    from app.bucket_preflight import check_arch_compat

    recipes = [
        {"slug": "linux-only", "recipe": {"runtime": {"compatibility": {"os": ["linux"]}}}},
    ]
    problems = check_arch_compat(recipes, {"os": "darwin", "arch": "arm64"})
    assert any("arch_incompat" in p for p in problems)
    assert check_arch_compat(recipes, {"os": "linux", "arch": "x86_64"}) == []

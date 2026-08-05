"""mesh_0408 Q-031 — bundle-scoped installs mint routable provenance.

Root cause (proven in production, 223 install_events rows, exactly 1 with a
non-NULL bundle_id): GET /api/skills/install hand-rolled its own InstallEvent
insert + counter bump + mint_provenance call, never accepting or stamping a
bundle_id. Every install through the public route was therefore structurally
unroutable through app/services/provenance.py::route_targets_for_provenance,
which keys entirely on InstallEvent.bundle_id.

The fix deletes the hand-rolled path and routes through the existing
canonical app/services/provenance.py::record_install_with_provenance helper
(already used 3x in app/bundle_routes.py), adding an optional VALIDATED
``bundle_id`` query parameter.

This file pins:
  - omitting bundle_id -> unchanged (NULL bundle_id) behaviour, backward compat
  - a valid bundle containing the skill -> InstallEvent.bundle_id stamped,
    resolve_provenance() agrees
  - THE KEY TEST: a bundle bound to a feedback_repo, install through it ->
    route_targets_for_provenance() returns a non-empty target list pointing
    at the bound repo (the rail can now actually route)
  - a bundle that does NOT contain the skill -> rejected, no InstallEvent row
    created
  - malformed uuid -> 422
  - an is_test API key -> InstallEvent recorded but Skill.install_count NOT
    bumped (Ph B §4.2 integrity, now enforced on this route too)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import (
    APIKey,
    Base,
    Bundle,
    BundleSkill,
    InstallEvent,
    Skill,
    SkillVersion,
    User,
)
from app.services.provenance import resolve_provenance, route_targets_for_provenance


# ── fixtures ─────────────────────────────────────────────────────────────


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


def _mk_user(db, tier="pro"):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"u-{uuid.uuid4().hex[:6]}@t.io",
        display_name="u",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_bundle(db, owner, **kw):
    cb = Bundle(
        id=uuid.uuid4(),
        name=kw.pop("name", "CB"),
        bundle_owner=owner.id if owner else None,
        visibility=kw.pop("visibility", "private"),
        **kw,
    )
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db, slug, is_public=True, tier="free"):
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        is_public=is_public,
        tier=tier,
        install_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.commit()
    return s


def _mk_version(db, skill, semver="1.0.0"):
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        semver=semver,
        checksum_sha256="a" * 64,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.commit()
    return v


def _attach(db, cb, skill, source="custom-added"):
    row = BundleSkill(id=uuid.uuid4(), bundle_id=cb.id, skill_id=skill.id, source=source)
    db.add(row)
    db.commit()
    return row


def _mk_apikey(db, *, is_test=False, user=None):
    if user is None:
        user = _mk_user(db)
    k = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_prefix="rec_test",
        key_hash=uuid.uuid4().hex,
        name="k",
        is_test=is_test,
        is_active=True,
    )
    db.add(k)
    db.commit()
    return k


def _build_app(db: Session) -> FastAPI:
    """Minimal app: install_router only, master-key auth (no middleware)."""
    from app.install_routes import router as install_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(install_router, prefix="/api")
    return app


def _client_for(db: Session, *, api_key_id=None) -> TestClient:
    """TestClient that stamps request.state like APIKeyMiddleware would for
    a master/admin key (api_key_user_id=None), optionally with a specific
    api_key_id so the is_test integrity path can be exercised."""
    from starlette.middleware.base import BaseHTTPMiddleware

    app = _build_app(db)

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = None  # master sentinel
            request.state.api_key_id = api_key_id
            request.state.is_anonymous_free_install = False
            request.state.auth_ctx = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    return TestClient(app)


def _patched():
    """Context manager bundle: keep tier/rate-limit resolution hermetic and
    permissive so tests exercise ONLY the bundle-provenance contract."""
    from app import install_routes

    return (
        patch.object(install_routes, "_resolve_caller_tier_for_install", return_value="pro_plus"),
        patch.object(install_routes, "_count_today_installs", return_value=0),
    )


# ── omitting bundle_id -> unchanged behaviour ───────────────────────────


def test_omitting_bundle_id_installs_with_null_bundle(db):
    """Backward compatibility: no bundle_id param -> install still works,
    InstallEvent.bundle_id is NULL, exactly today's (buggy-routing) behaviour."""
    skill = _mk_skill(db, "no-bundle-skill")
    _mk_version(db, skill)
    client = _client_for(db)

    p1, p2 = _patched()
    with p1, p2:
        resp = client.get("/api/skills/install", params={"slug": "no-bundle-skill", "mode": "files"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provenance_id"]

    ev = db.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).first()
    assert ev is not None
    assert ev.bundle_id is None


# ── valid bundle containing the skill -> stamped ────────────────────────


def test_valid_bundle_containing_skill_stamps_bundle_id(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "in-bundle-skill")
    _mk_version(db, skill)
    _attach(db, cb, skill)
    client = _client_for(db)

    p1, p2 = _patched()
    with p1, p2:
        resp = client.get(
            "/api/skills/install",
            params={"slug": "in-bundle-skill", "mode": "files", "bundle_id": str(cb.id)},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    pid = body["provenance_id"]
    assert pid

    ev = db.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).first()
    assert ev is not None
    assert ev.bundle_id == cb.id

    resolved = resolve_provenance(db, pid)
    assert resolved is not None
    assert resolved.bundle_id == cb.id


# ── THE KEY TEST: routing actually resolves via the real function ──────


def test_bundle_with_feedback_repo_routes_via_real_route_targets_for_provenance(db):
    """Proves the rail can now actually route: a bundle bound to a
    feedback_repo, installed through /api/skills/install?bundle_id=...,
    yields a provenance_id that route_targets_for_provenance() resolves to
    a NON-EMPTY target list pointing at the bound repo."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    cb.feedback_repo = "owner/their-private-repo"
    cb.feedback_mode = "pat"
    cb.feedback_pat_enc = "enc-blob"
    db.commit()

    skill = _mk_skill(db, "routable-skill")
    _mk_version(db, skill)
    _attach(db, cb, skill)
    client = _client_for(db)

    p1, p2 = _patched()
    with p1, p2:
        resp = client.get(
            "/api/skills/install",
            params={"slug": "routable-skill", "mode": "files", "bundle_id": str(cb.id)},
        )

    assert resp.status_code == 200, resp.text
    pid = resp.json()["provenance_id"]

    targets = route_targets_for_provenance(db, pid)
    assert len(targets) == 1, f"expected exactly one routing target, got {targets!r}"
    assert targets[0].repo == "owner/their-private-repo"
    assert targets[0].kind == "curator"
    assert targets[0].mode == "pat"


# ── bundle that does NOT contain the skill -> rejected ──────────────────


def test_bundle_not_containing_skill_is_rejected_no_event_created(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)  # empty bundle — no skills attached
    skill = _mk_skill(db, "unrelated-skill")
    _mk_version(db, skill)
    client = _client_for(db)

    before_count = db.query(InstallEvent).count()

    p1, p2 = _patched()
    with p1, p2:
        resp = client.get(
            "/api/skills/install",
            params={"slug": "unrelated-skill", "mode": "files", "bundle_id": str(cb.id)},
        )

    assert resp.status_code in (404, 422), resp.text

    after_count = db.query(InstallEvent).count()
    assert after_count == before_count, "no InstallEvent row must be created on rejection"


# ── malformed uuid -> 422 ────────────────────────────────────────────────


def test_malformed_bundle_id_returns_422(db):
    skill = _mk_skill(db, "malformed-uuid-skill")
    _mk_version(db, skill)
    client = _client_for(db)

    p1, p2 = _patched()
    with p1, p2:
        resp = client.get(
            "/api/skills/install",
            params={"slug": "malformed-uuid-skill", "mode": "files", "bundle_id": "not-a-uuid"},
        )

    assert resp.status_code == 422, resp.text


# ── is_test key: InstallEvent recorded, counter NOT bumped ─────────────


def test_is_test_key_records_event_but_does_not_bump_counter(db):
    """The integrity bug this fix closes: the OLD hand-rolled block bumped
    Skill.install_count unconditionally. record_install_with_provenance only
    bumps for organic (non-test) keys (Ph B §4.2). A test/CI key must still
    record the InstallEvent (audit trail) but must NOT inflate the public
    counter."""
    skill = _mk_skill(db, "test-key-skill")
    _mk_version(db, skill)
    tk = _mk_apikey(db, is_test=True)
    client = _client_for(db, api_key_id=tk.id)

    p1, p2 = _patched()
    with p1, p2:
        resp = client.get("/api/skills/install", params={"slug": "test-key-skill", "mode": "files"})

    assert resp.status_code == 200, resp.text

    db.expire_all()
    refreshed = db.query(Skill).filter(Skill.id == skill.id).first()
    assert refreshed.install_count == 0, "test-key install must NOT bump the public counter"

    ev = db.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).first()
    assert ev is not None, "test-key install must still record the InstallEvent (audit trail)"

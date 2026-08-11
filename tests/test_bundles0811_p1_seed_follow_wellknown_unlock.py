"""bundles0811-P1-follow-seed gate 2 — regression tests for the well-known
surface's federated-skill unlock bugfix (app/bundle_wellknown_routes.py).

THE BUG: every materialized external skill carries ``tier='external'`` — a
value not in ``_FREE_TIERS`` — so ``_is_free`` always returned False for a
federated bundle member. The well-known index (and per-skill SKILL.md route)
therefore flagged EVERY federated member ``locked`` regardless of its actual
resolved license, which broke ``install.sh`` for any bundle assembled purely
from the federated index (reproduced live against the pre-existing
``coreys-marketing`` bundle 2026-08-11: 49/49 skills locked, 0 installed).

THE FIX: ``_is_redistributable_external`` unlocks a materialized external
skill when its descriptor says ``redistributable=True`` AND
``install_path='fetch_origin'`` — the SAME redistribution gate
``app.services.federation.route_install`` already enforced at materialize
time. A deep-link / non-redistributable external skill stays locked, exactly
like a paid internal skill.
"""

from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Bundle, BundleSkill, Skill, User


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


def _app(db: Session) -> FastAPI:
    from app.bundle_wellknown_routes import router as wk_router

    app = FastAPI()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(wk_router)
    return app


def _seed_owner(db: Session) -> User:
    owner = User(id=uuid4(), display_name="O", email=f"{uuid4()}@t.example")
    db.add(owner)
    db.flush()
    return owner


def _make_external_skill(
    db: Session,
    *,
    slug: str,
    redistributable: bool,
    install_path: str = "fetch_origin",
) -> Skill:
    """Build a materialized-external-skill row exactly like
    ``bundle_external.materialize_external_skill`` does (same field set), so
    this test exercises the real descriptor shape instead of a hand-rolled
    approximation.
    """
    skill = Skill(
        id=uuid4(),
        slug=slug,
        title=slug,
        description="A federated skill",
        tier="external",
        is_public=False,
        skill_variant="external",
        original_source_url=f"https://github.com/example/{slug}",
        external_resources={
            "federation_source": "hermes-hub",
            "external_slug": slug,
            "install_path": install_path,
            "origin_url": f"https://github.com/example/{slug}",
            "redistributable": redistributable,
            "attribution": None,
            "scan_status": "pending",
            "scannable": True,
            "scan_findings": [],
            "scan_warnings": [],
        },
    )
    db.add(skill)
    db.flush()
    return skill


def _bundle_with_member(db: Session, member: Skill, *, slug="fed-bundle") -> Bundle:
    owner = _seed_owner(db)
    cb = Bundle(id=uuid4(), name="Federated Bundle", slug=slug, visibility="public", bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=member.id, source="custom-added", install_order=0))
    db.commit()
    return cb


class TestRedistributableExternalUnlocked:
    def test_index_does_not_lock_a_redistributable_external_skill(self, db_session):
        member = _make_external_skill(db_session, slug="ext:hermes-hub:free-fed-skill", redistributable=True)
        cb = _bundle_with_member(db_session, member)

        with TestClient(_app(db_session)) as c:
            r = c.get(f"/api/bundles/public/{cb.slug}/.well-known/skills/index.json")
        assert r.status_code == 200, r.text
        entry = next(s for s in r.json()["skills"] if s["name"] == member.slug)
        assert "locked" not in entry, "a redistributable external skill must NOT be flagged locked"

    def test_skill_md_streams_live_origin_content_for_redistributable_external(self, db_session, monkeypatch):
        member = _make_external_skill(db_session, slug="ext:hermes-hub:free-fed-skill", redistributable=True)
        cb = _bundle_with_member(db_session, member)

        from app.services import bundle_external as ce

        monkeypatch.setattr(
            ce,
            "resolve_external_install",
            lambda source, slug: {
                "content": "# Real Body\nfetched live from origin",
                "raw_url": "https://raw.githubusercontent.com/example/free-fed-skill/main/SKILL.md",
            },
        )

        with TestClient(_app(db_session)) as c:
            r = c.get(f"/api/bundles/public/{cb.slug}/.well-known/skills/{member.slug}/SKILL.md")
        assert r.status_code == 200, r.text
        assert "Real Body" in r.text
        assert "locked: true" not in r.text

    def test_skill_md_degrades_to_honest_stub_on_transient_origin_failure(self, db_session, monkeypatch):
        """A redistributable external skill whose origin fetch fails at serve
        time (stale index row, transient outage) must fall back to the
        non-leaking stub — never a raw error, never fabricated content."""
        member = _make_external_skill(db_session, slug="ext:hermes-hub:stale-fed-skill", redistributable=True)
        cb = _bundle_with_member(db_session, member)

        from app.services import bundle_external as ce

        monkeypatch.setattr(ce, "resolve_external_install", lambda source, slug: None)

        with TestClient(_app(db_session)) as c:
            r = c.get(f"/api/bundles/public/{cb.slug}/.well-known/skills/{member.slug}/SKILL.md")
        assert r.status_code == 200, r.text
        assert "locked: true" in r.text
        assert f"name: {member.slug}" in r.text


class TestNonRedistributableExternalStaysLocked:
    def test_index_still_locks_a_deep_link_external_skill(self, db_session):
        member = _make_external_skill(
            db_session,
            slug="ext:clawhub:proprietary-fed-skill",
            redistributable=False,
            install_path="deep_link",
        )
        cb = _bundle_with_member(db_session, member, slug="fed-bundle-locked")

        with TestClient(_app(db_session)) as c:
            r = c.get(f"/api/bundles/public/{cb.slug}/.well-known/skills/index.json")
        assert r.status_code == 200, r.text
        entry = next(s for s in r.json()["skills"] if s["name"] == member.slug)
        assert entry.get("locked") is True, "a non-redistributable external skill must stay locked"

    def test_skill_md_serves_stub_not_body_for_non_redistributable_external(self, db_session):
        member = _make_external_skill(
            db_session,
            slug="ext:clawhub:proprietary-fed-skill",
            redistributable=False,
            install_path="deep_link",
        )
        cb = _bundle_with_member(db_session, member, slug="fed-bundle-locked-2")

        with TestClient(_app(db_session)) as c:
            r = c.get(f"/api/bundles/public/{cb.slug}/.well-known/skills/{member.slug}/SKILL.md")
        assert r.status_code == 200, r.text
        assert "locked: true" in r.text
        assert f"name: {member.slug}" in r.text


class TestInternalSkillsUnaffected:
    """Byte-for-byte regression pin: a plain internal Skill's free/paid
    behaviour (the ORIGINAL contract this module already had) must be
    untouched by the new external branch."""

    def test_free_internal_skill_still_serves_real_body(self, db_session):
        owner = _seed_owner(db_session)
        free = Skill(
            id=uuid4(),
            slug="free-internal",
            title="Free Internal",
            tier="free",
            is_public=True,
            readme="# Free\n\nreal internal body",
        )
        db_session.add(free)
        db_session.flush()
        cb = Bundle(
            id=uuid4(),
            name="Internal Bundle",
            slug="internal-bundle",
            visibility="public",
            bundle_owner=owner.id,
        )
        db_session.add(cb)
        db_session.flush()
        db_session.add(BundleSkill(bundle_id=cb.id, skill_id=free.id, source="custom-added"))
        db_session.commit()

        with TestClient(_app(db_session)) as c:
            r = c.get(f"/api/bundles/public/{cb.slug}/.well-known/skills/{free.slug}/SKILL.md")
        assert r.status_code == 200, r.text
        assert "real internal body" in r.text

    def test_paid_internal_skill_still_serves_stub(self, db_session):
        owner = _seed_owner(db_session)
        paid = Skill(
            id=uuid4(),
            slug="paid-internal",
            title="Paid Internal",
            tier="pro",
            is_public=True,
            readme="SECRET must never leak",
        )
        db_session.add(paid)
        db_session.flush()
        cb = Bundle(
            id=uuid4(),
            name="Internal Bundle 2",
            slug="internal-bundle-2",
            visibility="public",
            bundle_owner=owner.id,
        )
        db_session.add(cb)
        db_session.flush()
        db_session.add(BundleSkill(bundle_id=cb.id, skill_id=paid.id, source="custom-added"))
        db_session.commit()

        with TestClient(_app(db_session)) as c:
            r = c.get(f"/api/bundles/public/{cb.slug}/.well-known/skills/{paid.slug}/SKILL.md")
        assert r.status_code == 200, r.text
        assert "SECRET" not in r.text
        assert "locked: true" in r.text

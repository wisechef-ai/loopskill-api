"""federation_0604 Unit 2 — cookbooks hold external (federated) skills.

The superset move: a cookbook can hold a federated skill (lobehub, clawhub,
skills-sh, hermes-hub, browse-sh, well-known) handed to an agent as ONE link,
exactly like an internal skill — while NEVER rehosting external content
(install resolves from origin at install time, the federation_0604 contract).

Design under test (materialize-on-add):
  - Adding an external skill MATERIALIZES a thin ``Skill`` row
    (slug ``ext:{source}:{slug}``, is_public=False, skill_variant="external",
     tier="external") so the existing ``cookbook_skills.skill_id`` FK and ALL
     cookbook plumbing (install/manifest/sync/share-token) work unchanged.
  - The row is a POINTER, not a content snapshot: a re-resolution descriptor
    lives in ``external_resources``; install fetches the body from origin.
  - Isolation walls (all asserted here):
      1. is_public=False → invisible to the public catalog (search filters it).
      2. Bulk install returns a CHEAP descriptor + cookbook-scoped URL, never
         N origin fetches.
      3. Single install resolves content inline from origin (never rehosted).

All network is mocked — no live calls in CI (Mom-test discipline).
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
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import get_db
from app.models import Base, Cookbook, CookbookSkill, Skill, User
from app.services.federation import ExternalSkill, InstallPath


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

def _make_user(db: Session, *, tier: str = "pro_plus") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _make_app(db: Session, *, api_key_user_id) -> FastAPI:
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
            request.state.api_key_user_id = _uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)
    return app


def _stub_external(source="lobehub", slug="seo-writer") -> ExternalSkill:
    return ExternalSkill(
        slug=slug,
        title="SEO Writer",
        source=source,
        install_path=InstallPath.FETCH_ORIGIN,
        origin_url=f"https://{source}.example/{slug}",
        license="MIT",
        redistributable=True,
        description="Writes SEO copy.",
    )


# ─────────────────────── Materialize-on-add (unit) ──────────────────────

class TestMaterializeExternalSkill:
    def test_materialize_creates_private_pointer_row(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))

        skill = ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()

        assert skill.slug == "ext:lobehub:seo-writer"
        assert skill.is_public is False, "external rows MUST be private (catalog isolation)"
        assert skill.skill_variant == "external"
        assert skill.tier == "external"
        assert skill.title == "SEO Writer"
        assert skill.license == "MIT"
        # re-resolution descriptor — the POINTER, not a content snapshot
        desc = skill.external_resources
        assert desc["federation_source"] == "lobehub"
        assert desc["external_slug"] == "seo-writer"
        assert desc["install_path"] == "fetch_origin"
        assert skill.original_source_url == "https://lobehub.example/seo-writer"

    def test_materialize_is_idempotent(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))

        a = ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()
        b = ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()

        assert a.id == b.id, "same external skill must reuse the one materialized row"
        rows = db_session.query(Skill).filter(Skill.slug == "ext:lobehub:seo-writer").all()
        assert len(rows) == 1, "no duplicate Skill rows for the same external skill"

    def test_materialize_unresolvable_returns_none(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: None)
        assert ce.materialize_external_skill(db_session, "lobehub", "ghost") is None

    def test_is_external_skill_predicate(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))
        ext = ce.materialize_external_skill(db_session, "clawhub", "x")
        internal = Skill(id=uuid4(), slug="plain", title="Plain", is_public=True)
        assert ce.is_external_skill(ext) is True
        assert ce.is_external_skill(internal) is False


# ─────────────────────── Isolation wall #1: catalog ─────────────────────

class TestCatalogIsolation:
    def test_external_row_excluded_by_public_catalog_filter(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))
        ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()

        # The canonical public-catalog filter (skill_routes uses exactly this).
        visible = (
            db_session.query(Skill)
            .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
            .all()
        )
        assert all(not s.slug.startswith("ext:") for s in visible), (
            "materialized external skills must NEVER surface in the public catalog"
        )


# ─────────────────────── Add external skill (route) ─────────────────────

class TestAddExternalSkillToCookbook:
    def test_add_external_materializes_and_links(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))

        user = _make_user(db_session)
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post(
                f"/api/cookbooks/{cb.id}/skills",
                json={"slug": "seo-writer", "external_source": "lobehub"},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == "ext:lobehub:seo-writer"
        assert body["external"] is True
        assert body["source"] == "custom-added"

        cs = (
            db_session.query(CookbookSkill)
            .join(Skill, Skill.id == CookbookSkill.skill_id)
            .filter(CookbookSkill.cookbook_id == cb.id, Skill.slug == "ext:lobehub:seo-writer")
            .first()
        )
        assert cs is not None, "cookbook_skills join row must exist (FK satisfied)"

    def test_add_external_unknown_source_422(self, db_session):
        user = _make_user(db_session)
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post(
                f"/api/cookbooks/{cb.id}/skills",
                json={"slug": "x", "external_source": "not-a-source"},
            )
        assert r.status_code == 422
        assert r.json()["detail"] == "unknown_external_source"

    def test_add_external_unresolvable_404(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: None)
        user = _make_user(db_session)
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post(
                f"/api/cookbooks/{cb.id}/skills",
                json={"slug": "ghost", "external_source": "lobehub"},
            )
        assert r.status_code == 404
        assert r.json()["detail"] == "external_skill_not_found"


# ─────────────── Isolation wall #2: bulk = cheap descriptor ─────────────

class TestBulkInstallDescriptor:
    def test_bulk_install_external_returns_url_not_content(self, db_session, monkeypatch):
        """Bulk install must NOT fetch N origins — it returns a cheap
        per-skill descriptor with a cookbook-scoped install URL. The actual
        origin fetch happens per-skill on the single-install path."""
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))

        # Tripwire: bulk path must never call the origin fetcher.
        def _boom(*a, **k):
            raise AssertionError("bulk install must NOT resolve origin content")

        monkeypatch.setattr(ce, "resolve_external_install", _boom)

        user = _make_user(db_session)
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        skill = ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()
        db_session.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post(f"/api/cookbooks/{cb.id}/install")
        assert r.status_code == 200, r.text
        entry = next(s for s in r.json()["skills"] if s["slug"] == "ext:lobehub:seo-writer")
        assert entry["external"] is True
        assert entry["tarball_url"] is None, "external skills have no tarball"
        assert entry["install_url"].endswith(
            f"/api/cookbooks/{cb.id}/skills/ext:lobehub:seo-writer/install"
        )
        assert "content" not in entry, "bulk must stay cheap — no inline body"


# ─────────────── Isolation wall #3: single = origin resolve ─────────────

class TestSingleInstallResolvesOrigin:
    def test_single_external_install_streams_origin_content(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))
        # Stub the origin fetcher the resolver calls.
        monkeypatch.setattr(
            ce,
            "get_origin_fetcher",
            lambda source: (lambda sl: ("https://raw.example/SKILL.md", "# SEO Writer\nreal body")),
        )

        user = _make_user(db_session)
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        skill = ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()
        db_session.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.get(
                f"/api/cookbooks/{cb.id}/skills/ext:lobehub:seo-writer/install"
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["external"] is True
        assert body["content"] == "# SEO Writer\nreal body"
        assert body["raw_url"] == "https://raw.example/SKILL.md"
        assert body["license"] == "MIT"
        assert "install_command" in body
        assert "curl" in body["install_command"]

    def test_single_external_install_origin_unreachable_404(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))
        monkeypatch.setattr(ce, "get_origin_fetcher", lambda source: (lambda sl: None))

        user = _make_user(db_session)
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        skill = ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()
        db_session.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.get(
                f"/api/cookbooks/{cb.id}/skills/ext:lobehub:seo-writer/install"
            )
        assert r.status_code == 404


# ─────────────────────── Shared resolver (SSOT) ─────────────────────────

class TestResolveExternalInstallSSOT:
    def test_resolver_returns_origin_payload(self, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))
        monkeypatch.setattr(
            ce,
            "get_origin_fetcher",
            lambda source: (lambda sl: ("https://raw.example/SKILL.md", "BODY")),
        )
        out = ce.resolve_external_install("lobehub", "seo-writer")
        assert out["content"] == "BODY"
        assert out["raw_url"] == "https://raw.example/SKILL.md"
        assert out["license"] == "MIT"
        assert out["source"] == "lobehub"

    def test_resolver_deep_link_blocks(self, monkeypatch):
        from app.services import cookbook_external as ce

        locked = ExternalSkill(
            slug="x", title="X", source="clawhub",
            install_path=InstallPath.DEEP_LINK,
            origin_url="https://clawhub.example/x", license="proprietary",
            redistributable=False,
        )
        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: locked)
        out = ce.resolve_external_install("clawhub", "x")
        assert out is None, "deep-link / non-redistributable must not resolve content"


# ───────────────── MCP/REST parity (AGENTS.md contract) ─────────────────

class TestMcpExternalParity:
    """recipes_cookbook_install MUST mirror the REST external shapes exactly so
    agents can switch transports without re-parsing (AGENTS.md contract)."""

    def _setup_cb_with_external(self, db_session, monkeypatch):
        from app.services import cookbook_external as ce

        monkeypatch.setattr(ce, "_resolve_external", lambda s, sl: _stub_external(s, sl))
        user = _make_user(db_session)
        cb = Cookbook(id=uuid4(), name="Mine", cookbook_owner=user.id)
        db_session.add(cb)
        skill = ce.materialize_external_skill(db_session, "lobehub", "seo-writer")
        db_session.flush()
        db_session.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))
        db_session.commit()
        return user, cb

    def test_mcp_bulk_external_returns_descriptor(self, db_session, monkeypatch):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.cookbook_install import recipes_cookbook_install
        from app.services import cookbook_external as ce

        user, cb = self._setup_cb_with_external(db_session, monkeypatch)
        # Tripwire: MCP bulk must not resolve origin content either.
        monkeypatch.setattr(
            ce, "resolve_external_install",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("bulk must not fetch origin")),
        )
        ctx = AuthContext(scope="user", user_id=user.id)
        result = recipes_cookbook_install(db=db_session, ctx=ctx, cookbook_id=str(cb.id))
        entry = next(s for s in result["skills"] if s["slug"] == "ext:lobehub:seo-writer")
        assert entry["external"] is True
        assert entry["tarball_url"] is None
        assert entry["install_url"].endswith(
            f"/api/cookbooks/{cb.id}/skills/ext:lobehub:seo-writer/install"
        )

    def test_mcp_single_external_streams_origin(self, db_session, monkeypatch):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.cookbook_install import recipes_cookbook_install
        from app.services import cookbook_external as ce

        user, cb = self._setup_cb_with_external(db_session, monkeypatch)
        monkeypatch.setattr(
            ce, "get_origin_fetcher",
            lambda source: (lambda sl: ("https://raw.example/SKILL.md", "BODY")),
        )
        ctx = AuthContext(scope="user", user_id=user.id)
        result = recipes_cookbook_install(
            db=db_session, ctx=ctx, cookbook_id=str(cb.id), slug="ext:lobehub:seo-writer"
        )
        assert result["external"] is True
        assert result["content"] == "BODY"
        assert result["raw_url"] == "https://raw.example/SKILL.md"


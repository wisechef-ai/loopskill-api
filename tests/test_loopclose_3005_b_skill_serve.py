"""loopclose_3005 Phase B — canonical /skill serve route.

Proves GET /skill serves the in-repo SKILL.md directly as text/plain (200, no
302 to the GitHub mirror), with leak headers stripped and the correct MCP tool
list, so an agent can `curl -sL .../skill -o SKILL.md` and load it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.skill_serve_routes import (
    SKILL_MD_PATH,
    _canonical_skill_md,
    _strip_leak_headers,
    skill_serve_router,
)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(skill_serve_router)
    return TestClient(app)


class TestSkillServeRoute:
    def test_returns_200_text_plain_no_redirect(self):
        with _client() as client:
            r = client.get("/skill", follow_redirects=False)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/plain")
        # No redirect — the whole point of Phase B.
        assert "location" not in {k.lower() for k in r.headers}

    def test_body_is_the_in_repo_source(self):
        with _client() as client:
            body = client.get("/skill").text
        # Served body equals the cleaned in-repo canonical source.
        assert body == _canonical_skill_md()
        assert body.startswith("---\nname: recipes")

    def test_no_leak_headers_in_served_body(self):
        with _client() as client:
            body = client.get("/skill").text
        for leak in ("auto-mirrored from", "DO NOT EDIT", "last sync: commit"):
            assert leak not in body, f"leak header leaked into /skill: {leak!r}"

    def test_no_phantom_cli(self):
        """The stale mirror named a nonexistent `recipes share <id>` CLI."""
        with _client() as client:
            body = client.get("/skill").text
        assert "recipes share <id>" not in body

    def test_correct_tool_count_and_tailor_tools_present(self):
        with _client() as client:
            body = client.get("/skill").text
        # Post-Phase-0 the canonical doc lists 26 tools incl. the tailor tools.
        assert "26 MCP tools available" in body
        assert "`recipes_tailor`" in body
        assert "`recipes_fork_list`" in body

    def test_aliases_serve_same_body(self):
        """/skill/ and /SKILL.md serve the same canonical body as /skill."""
        with _client() as client:
            canonical = client.get("/skill").text
            assert client.get("/skill/").text == canonical
            assert client.get("/SKILL.md").text == canonical


class TestSkillPublicViaMiddleware:
    """Regression: /skill MUST be exempt from APIKeyMiddleware (it 401'd in the
    first deploy). Uses build_test_app — the production middleware seam, same
    pattern as the W0.1 skill-files public-middleware regression."""

    @pytest.fixture()
    def _db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.models import Base

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        connection = engine.connect()
        transaction = connection.begin()
        SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()
            connection.close()
            Base.metadata.drop_all(bind=engine)

    def test_skill_is_public_no_key_needed(self, _db, monkeypatch):
        from tests._app_factory import build_test_app

        app = build_test_app(db_session=_db, monkeypatch=monkeypatch)
        client = TestClient(app)
        r = client.get("/skill", follow_redirects=False)  # deliberately no x-api-key
        assert r.status_code == 200, (
            f"/skill must be public at the middleware seam, got {r.status_code}: {r.text[:200]}"
        )
        assert r.headers["content-type"].startswith("text/plain")
        assert "26 MCP tools available" in r.text

    def test_skill_aliases_public(self, _db, monkeypatch):
        from tests._app_factory import build_test_app

        app = build_test_app(db_session=_db, monkeypatch=monkeypatch)
        client = TestClient(app)
        for path in ("/skill/", "/SKILL.md"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 200, f"{path} must be public, got {r.status_code}"


class TestMiddlewareExemptGuard:
    """Grep guard — a refactor that drops /skill from EXEMPT_PATHS re-breaks the
    bare-401 bug. Trip CI here, not in prod."""

    def test_middleware_exempts_skill_paths(self):
        from pathlib import Path

        src = Path("app/middleware/api_key.py").read_text(encoding="utf-8")
        for p in ('"/skill"', '"/skill/"', '"/SKILL.md"'):
            assert p in src, (
                f"APIKeyMiddleware no longer exempts {p} — re-introduces the "
                "loopclose_3005 Phase B bare-401 bug. Restore it in EXEMPT_PATHS."
            )


class TestLeakHeaderStripping:
    def test_strips_mirror_bot_headers(self):
        dirty = (
            "<!-- auto-mirrored from wisechef-ai/recipes-api:docs/recipes-skill/SKILL.md -->\n"
            "<!-- DO NOT EDIT here — edit upstream and the bot will sync -->\n"
            "<!-- last sync: commit 2d0f8ad -->\n"
            "\n"
            "---\n"
            "name: recipes\n"
        )
        cleaned = _strip_leak_headers(dirty)
        assert cleaned.startswith("---\nname: recipes")
        assert "auto-mirrored" not in cleaned
        assert "DO NOT EDIT" not in cleaned
        assert "last sync" not in cleaned

    def test_clean_source_unchanged(self):
        clean = "---\nname: recipes\ndescription: x\n"
        assert _strip_leak_headers(clean) == clean

    def test_in_repo_source_is_already_clean(self):
        """The committed source must carry no leak headers (drift guard)."""
        raw = SKILL_MD_PATH.read_text(encoding="utf-8")
        for leak in ("auto-mirrored from", "DO NOT EDIT", "last sync: commit"):
            assert leak not in raw, f"in-repo SKILL.md contains a leak header: {leak!r}"

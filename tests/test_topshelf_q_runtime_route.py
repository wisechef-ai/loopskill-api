"""Phase Q — tests for /api/skills/{slug}/runtime.

Covers:
  - Frontmatter parsing: runtime:, runtimes:, tools:, requires: keys
  - Category fallback when frontmatter has no runtime keys
  - inferred=True flag when inference was used
  - frontmatter_present flag
  - No readme → empty runtimes, inferred from category
  - Unknown category → empty runtimes, inferred=False
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Skill


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def set_pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture):
    conn = engine_fixture.connect()
    txn = conn.begin()
    _Session = sessionmaker(bind=conn)
    session = _Session()
    nested = conn.begin_nested()

    @sa_event.listens_for(session, "after_transaction_end")
    def restart_sp(s, t):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    yield session
    session.close()
    txn.rollback()
    conn.close()


def _make_skill(
    db,
    slug: str,
    category: str = "devops",
    readme: str | None = None,
    tier: str = "pro",
) -> Skill:
    sk = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.title(),
        description="Test skill",
        tier=tier,
        category=category,
        readme=readme,
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    return sk


def _make_app(db_session):
    from app.skill_files_routes import router as files_router

    app = FastAPI()
    app.include_router(files_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestRuntimeEndpoint:

    def test_frontmatter_runtime_key(self, db_session):
        """runtime: python in frontmatter → runtimes=["python"], inferred=False."""
        readme = "---\nruntime: python\n---\n# Skill"
        _make_skill(db_session, slug="rt-runtime-q", readme=readme, category="data")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-runtime-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert "python" in data["runtimes"]
        assert data["inferred"] is False
        assert data["frontmatter_present"] is True

    def test_frontmatter_runtimes_list(self, db_session):
        """runtimes: [python, node] list → both in runtimes."""
        readme = "---\nruntimes:\n  - python\n  - node\n---\n# Skill"
        _make_skill(db_session, slug="rt-runtimes-list-q", readme=readme, category="data")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-runtimes-list-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert "python" in data["runtimes"]
        assert "node" in data["runtimes"]
        assert data["inferred"] is False

    def test_frontmatter_tools_key(self, db_session):
        """tools: [jq, curl] → tools_required populated."""
        readme = "---\nruntime: bash\ntools:\n  - jq\n  - curl\n---\n# Skill"
        _make_skill(db_session, slug="rt-tools-q", readme=readme, category="devops")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-tools-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert "jq" in data["tools_required"]
        assert "curl" in data["tools_required"]

    def test_frontmatter_requires_key(self, db_session):
        """requires: [docker] → tools_required populated."""
        readme = "---\nruntime: docker\nrequires:\n  - docker\n---\n# Skill"
        _make_skill(db_session, slug="rt-requires-q", readme=readme, category="devops")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-requires-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert "docker" in data["tools_required"]

    def test_category_fallback_python(self, db_session):
        """No frontmatter → category=data infers python, inferred=True."""
        _make_skill(db_session, slug="rt-cat-python-q", category="data", readme=None)

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-cat-python-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert "python" in data["runtimes"]
        assert data["inferred"] is True
        assert data["frontmatter_present"] is False

    def test_category_fallback_devops(self, db_session):
        """No frontmatter → category=devops infers bash+docker."""
        _make_skill(db_session, slug="rt-cat-devops-q", category="devops", readme=None)

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-cat-devops-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert "bash" in data["runtimes"] or "docker" in data["runtimes"]
        assert data["inferred"] is True

    def test_unknown_category_no_inference(self, db_session):
        """Unknown category → empty runtimes, inferred=False."""
        _make_skill(db_session, slug="rt-unknown-cat-q", category="xyzzy", readme=None)

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-unknown-cat-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runtimes"] == []
        assert data["inferred"] is False

    def test_no_readme_uses_category(self, db_session):
        """readme=None → frontmatter_present=False, falls back to category."""
        _make_skill(db_session, slug="rt-noreadme-q", category="python", readme=None)

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-noreadme-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert data["frontmatter_present"] is False
        # Python category should infer python runtime
        assert data["inferred"] is True

    def test_frontmatter_present_without_runtime_key(self, db_session):
        """Frontmatter exists but no runtime/runtimes key → falls back to category."""
        readme = "---\ntitle: My Skill\nauthor: Test\n---\n# Skill body"
        _make_skill(db_session, slug="rt-no-runtime-key-q", readme=readme, category="data")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-no-runtime-key-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        # frontmatter IS present
        assert data["frontmatter_present"] is True
        # But runtime key absent → inferred from category
        assert data["inferred"] is True
        assert "python" in data["runtimes"]

    def test_skill_not_found_returns_404(self, db_session):
        """Unknown slug → 404."""
        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/nonexistent-rt-q/runtime")
        assert resp.status_code == 404

    def test_response_shape(self, db_session):
        """Response always has all four keys."""
        _make_skill(db_session, slug="rt-shape-q", category="devops")

        app = _make_app(db_session)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/skills/rt-shape-q/runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert "runtimes" in data
        assert "tools_required" in data
        assert "frontmatter_present" in data
        assert "inferred" in data


# ── Unit tests for the helper functions ────────────────────────────────────────


class TestParseFrontmatterRuntimes:
    """Direct unit tests for the parser — no HTTP, no DB."""

    def test_parse_no_readme(self):
        from app.skill_files_routes import _parse_frontmatter_runtimes
        result = _parse_frontmatter_runtimes(None)
        assert result["frontmatter_present"] is False
        assert result["runtimes"] == []

    def test_parse_no_frontmatter(self):
        from app.skill_files_routes import _parse_frontmatter_runtimes
        result = _parse_frontmatter_runtimes("# Just markdown, no frontmatter")
        assert result["frontmatter_present"] is False

    def test_parse_single_runtime(self):
        from app.skill_files_routes import _parse_frontmatter_runtimes
        result = _parse_frontmatter_runtimes("---\nruntime: python\n---\n# Skill")
        assert "python" in result["runtimes"]
        assert result["frontmatter_present"] is True

    def test_parse_compatible_key(self):
        from app.skill_files_routes import _parse_frontmatter_runtimes
        result = _parse_frontmatter_runtimes("---\ncompatible:\n  - node\n  - deno\n---\n# Skill")
        assert "node" in result["runtimes"]

    def test_parse_deduplication(self):
        from app.skill_files_routes import _parse_frontmatter_runtimes
        # runtime: python + runtimes: [python] → deduped
        result = _parse_frontmatter_runtimes(
            "---\nruntime: python\nruntimes:\n  - python\n  - node\n---\n# Skill"
        )
        assert result["runtimes"].count("python") == 1

    def test_infer_runtimes_from_category(self):
        from app.skill_files_routes import _infer_runtimes_from_category
        assert _infer_runtimes_from_category("python") == ["python"]
        assert _infer_runtimes_from_category("javascript") == ["node"]
        assert _infer_runtimes_from_category(None) == []
        assert _infer_runtimes_from_category("unknown_cat") == []

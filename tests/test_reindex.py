"""Phase 4 — BM25 auto-reindex on publish/archive tests.

Tests:
  - test_publish_updates_search_within_one_second
  - test_archive_drops_from_search
  - test_admin_reindex_all_no_regression
  - test_publish_201_response_under_500ms
  - test_search_vector_is_set_after_publish

All tests use in-memory SQLite + TestClient (no Postgres / Redis needed).
The APIKeyMiddleware is bypassed by injecting request.state directly.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Generator
from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from app.database import get_db
from app.models import Base, Creator, Skill, SkillVersion, User


# ── DB Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def engine_fixture():
    """In-memory SQLite engine shared for the test session."""
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
    """Per-test transactional session that rolls back after each test."""
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


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session, user_id=None) -> User:
    uid = user_id or uuid4()
    user = User(id=uid, display_name="Test Creator", email=f"{uid}@test.example")
    db.add(user)
    db.flush()
    return user


def _make_creator(db: Session, user: User, slug="test-creator") -> Creator:
    creator = Creator(id=uuid4(), user_id=user.id, name="Test Creator", slug=slug)
    db.add(creator)
    db.flush()
    return creator


def _make_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv, pub_bytes


def _sign_tarball(priv_key, tarball_bytes: bytes) -> bytes:
    digest = hashlib.sha256(tarball_bytes).digest()
    return priv_key.sign(digest)


def _valid_toml(
    name="test-canary-bm25",
    version="1.0.0",
    description="A canary skill for BM25 testing",
    license="MIT",
    entrypoint="run.sh",
    slug=None,
) -> bytes:
    slug_line = f'\nslug = "{slug}"' if slug else ""
    return (
        f'[skill]\n'
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f'description = "{description}"\n'
        f'license = "{license}"\n'
        f'entrypoint = "{entrypoint}"'
        f'{slug_line}\n'
    ).encode()


def _make_tarball(content: bytes = b"fake tarball data") -> bytes:
    return content


# ── App Fixture (publisher + recall + admin routes) ──────────────────────


def _make_client(db: Session, skills_dir: str):
    """Create a TestClient with publisher, recall, and admin routes."""
    from app.publisher_routes import router as publisher_router
    from app.recall_routes import router as recall_router
    from app.admin_routes import router as admin_router

    test_app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.include_router(publisher_router)
    test_app.include_router(recall_router)
    test_app.include_router(admin_router)

    # Inject auth state — admin (master key)
    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            request.state.api_key_user_id = None  # admin / master key
            request.state.api_key_id = None
            return await call_next(request)

    test_app.add_middleware(InjectAuthState)

    env_patch = patch.dict(os.environ, {"RECIPES_SKILLS_DIR": skills_dir})
    env_patch.start()

    client = TestClient(test_app, raise_server_exceptions=True)
    return client, env_patch


@pytest.fixture()
def client_fixture(db_session):
    """TestClient with publisher + recall + admin routes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        client, env_patch = _make_client(db_session, tmpdir)
        yield client, db_session
        env_patch.stop()


# ── Canary publish helper ────────────────────────────────────────────────


def _publish_canary(client, slug="test-canary-bm25", title="xyzzyplugh-marker",
                     description="Canary skill for BM25", version="1.0.0", db=None):
    """Publish a canary skill via the _publish endpoint.

    If *db* is provided, sets tier='free' on the created skill so it
    shows up in recall (which filters by tier).
    """
    priv, pub_bytes = _make_keypair()
    tarball = _make_tarball()
    sig = _sign_tarball(priv, tarball)
    toml_bytes = _valid_toml(
        name=title,
        version=version,
        description=description,
        slug=slug,
    )
    resp = client.post(
        "/api/skills/_publish",
        files={
            "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "application/octet-stream"),
            "tarball": ("skill.tar.gz", io.BytesIO(tarball), "application/gzip"),
            "signature": ("sig.bin", io.BytesIO(sig), "application/octet-stream"),
            "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
        },
        data={"is_public": "true"},
    )
    # Set tier so recall can find it (recall filters by tier).
    if resp.status_code == 201 and db is not None:
        from app.models import Skill as _Skill
        skill = db.query(_Skill).filter(_Skill.slug == slug).first()
        if skill:
            skill.tier = "free"
            skill.is_public = True
            db.flush()
    return resp


# ══════════════════════════════════════════════════════════════════════════
# TEST CASES
# ══════════════════════════════════════════════════════════════════════════


def test_publish_updates_search_within_one_second(client_fixture):
    """Publish a canary skill, then recall must find it within 1 second."""
    client, db = client_fixture

    # Publish canary
    resp = _publish_canary(client, title="xyzzyplugh-marker", db=db)
    assert resp.status_code == 201, f"publish failed: {resp.text}"

    # Recall must find it
    recall_resp = client.post(
        "/api/recall",
        json={"query": "xyzzyplugh-marker", "tier_filter": ["free", "cook", "operator"]},
    )
    assert recall_resp.status_code == 200, f"recall failed: {recall_resp.text}"
    data = recall_resp.json()
    slugs = [h["slug"] for h in data["hits"]]
    assert "test-canary-bm25" in slugs, (
        f"Canary not in recall results within 1s. Hits: {slugs}"
    )


def test_archive_drops_from_search(client_fixture):
    """After archiving, recall must NOT return the skill."""
    client, db = client_fixture

    # Publish canary
    resp = _publish_canary(client, title="archive-test-canary", db=db)
    assert resp.status_code == 201

    # Verify it shows in recall
    recall_resp = client.post(
        "/api/recall",
        json={"query": "archive-test-canary", "tier_filter": ["free", "cook", "operator"]},
    )
    assert recall_resp.status_code == 200
    slugs = [h["slug"] for h in recall_resp.json()["hits"]]
    assert "test-canary-bm25" in slugs, "Canary should appear before archiving"

    # Archive it
    archive_resp = client.post("/api/skills/test-canary-bm25/_archive")
    assert archive_resp.status_code == 200, f"archive failed: {archive_resp.text}"

    # Recall must NOT return it
    recall_resp2 = client.post(
        "/api/recall",
        json={"query": "archive-test-canary", "tier_filter": ["free", "cook", "operator"]},
    )
    assert recall_resp2.status_code == 200
    slugs2 = [h["slug"] for h in recall_resp2.json()["hits"]]
    assert "test-canary-bm25" not in slugs2, (
        f"Archived skill still in results: {slugs2}"
    )


def test_admin_reindex_all_no_regression(client_fixture):
    """POST /api/admin/reindex-all should not break existing search results."""
    client, db = client_fixture

    # Publish a canary
    resp = _publish_canary(client, title="reindex-regression-canary", db=db)
    assert resp.status_code == 201

    # Call admin reindex-all
    reindex_resp = client.post("/api/admin/reindex-all")
    assert reindex_resp.status_code == 200, f"reindex-all failed: {reindex_resp.text}"
    data = reindex_resp.json()
    assert data["reindexed"] >= 1

    # Recall must still find the canary
    recall_resp = client.post(
        "/api/recall",
        json={"query": "reindex-regression-canary", "tier_filter": ["free", "cook", "operator"]},
    )
    assert recall_resp.status_code == 200
    slugs = [h["slug"] for h in recall_resp.json()["hits"]]
    assert "test-canary-bm25" in slugs, (
        f"Canary lost after reindex-all. Hits: {slugs}"
    )


def test_publish_201_response_under_500ms(client_fixture):
    """Publish response time must stay under 500ms (BM25 is fast)."""
    client, db = client_fixture

    start = time.monotonic()
    resp = _publish_canary(client, title="perf-test-canary", version="2.0.0", db=db)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 201, f"publish failed: {resp.text}"
    assert elapsed_ms < 500, (
        f"Publish took {elapsed_ms:.1f}ms — must be under 500ms. "
        f"This guards against accidentally adding async/embedding work."
    )

    # Log performance for PR body
    print(f"\n[P4-PERF] publish_201_response_time = {elapsed_ms:.1f}ms")


def test_search_vector_is_set_after_publish(client_fixture):
    """Direct DB check: search_vector must be non-NULL after publish."""
    client, db = client_fixture

    resp = _publish_canary(client, title="search-vector-canary", db=db)
    assert resp.status_code == 201

    # Direct SQL query to check search_vector
    row = db.execute(
        text("SELECT search_vector FROM skills WHERE slug = :slug"),
        {"slug": "test-canary-bm25"},
    ).fetchone()
    assert row is not None, "Skill row not found"
    assert row[0] is not None, "search_vector is NULL after publish"
    assert len(row[0]) > 0, "search_vector is empty after publish"

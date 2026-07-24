"""Regression: the signed install token round-trips version_id as a STRING, and
the /api/skills/_download handler must coerce it to UUID before querying
SkillVersion.id (UUID column).

The bug (caught by a cold-clone self-host install, 2026-06-26): on SQLite — the
self-host path — querying ``SkillVersion.id == "<uuid-string>"`` makes the
UUID(as_uuid=True) type adapter call ``.hex`` on the raw str and raise
``AttributeError: 'str' object has no attribute 'hex'`` → HTTP 500. Postgres
(CI) silently coerced it, so the suite never caught it; only a real install
against a SQLite container hit it.

After the fix the handler coerces to UUID, so a valid token reaches the
tarball-on-disk check and returns a clean 404 ("Tarball missing on disk") for a
seeded skill with no physical tarball — NOT a 500.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


def _seed_version(db, *, slug: str) -> uuid.UUID:
    """Create a free public skill + one version; return the version id."""
    from app.models import Skill, SkillVersion

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug.replace("-", " ").title(),
        description=f"probe {slug}",
        tier="free",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=sk.id,
        semver="1.0.0",
        tarball_size_bytes=1024,
        checksum_sha256="deadbeef" * 8,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return v.id


def _sign(version_id: uuid.UUID, slug: str) -> str:
    """Sign a download token exactly like the install builders do — version_id
    serialized as a STRING (this is what triggers the bug)."""
    from app.config import settings

    s = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    return s.dumps({"slug": slug, "version_id": str(version_id), "mode": "install"})


def test_download_with_string_version_id_does_not_500(middleware_client, db_session):
    """A valid signed token (string version_id) must NOT crash the download
    handler on SQLite. It should reach the tarball-on-disk check → 404, not 500."""
    slug = "dl-uuid-coercion-probe"
    vid = _seed_version(db_session, slug=slug)
    token = _sign(vid, slug)

    resp = middleware_client.get(f"/api/skills/_download?token={token}")

    # The bug produced 500 ('str' has no attribute 'hex'). After the fix, the
    # query resolves the version and we hit the missing-tarball path → 404.
    assert resp.status_code != 500, (
        f"_download 500'd on a string version_id — UUID coercion regressed. "
        f"Body: {resp.text[:200]}"
    )
    assert resp.status_code == 404
    assert "missing on disk" in resp.text.lower()


def test_download_malformed_version_id_is_400_not_500(middleware_client, db_session):
    """A token carrying a non-UUID version_id must 400, never 500."""
    from app.config import settings

    s = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    bad = s.dumps({"slug": "x", "version_id": "not-a-uuid", "mode": "install"})

    resp = middleware_client.get(f"/api/skills/_download?token={bad}")

    assert resp.status_code != 500, f"malformed id should 400, got 500: {resp.text[:200]}"
    assert resp.status_code == 400

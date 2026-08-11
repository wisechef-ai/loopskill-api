"""bundles0811 Phase P3 — GET /api/skills/install?slug=hermes-hub:<slug>
must actually resolve (item 3 of the P3 brief).

Live prod repro before this fix (verified by the brief's author):
    GET /api/skills/install?slug=hermes-hub:1password  -> 404

Root cause was in app.services.federation_live.hermes_origin_skill_md; this
file tests the ROUTE-level fix (app/install_routes.py's new federated-ref
branch) end-to-end through a real FastAPI TestClient. All network calls are
mocked — no test here hits GitHub.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, FederationHubSkill


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture):
    conn = engine_fixture.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn)
    session = Session()
    yield session
    session.close()
    txn.rollback()
    conn.close()


@pytest.fixture()
def master_client(db_session, monkeypatch):
    from app.config import settings
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, headers={"x-api-key": settings.API_KEY}, raise_server_exceptions=True)


def _mk_hub_row(db, slug, repo=None, path=None, origin_url=None):
    row = FederationHubSkill(
        slug=slug,
        title=slug,
        source="hermes-hub",
        repo=repo,
        path=path,
        origin_url=origin_url,
    )
    db.add(row)
    db.commit()
    return row


def test_hermes_hub_slug_resolves_via_repo_path(master_client, db_session):
    """The exact live-prod repro: hermes-hub:1password must NOT 404, and the
    resolved instruction must come from the real repo/path coordinates."""
    _mk_hub_row(
        db_session,
        "1password",
        repo="NousResearch/claude-code",
        path="optional-skills/security/1password",
    )

    with patch(
        "app.services.federation_hub_install._probe_branch",
        side_effect=lambda repo, path, branch: branch == "main",
    ):
        resp = master_client.get("/api/skills/install?slug=hermes-hub:1password")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "fetch"
    assert body["source"] == "hermes-hub"
    assert "NousResearch/claude-code" in body["url"]
    assert "optional-skills/security/1password" in body["url"]
    assert body["url"].endswith("SKILL.md")
    assert body["install_command"] is not None


def test_hermes_hub_slug_degrades_to_origin_when_unfetchable(master_client, db_session):
    _mk_hub_row(
        db_session,
        "some-skill",
        repo="owner/moved-repo",
        path="old/path",
        origin_url="https://github.com/owner/moved-repo",
    )

    with (
        patch("app.services.federation_hub_install._probe_branch", return_value=False),
        patch("app.services.federation_hub_install._tree_walk_fallback", return_value=None),
    ):
        resp = master_client.get("/api/skills/install?slug=hermes-hub:some-skill")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "origin"
    assert body["url"] == "https://github.com/owner/moved-repo"
    assert body["install_command"] is None


def test_hermes_hub_slug_unknown_slug_404s_honestly(master_client, db_session):
    resp = master_client.get("/api/skills/install?slug=hermes-hub:totally-unknown-slug")
    assert resp.status_code == 404


def test_local_skill_install_unaffected_by_federated_ref_branch(master_client, db_session):
    """CONTRACT SAFETY: a normal local-skill install must still work exactly
    as before — the new federated branch must not intercept slugs without a
    colon."""
    from app.models import Skill, SkillVersion

    sk = Skill(id=uuid4(), slug="normal-local-skill", title="x", is_public=True, tier="free")
    db_session.add(sk)
    db_session.commit()
    db_session.add(SkillVersion(id=uuid4(), skill_id=sk.id, semver="1.0.0", checksum_sha256="a" * 64))
    db_session.commit()

    resp = master_client.get("/api/skills/install?slug=normal-local-skill")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "normal-local-skill"
    assert resp.json()["version"] == "1.0.0"


def test_ext_prefixed_slug_still_routes_to_local_skill_lookup(master_client, db_session):
    """`ext:` is the pre-existing materialized-pointer namespace (a REAL
    local Skill row, per app/services/bundle_external.py) and must NOT be
    swallowed by the new federated-ref branch even though it contains a
    colon-like prefix pattern."""
    resp = master_client.get("/api/skills/install?slug=ext:hermes-hub:doesnotexist")
    # Falls through to the normal local-Skill 404 path (no such Skill row),
    # NOT the federated-ref branch's 404 message.
    assert resp.status_code == 404
    assert "not found in hermes-hub" not in resp.text


# ── bundles_0811 P3 follow-up: the prefix a user was SHOWN must also work ─────
#
# P3 wired only `hermes-hub:<slug>`. But `hermes-hub` is the HUB NAMESPACE, not
# an upstream source — prod's distinct upstream_source values are browse-sh,
# claude-marketplace, clawhub, github, lobehub, official, skills-sh. A search
# card shows `source: "skills-sh"`, so a user types `skills-sh:<slug>` and got a
# bare "Skill not found" for a row we demonstrably hold (the same row resolved
# 200 under the hermes-hub prefix). Slugs are globally unique — 90,605 rows /
# 90,605 distinct slugs — so the prefix is a hint, never a disambiguator.


def test_upstream_source_prefix_resolves_the_same_row_as_hermes_hub(master_client, db_session):
    """`skills-sh:<slug>` must resolve identically to `hermes-hub:<slug>`."""
    _mk_hub_row(
        db_session,
        "skills-sh-getpaseo-paseo-handoff",
        repo="getpaseo/paseo",
        path="skills/handoff",
    )

    with patch(
        "app.services.federation_hub_install._probe_branch",
        side_effect=lambda repo, path, branch: branch == "main",
    ):
        via_source = master_client.get(
            "/api/skills/install?slug=skills-sh:skills-sh-getpaseo-paseo-handoff"
        )
        via_hub = master_client.get(
            "/api/skills/install?slug=hermes-hub:skills-sh-getpaseo-paseo-handoff"
        )

    assert via_source.status_code == 200, via_source.text
    assert via_hub.status_code == 200, via_hub.text
    # Same row, same instruction — the prefix is a hint, not a selector.
    assert via_source.json()["url"] == via_hub.json()["url"]
    assert via_source.json()["kind"] == "fetch"


def test_unknown_prefix_still_404s_as_a_local_skill(master_client, db_session):
    """An unknown prefix must NOT be treated as federated.

    It falls through to the local lookup so the 404 names the real problem
    rather than blaming the federated hub.
    """
    resp = master_client.get("/api/skills/install?slug=not-a-source:whatever")
    assert resp.status_code == 404
    assert "not-a-source:whatever" in resp.json()["detail"]

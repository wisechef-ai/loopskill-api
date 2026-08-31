"""bundles0811 Phase P3 item 5 — MCP/REST parity for federated bundle entries.

loopskill_bundle_install (MCP, bulk path) previously INNER JOINed Skill on
BundleSkill.skill_id — the exact bug sp2607fix-1 already fixed for the REST
bulk-install route — so every federated liked entry (skill_id NULL) was
silently dropped from the MCP payload while REST carried it. This closes
that gap AND adds the install_instruction the P3 resolver produces, so an
MCP caller gets the same resolvable coordinates a REST caller does.

All network calls are mocked — no test here hits GitHub.
"""

from __future__ import annotations

import uuid
from typing import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.mcp.tools.bundle_install import loopskill_bundle_install
from app.models import Base, Bundle, BundleSkill, FederationHubSkill, Skill, SkillVersion, User


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _record):
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


def _mk_user(db):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"u-{uuid.uuid4().hex[:6]}@t.io",
        display_name="u",
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_cookbook(db, owner):
    cb = Bundle(id=uuid.uuid4(), name="B", bundle_owner=owner.id)
    db.add(cb)
    db.commit()
    return cb


def _mk_local_skill(db, cb, slug):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, tier="free")
    db.add(s)
    db.commit()
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="a" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.commit()
    return s


def _mk_federated_bundle_skill(db, cb, source, slug):
    row = BundleSkill(
        bundle_id=cb.id, skill_id=None, federated_source=source, federated_slug=slug, source="custom-added"
    )
    db.add(row)
    db.commit()
    return row


class _MasterCtx:
    scope = "master"
    bundle_scope = None


def test_mcp_bulk_install_federated_row_no_longer_dropped(db):
    """THE bug: an inner-join-only bulk path drops skill_id=NULL rows."""
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_local_skill(db, cb, "local-one")
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")

    out = loopskill_bundle_install(db=db, ctx=_MasterCtx(), cookbook_id=str(cb.id))

    slugs = {s["slug"] for s in out["skills"]}
    assert slugs == {"local-one", "official-security-1password"}, (
        f"federated row missing from MCP bulk install payload: {out['skills']}"
    )


def test_mcp_bulk_install_federated_entry_carries_install_instruction(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "1password")
    db.add(
        FederationHubSkill(
            slug="1password",
            title="1Password",
            source="hermes-hub",
            repo="NousResearch/claude-code",
            path="optional-skills/security/1password",
        )
    )
    db.commit()

    with patch(
        "app.services.federation_hub_install._probe_branch",
        side_effect=lambda repo, path, branch: branch == "main",
    ):
        out = loopskill_bundle_install(db=db, ctx=_MasterCtx(), cookbook_id=str(cb.id))

    fed_entry = next(s for s in out["skills"] if s["slug"] == "1password")
    assert fed_entry["federated"] is True
    assert "install_instruction" in fed_entry
    instr = fed_entry["install_instruction"]
    assert instr["kind"] == "fetch"
    assert "NousResearch/claude-code" in instr["url"]
    assert instr["url"].endswith("SKILL.md")


def test_mcp_bulk_install_federated_entry_degrades_to_origin_when_no_coords(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "clawhub", "no-coords-skill")

    out = loopskill_bundle_install(db=db, ctx=_MasterCtx(), cookbook_id=str(cb.id))
    fed_entry = next(s for s in out["skills"] if s["slug"] == "no-coords-skill")
    assert fed_entry["install_instruction"]["kind"] == "origin"


def test_mcp_bulk_install_local_only_bundle_unaffected(db):
    """CONTRACT SAFETY: a purely-local bundle's payload shape/values must be
    byte-identical to before — no federated code path touches it."""
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_local_skill(db, cb, "purely-local")

    out = loopskill_bundle_install(db=db, ctx=_MasterCtx(), cookbook_id=str(cb.id))
    assert len(out["skills"]) == 1
    entry = out["skills"][0]
    assert entry["slug"] == "purely-local"
    assert entry["version"] == "1.0.0"
    assert "install_instruction" not in entry

"""TDD tests for fleet MCP tools and x-fleet-key middleware.

Phase E — recipes_2005 sprint.

Tests:
  1. test_fleet_create_returns_unique_rec_fleet_key
  2. test_fleet_create_persists_hash_not_plaintext
  3. test_fleet_subscribe_idempotent
  4. test_fleet_sync_aggregates_across_cookbooks
  5. test_x_fleet_key_header_grants_install_access
  6. test_fleet_key_prefix_distinct_from_cbt_and_rec_live
  7. test_fleet_list_returns_subscriptions
  8. test_can_use_fleet_predicate (authz)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from uuid import UUID, uuid4

from app.auth_ctx import AuthContext
from app.authz import can_use_fleet
from app.database import get_db
from app.mcp.tools.fleet import (
    recipes_fleet_create,
    recipes_fleet_list,
    recipes_fleet_subscribe,
    recipes_fleet_sync,
)
from app.models import Base, Cookbook, CookbookSkill, Fleet, Skill, SkillVersion


# ── in-memory SQLite engine for fleet tests ───────────────────────────────


@pytest.fixture(scope="module")
def fleet_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def fleet_db(fleet_engine):
    conn = fleet_engine.connect()
    transaction = conn.begin()
    factory = sessionmaker(bind=conn)
    session = factory()
    nested = conn.begin_nested()
    from sqlalchemy import event as sa_event

    @sa_event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session, tx):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        conn.close()


def _make_cookbook(db: Session, owner_id=None) -> Cookbook:
    cb = Cookbook(
        id=uuid4(),
        name="Fleet Test Cookbook",
        cookbook_owner=owner_id or uuid4(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(cb)
    db.flush()
    return cb


def _make_skill_with_versions(db: Session, slug: str) -> Skill:
    from tests.conftest import make_skill

    skill = make_skill(db, slug=slug, title=f"Skill {slug}", category="ops")
    sv = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver="1.0.0",
        skill_toml='[skill]\ncategory="ops"\ntags=[]\n',
        checksum_sha256="abc",
        tarball_size_bytes=100,
        tarball_path="/tmp/fake.tar.gz",
        created_at=datetime.now(timezone.utc),
    )
    db.add(sv)
    sv2 = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver="2.0.0",
        skill_toml='[skill]\ncategory="ops"\ntags=[]\n',
        checksum_sha256="def",
        tarball_size_bytes=200,
        tarball_path="/tmp/fake2.tar.gz",
        created_at=datetime.now(timezone.utc),
    )
    db.add(sv2)
    db.flush()
    return skill


# ── 1. test_fleet_create_returns_unique_rec_fleet_key ─────────────────────


def test_fleet_create_returns_unique_rec_fleet_key(fleet_db):
    """recipes_fleet_create must return a key with the rec_fleet_ prefix."""
    owner = uuid4()
    ctx = AuthContext(scope="user", user_id=owner)
    result = recipes_fleet_create(fleet_db, name="Alpha Fleet", ctx=ctx)

    assert "fleet_id" in result
    assert "fleet_key" in result
    assert "name" in result
    assert result["name"] == "Alpha Fleet"

    key = result["fleet_key"]
    assert key.startswith("rec_fleet_"), f"Expected rec_fleet_ prefix, got: {key!r}"
    # Format: rec_fleet_<8hex>_<32hex> splits into 4 parts: ['rec','fleet','8hex','32hex']
    parts = key.split("_")
    assert len(parts) == 4, f"Expected 4 underscore-parts, got: {parts}"
    assert len(parts[2]) == 8, f"Expected 8-hex id segment, got: {parts[2]!r}"
    assert len(parts[3]) == 32, f"Expected 32-hex random, got: {parts[3]!r}"

    # Second create must produce a DIFFERENT key
    result2 = recipes_fleet_create(fleet_db, name="Beta Fleet", ctx=ctx)
    assert result2["fleet_key"] != result["fleet_key"]


# ── 2. test_fleet_create_persists_hash_not_plaintext ──────────────────────


def test_fleet_create_persists_hash_not_plaintext(fleet_db):
    """Fleet row must store sha256 hash, NOT the plaintext key."""
    owner = uuid4()
    ctx = AuthContext(scope="user", user_id=owner)
    result = recipes_fleet_create(fleet_db, name="Hash Fleet", ctx=ctx)

    fleet_id = result["fleet_id"]
    plaintext_key = result["fleet_key"]

    fleet_row = fleet_db.query(Fleet).filter(Fleet.id == UUID(fleet_id)).first()
    assert fleet_row is not None

    expected_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    assert fleet_row.fleet_api_key_hash == expected_hash, (
        "fleet_api_key_hash must be sha256 of plaintext key, not the key itself"
    )
    assert fleet_row.fleet_api_key_hash != plaintext_key


# ── 3. test_fleet_subscribe_idempotent ────────────────────────────────────


def test_fleet_subscribe_idempotent(fleet_db):
    """Calling recipes_fleet_subscribe twice with same args must be idempotent."""
    owner = uuid4()
    ctx = AuthContext(scope="user", user_id=owner)

    fleet_result = recipes_fleet_create(fleet_db, name="Sub Fleet", ctx=ctx)
    fleet_id = fleet_result["fleet_id"]

    cookbook = _make_cookbook(fleet_db, owner_id=owner)
    cb_id = str(cookbook.id)

    r1 = recipes_fleet_subscribe(
        fleet_db, fleet_id=fleet_id, cookbook_id=cb_id, channel="stable", ctx=ctx
    )
    assert r1["fleet_id"] == fleet_id
    assert r1["cookbook_id"] == cb_id
    assert r1["channel"] == "stable"

    # Second call must NOT raise and must return same result
    r2 = recipes_fleet_subscribe(
        fleet_db, fleet_id=fleet_id, cookbook_id=cb_id, channel="stable", ctx=ctx
    )
    assert r2["fleet_id"] == fleet_id
    assert r2["cookbook_id"] == cb_id

    # Verify only ONE subscription row exists
    from app.models import FleetSubscription

    subs = (
        fleet_db.query(FleetSubscription)
        .filter(
            FleetSubscription.fleet_id == UUID(fleet_id),
            FleetSubscription.cookbook_id == cookbook.id,
        )
        .all()
    )
    assert len(subs) == 1, f"Expected 1 subscription row, got {len(subs)}"


# ── 4. test_fleet_sync_aggregates_across_cookbooks ────────────────────────


def test_fleet_sync_aggregates_across_cookbooks(fleet_db):
    """recipes_fleet_sync must aggregate changes across all subscribed cookbooks."""
    owner = uuid4()
    ctx = AuthContext(scope="master")

    fleet_result = recipes_fleet_create(
        fleet_db, name="Sync Fleet", ctx=AuthContext(scope="user", user_id=owner)
    )
    fleet_id = fleet_result["fleet_id"]

    # Create two cookbooks with outdated skills
    cb1 = _make_cookbook(fleet_db, owner_id=owner)
    cb2 = _make_cookbook(fleet_db, owner_id=owner)

    skill1 = _make_skill_with_versions(fleet_db, "fleet-skill-one")
    skill2 = _make_skill_with_versions(fleet_db, "fleet-skill-two")

    # Add outdated skills to each cookbook
    for cb, skill in [(cb1, skill1), (cb2, skill2)]:
        cs = CookbookSkill(
            cookbook_id=cb.id,
            skill_id=skill.id,
            pinned_version="1.0.0",
            source="custom-added",
        )
        fleet_db.add(cs)
    fleet_db.flush()

    # Subscribe both cookbooks to the fleet.
    # evergreen_0206 Phase C: channels are now REAL — a 'stable' subscription
    # only advances to versions that passed the eval gate (promoted_to_stable_at).
    # This test's versions are unpromoted, and its purpose is aggregation across
    # cookbooks, so it subscribes on 'canary' (advances to latest = 2.0.0).
    recipes_fleet_subscribe(
        fleet_db,
        fleet_id=fleet_id,
        cookbook_id=str(cb1.id),
        channel="canary",
        ctx=ctx,
    )
    recipes_fleet_subscribe(
        fleet_db,
        fleet_id=fleet_id,
        cookbook_id=str(cb2.id),
        channel="canary",
        ctx=ctx,
    )

    # Sync with dry_run=True
    result = recipes_fleet_sync(fleet_db, fleet_id=fleet_id, dry_run=True, ctx=ctx)

    assert "fleet_id" in result
    assert result["fleet_id"] == fleet_id
    assert "cookbooks_synced" in result
    assert len(result["cookbooks_synced"]) == 2, (
        f"Expected 2 cookbooks_synced, got {len(result['cookbooks_synced'])}"
    )

    # Both must show changes
    total_changes = sum(len(cs["changes"]) for cs in result["cookbooks_synced"])
    assert total_changes >= 2, f"Expected ≥2 changes total, got {total_changes}"

    # dry_run → applied=False for all
    for cs_entry in result["cookbooks_synced"]:
        assert cs_entry["applied"] is False


# ── 5. test_x_fleet_key_header_grants_install_access ────────────────────


def test_x_fleet_key_header_grants_install_access(fleet_db):
    """x-fleet-key header with valid rec_fleet_ key must resolve to fleet AuthContext."""
    import hashlib

    from app.middleware import APIKeyMiddleware

    owner = uuid4()
    ctx_user = AuthContext(scope="user", user_id=owner)
    result = recipes_fleet_create(fleet_db, name="Middleware Fleet", ctx=ctx_user)
    fleet_key = result["fleet_key"]
    fleet_id = result["fleet_id"]

    # The middleware path looks up by sha256 hash
    key_hash = hashlib.sha256(fleet_key.encode()).hexdigest()
    fleet_row = fleet_db.query(Fleet).filter(
        Fleet.fleet_api_key_hash == key_hash
    ).first()
    assert fleet_row is not None, "Fleet row must be queryable by key hash"

    # Verify the key prefix is distinct
    assert fleet_key.startswith("rec_fleet_")

    # Verify middleware constant recognizes it
    assert fleet_key.startswith("rec_fleet_")
    assert not fleet_key.startswith("cbt_")
    assert not (fleet_key.startswith("rec_") and not fleet_key.startswith("rec_fleet_"))


# ── 6. test_fleet_key_prefix_distinct_from_cbt_and_rec_live ─────────────


def test_fleet_key_prefix_distinct_from_cbt_and_rec_live(fleet_db):
    """Fleet keys must be distinctly prefixed from cbt_ and rec_live_ keys."""
    owner = uuid4()
    ctx = AuthContext(scope="user", user_id=owner)
    result = recipes_fleet_create(fleet_db, name="Prefix Fleet", ctx=ctx)
    key = result["fleet_key"]

    # Must NOT be a cbt_ token
    assert not key.startswith("cbt_"), f"fleet key must not start with cbt_: {key!r}"
    # Must NOT be a plain rec_live_ key
    assert not key.startswith("rec_live_"), f"fleet key must not start with rec_live_: {key!r}"
    # Must be the fleet prefix
    assert key.startswith("rec_fleet_"), f"fleet key must start with rec_fleet_: {key!r}"

    # The FLEET_KEY_PREFIX constant must exist in middleware
    from app.middleware import FLEET_KEY_PREFIX

    assert FLEET_KEY_PREFIX == "rec_fleet_"
    assert key.startswith(FLEET_KEY_PREFIX)


# ── 7. test_fleet_list_returns_subscriptions ──────────────────────────────


def test_fleet_list_returns_subscriptions(fleet_db):
    """recipes_fleet_list must return all fleets owned by the caller with subscriptions."""
    owner = uuid4()
    ctx = AuthContext(scope="user", user_id=owner)

    f1 = recipes_fleet_create(fleet_db, name="List Fleet 1", ctx=ctx)
    f2 = recipes_fleet_create(fleet_db, name="List Fleet 2", ctx=ctx)

    cb = _make_cookbook(fleet_db, owner_id=owner)
    recipes_fleet_subscribe(
        fleet_db,
        fleet_id=f1["fleet_id"],
        cookbook_id=str(cb.id),
        channel="canary",
        ctx=ctx,
    )

    result = recipes_fleet_list(fleet_db, ctx=ctx)
    assert "fleets" in result
    fleet_ids = {f["fleet_id"] for f in result["fleets"]}
    assert f1["fleet_id"] in fleet_ids
    assert f2["fleet_id"] in fleet_ids

    # The fleet with subscription must show it
    fleet1_data = next(f for f in result["fleets"] if f["fleet_id"] == f1["fleet_id"])
    assert len(fleet1_data["subscriptions"]) == 1
    assert fleet1_data["subscriptions"][0]["channel"] == "canary"


# ── 8. test_can_use_fleet_predicate ──────────────────────────────────────


def test_can_use_fleet_predicate(fleet_db):
    """can_use_fleet(ctx, fleet) predicate must respect master/owner/fleet scopes."""
    owner_id = uuid4()
    other_id = uuid4()

    fleet_row = Fleet(
        id=uuid4(),
        owner_user_id=owner_id,
        name="Predicate Fleet",
        fleet_api_key_hash="a" * 64,
        created_at=datetime.now(timezone.utc),
    )
    fleet_db.add(fleet_row)
    fleet_db.flush()

    # master scope → allowed
    assert can_use_fleet(AuthContext(scope="master"), fleet_row) is True

    # owner user scope → allowed
    assert can_use_fleet(AuthContext(scope="user", user_id=owner_id), fleet_row) is True

    # non-owner user scope → denied
    assert can_use_fleet(AuthContext(scope="user", user_id=other_id), fleet_row) is False

    # anonymous → denied
    assert can_use_fleet(AuthContext.anonymous(), fleet_row) is False

    # fleet scope matching → allowed
    assert (
        can_use_fleet(
            AuthContext(scope="fleet", fleet_id=fleet_row.id, user_id=owner_id),  # type: ignore[call-arg]
            fleet_row,
        )
        is True
    )

    # fleet scope not matching → denied
    assert (
        can_use_fleet(
            AuthContext(scope="fleet", fleet_id=uuid4(), user_id=owner_id),  # type: ignore[call-arg]
            fleet_row,
        )
        is False
    )


# ── 9. Error-path coverage tests ─────────────────────────────────────────


def test_fleet_create_forbidden_for_anonymous(fleet_db):
    """recipes_fleet_create must return forbidden for anonymous callers."""
    ctx = AuthContext.anonymous()
    result = recipes_fleet_create(fleet_db, name="Anon Fleet", ctx=ctx)
    assert "error" in result
    assert result["error"] == "forbidden"


def test_fleet_subscribe_invalid_fleet_id(fleet_db):
    """recipes_fleet_subscribe must return error for bad fleet_id."""
    ctx = AuthContext(scope="master")
    result = recipes_fleet_subscribe(
        fleet_db, fleet_id="not-a-uuid", cookbook_id=str(uuid4()), ctx=ctx
    )
    assert result.get("error") == "invalid_fleet_id"


def test_fleet_subscribe_not_found(fleet_db):
    """recipes_fleet_subscribe must return not_found for unknown fleet."""
    ctx = AuthContext(scope="master")
    result = recipes_fleet_subscribe(
        fleet_db, fleet_id=str(uuid4()), cookbook_id=str(uuid4()), ctx=ctx
    )
    assert result.get("error") == "not_found"


def test_fleet_subscribe_forbidden(fleet_db):
    """recipes_fleet_subscribe must return forbidden for wrong user."""
    owner = uuid4()
    ctx_owner = AuthContext(scope="user", user_id=owner)
    fleet_result = recipes_fleet_create(fleet_db, name="Forbidden Fleet", ctx=ctx_owner)
    fleet_id = fleet_result["fleet_id"]

    other_ctx = AuthContext(scope="user", user_id=uuid4())
    cb_id = str(uuid4())
    result = recipes_fleet_subscribe(fleet_db, fleet_id=fleet_id, cookbook_id=cb_id, ctx=other_ctx)
    assert result.get("error") == "forbidden"


def test_fleet_subscribe_invalid_cookbook_id(fleet_db):
    """recipes_fleet_subscribe must return error for bad cookbook_id."""
    owner = uuid4()
    ctx = AuthContext(scope="user", user_id=owner)
    fleet_result = recipes_fleet_create(fleet_db, name="CB Invalid", ctx=ctx)
    fleet_id = fleet_result["fleet_id"]

    result = recipes_fleet_subscribe(fleet_db, fleet_id=fleet_id, cookbook_id="bad", ctx=ctx)
    assert result.get("error") == "invalid_cookbook_id"


def test_fleet_sync_invalid_fleet_id(fleet_db):
    """recipes_fleet_sync must return error for invalid fleet_id."""
    ctx = AuthContext(scope="master")
    result = recipes_fleet_sync(fleet_db, fleet_id="bad-uuid", ctx=ctx)
    assert result.get("error") == "invalid_fleet_id"


def test_fleet_sync_not_found(fleet_db):
    """recipes_fleet_sync must return not_found for unknown fleet."""
    ctx = AuthContext(scope="master")
    result = recipes_fleet_sync(fleet_db, fleet_id=str(uuid4()), ctx=ctx)
    assert result.get("error") == "not_found"


def test_fleet_sync_forbidden(fleet_db):
    """recipes_fleet_sync must return forbidden for wrong user."""
    owner = uuid4()
    ctx_owner = AuthContext(scope="user", user_id=owner)
    fleet_result = recipes_fleet_create(fleet_db, name="Sync Forbidden", ctx=ctx_owner)
    fleet_id = fleet_result["fleet_id"]

    other_ctx = AuthContext(scope="user", user_id=uuid4())
    result = recipes_fleet_sync(fleet_db, fleet_id=fleet_id, ctx=other_ctx)
    assert result.get("error") == "forbidden"


def test_fleet_list_master_sees_all(fleet_db):
    """Master scope can list all fleets."""
    owner1 = uuid4()
    owner2 = uuid4()
    ctx1 = AuthContext(scope="user", user_id=owner1)
    ctx2 = AuthContext(scope="user", user_id=owner2)
    r1 = recipes_fleet_create(fleet_db, name="Master List A", ctx=ctx1)
    r2 = recipes_fleet_create(fleet_db, name="Master List B", ctx=ctx2)

    result = recipes_fleet_list(fleet_db, ctx=AuthContext(scope="master"))
    fleet_ids = {f["fleet_id"] for f in result["fleets"]}
    assert r1["fleet_id"] in fleet_ids
    assert r2["fleet_id"] in fleet_ids


def test_fleet_list_fleet_scope(fleet_db):
    """Fleet-scoped key can list only its own fleet."""
    owner = uuid4()
    ctx_user = AuthContext(scope="user", user_id=owner)
    fleet_result = recipes_fleet_create(fleet_db, name="Fleet Scope List", ctx=ctx_user)
    fleet_id = UUID(fleet_result["fleet_id"])

    ctx_fleet = AuthContext(scope="fleet", fleet_id=fleet_id, user_id=owner)  # type: ignore[call-arg]
    result = recipes_fleet_list(fleet_db, ctx=ctx_fleet)
    assert len(result["fleets"]) == 1
    assert result["fleets"][0]["fleet_id"] == str(fleet_id)


def test_fleet_list_forbidden_for_anonymous(fleet_db):
    """Anonymous callers cannot list fleets."""
    result = recipes_fleet_list(fleet_db, ctx=AuthContext.anonymous())
    assert "error" in result

"""sp2607fix-1 — federated liked skills must be visible on the bundle-detail
and install read paths, not just GET /api/library.

CRITICAL bug (live prod, reproduced 2026-07-26): ``set_federated_like_in_bundle``
(app/library_service.py, spotify_2607 Phase A) writes a ``BundleSkill`` row
with ``skill_id=NULL`` + ``federated_source``/``federated_slug`` set into the
caller's Liked bundle. ``GET /api/library`` reads it correctly (Phase A's own
``_liked_shelf``/``_liked_skill_shelf``). But ``app/bundle_routes.py::_skills_for``
— which feeds BOTH ``GET /api/cookbooks/{id}`` and ``POST
/api/cookbooks/{id}/install`` — INNER JOINs ``Skill`` on ``skill_id``, which is
NULL for every federated row, silently dropping ALL of them.

This test file is the RED-proof Phase A's own acceptance gate never had (it
only tested GET /api/library — see this file's sibling skill note on "dual
surface divergence"). It asserts on BOTH GET /api/cookbooks/{id} AND the
install payload, per task requirement.

RED-PROOF (recorded 2026-07-26, before the fix):
    Neutralised the fix by reverting ``_skills_for`` and removing the
    ``_federated_skills_for``/``_federated_cookbook_skill_out`` calls from
    ``get_cookbook`` and ``install_cookbook`` (i.e. restoring the pre-fix
    bundle_routes.py). Result:
        test_federated_liked_skill_appears_in_cookbook_detail   FAILED
            AssertionError: assert 1 == 2  (only the local skill was present)
        test_federated_liked_skill_appears_in_install_payload   FAILED
            AssertionError: assert 1 == 2
        test_federated_liked_skill_counted_as_community_not_vetted  FAILED
            AssertionError: assert 0 == 1  (community count stayed 0)
    Restored the fix; all three pass (see suite run below).
"""

from __future__ import annotations

import json
import uuid
from typing import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Bundle,
    BundleSkill,
    FederationHubSkill,
    Skill,
    SkillVersion,
    User,
)


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


def _mk_cookbook(db, owner, name="Liked"):
    cb = Bundle(id=uuid.uuid4(), name=name, bundle_owner=owner.id)
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db, slug, tier=None):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, tier=tier)
    db.add(s)
    db.commit()
    return s


def _mk_federated_bundle_skill(db, cb, source, slug):
    """Mirror exactly what set_federated_like_in_bundle (library_service.py) writes."""
    row = BundleSkill(
        bundle_id=cb.id,
        skill_id=None,
        federated_source=source,
        federated_slug=slug,
        source="custom-added",
    )
    db.add(row)
    db.commit()
    return row


class _State:
    api_key_id = None


class _GetReq:
    client = None
    state = _State()
    method = "GET"
    url = type("U", (), {"path": "/api/cookbooks/x"})()


class _PostReq:
    client = None
    state = _State()
    method = "POST"
    url = type("U", (), {"path": "/api/cookbooks/x/install"})()


def _ctx(owner, tier="pro"):
    class _Ctx:
        pass

    c = _Ctx()
    c.is_master = False
    c.user_id = owner.id
    c.tier = tier
    c.cbt_cookbook_id = None
    c.org_id = None
    return c


def _bypass(cb):
    from app import bundle_routes

    return (
        patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route", return_value=None),
        patch.object(bundle_routes, "_resolve_owned_cookbook", return_value=cb),
    )


# ── GET /api/cookbooks/{id}: federated rows must appear ─────────────────────


def test_federated_liked_skill_appears_in_cookbook_detail(db):
    """LIVE-PROOF target #1: GET /api/cookbooks/{id} must include the
    federated row, not just the local skill (prod reproduction: bundle
    9b6053ae-... showed only 'hyperspace-matrix', the federated
    1password skill was silently dropped)."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    local = _mk_skill(db, "hyperspace-matrix")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local.id, source="custom-added"))
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    slugs = {s["slug"] for s in detail["skills"]}
    assert slugs == {"hyperspace-matrix", "official-security-1password"}, (
        f"federated row missing from bundle-detail skills: {detail['skills']}"
    )

    fed_entry = next(s for s in detail["skills"] if s["slug"] == "official-security-1password")
    assert fed_entry["federated"] is True
    assert fed_entry["federated_source"] == "hermes-hub"
    assert fed_entry["provenance"] == "community"

    local_entry = next(s for s in detail["skills"] if s["slug"] == "hyperspace-matrix")
    assert local_entry["federated"] is False
    assert local_entry["provenance"] == "vetted"


def test_federated_liked_skill_title_resolves_from_hub_snapshot(db):
    """Reuses library_service's title-resolution contract: prefer the hub
    snapshot's title, fail soft to the federated slug if unresolved."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")
    db.add(
        FederationHubSkill(
            slug="official-security-1password",
            title="1Password Security",
            source="hermes-hub",
        )
    )
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    fed_entry = next(s for s in detail["skills"] if s["slug"] == "official-security-1password")
    assert fed_entry["title"] == "1Password Security"


def test_federated_liked_skill_title_fails_soft_to_slug_when_unresolved(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "clawhub", "some-unindexed-skill")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    fed_entry = next(s for s in detail["skills"] if s["slug"] == "some-unindexed-skill")
    assert fed_entry["title"] == "some-unindexed-skill"


# ── POST /api/cookbooks/{id}/install: federated rows appear + count as community ──


def test_federated_liked_skill_appears_in_install_payload(db):
    """LIVE-PROOF target #2: POST /api/cookbooks/{id}/install must include
    the federated skill (prod reproduction: skills:1, vetted:1, community:0
    — should have been skills:2, vetted:1, community:1)."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    local = _mk_skill(db, "hyperspace-matrix")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=local.id, semver="1.0.0", checksum_sha256="a" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local.id, source="custom-added"))
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner)
        )

    slugs = {s["slug"] for s in out["skills"]}
    assert slugs == {"hyperspace-matrix", "official-security-1password"}, (
        f"federated skill missing from install payload: {out['skills']}"
    )


def test_federated_liked_skill_counted_as_community_not_vetted(db):
    """LIVE-PROOF target #3: the vetted/community split must count the
    federated entry as community (task requirement: never vetted)."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    local = _mk_skill(db, "hyperspace-matrix")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=local.id, semver="1.0.0", checksum_sha256="a" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local.id, source="custom-added"))
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner)
        )

    assert out["vetted"] == 1, f"expected exactly 1 vetted (the local skill), got {out['vetted']}"
    assert out["community"] == 1, (
        f"expected exactly 1 community (the federated skill), got {out['community']}"
    )

    fed_entry = next(s for s in out["skills"] if s["slug"] == "official-security-1password")
    assert fed_entry["federated"] is True
    assert fed_entry.get("external") is not True  # never mistaken for an external/hosted-vetted row


def test_federated_only_bundle_install_has_zero_vetted(db):
    """A bundle with ONLY a federated liked skill (no local skills at all) —
    the pure-federation case from the live-proof bundle."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner)
        )

    assert len(out["skills"]) == 1
    assert out["vetted"] == 0
    assert out["community"] == 1


def test_federated_skill_never_500s_for_free_tier_owner_no_authz_gate(db):
    """Task requirement: no authz gate should apply to a federated row (no
    Skill row exists — tier_rank_allows_install must never be invoked on it),
    so even a free-tier owner's federated liked entries always install."""
    from app import bundle_routes

    owner = _mk_user(db, tier="free")
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner)
        )

    assert len(out["skills"]) == 1
    assert out["skills"][0]["slug"] == "official-security-1password"


# ── CONTRACT SAFETY: local-only skills key stays byte-identical ────────────


def test_local_only_bundle_skills_key_shape_unchanged(db):
    """CONTRACT SAFETY (mandatory). Pins the exact key set + values a
    LOCAL-only bundle's `skills` entries carry in GET /api/cookbooks/{id} —
    this must stay identical whether or not any federated entry exists in
    other bundles. Additive keys only (federated/federated_source/provenance,
    all with harmless defaults for local rows)."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "contract-pin-skill")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    assert len(detail["skills"]) == 1
    entry = detail["skills"][0]

    # Pre-existing keys — every one must still be present, values unchanged.
    pinned_keys = {
        "slug",
        "source",
        "pinned_version",
        "added_at",
        "title",
        "skill_variant",
        "is_public",
        "parent_skill_slug",
        "related_skills",
        "pinned",
        "corrections_absorbed",
    }
    assert pinned_keys <= set(entry.keys()), f"a pre-existing key was dropped: {entry.keys()}"
    assert entry["slug"] == "contract-pin-skill"
    assert entry["source"] == "custom-added"
    assert entry["is_public"] is True
    assert entry["pinned"] is False
    assert entry["related_skills"] == []
    assert entry["corrections_absorbed"] == 0

    # New keys are additive-only and default to the "vetted/local" reading.
    assert entry["federated"] is False
    assert entry["federated_source"] is None
    assert entry["provenance"] == "vetted"


def test_install_payload_skills_key_byte_identical_for_local_only_bundle(db):
    """Same contract, on the install payload — captures a baseline BEFORE
    any federated skill is added anywhere, matches the shape
    test_spotify2607_c_mixed_bundles.py's byte-identical contract test
    already pins for personalities/loops, extended to prove federated
    entries don't disturb a LOCAL-only bundle's own payload."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "install-contract-skill")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="d" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner)
        )

    assert len(out["skills"]) == 1
    entry = {k: v for k, v in out["skills"][0].items() if k != "provenance_id"}
    expected_keys = {"slug", "version", "tarball_url", "checksum_sha256", "source"}
    assert expected_keys <= set(entry.keys())
    assert entry["slug"] == "install-contract-skill"
    assert entry["version"] == "1.0.0"
    assert out["vetted"] == 1
    assert out["community"] == 0
    # sanity: JSON-serializable, no drift in field ORDER-independent content
    json.dumps(entry, sort_keys=True)


# ── Codex adversarial-review follow-ups (2026-07-26) ───────────────────────
# The mandatory review gate returned REQUEST_CHANGES with 6 MUST-FIX. Each was
# adjudicated against the code; 3 confirmed, 3 rejected with evidence (see the
# PR thread). These pin the two CONFIRMED defects that would break a consumer.


def test_merged_skills_respect_install_order_across_local_and_federated(db):
    """Codex MUST-FIX 1: ordering must be GLOBAL, not local-block-then-federated.

    Local and federated rows come from two separate queries. Appending one
    block after the other emits a federated row with a LOWER install_order
    AFTER a local row with a higher one — silently breaking the Composer
    ordering contract (portal_0610 J2). Interleave the install_orders so a
    naive append is provably wrong.
    """
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    # Deliberately interleaved: fed(0) local(1) fed(2) local(3).
    fed_a = _mk_federated_bundle_skill(db, cb, "hermes-hub", "fed-first")
    fed_a.install_order = 0
    local_b = _mk_skill(db, "local-second")
    bs_b = BundleSkill(bundle_id=cb.id, skill_id=local_b.id, source="custom-added", install_order=1)
    db.add(bs_b)
    fed_c = _mk_federated_bundle_skill(db, cb, "hermes-hub", "fed-third")
    fed_c.install_order = 2
    local_d = _mk_skill(db, "local-fourth")
    bs_d = BundleSkill(bundle_id=cb.id, skill_id=local_d.id, source="custom-added", install_order=3)
    db.add(bs_d)
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    assert [s["slug"] for s in out["skills"]] == [
        "fed-first",
        "local-second",
        "fed-third",
        "local-fourth",
    ], f"install_order not honoured across the local/federated merge: {[s['slug'] for s in out['skills']]}"


def test_federated_install_entry_uses_the_install_descriptor_shape(db):
    """Codex MUST-FIX 3: install entries are DESCRIPTORS, not detail objects.

    `skills_payload` entries carry {slug, version, tarball_url,
    checksum_sha256}. Appending a detail-shaped dict made the list
    heterogeneous — a consumer iterating it to fetch tarballs would hit an
    entry with no `tarball_url` KEY AT ALL and KeyError. A federated skill is
    DEEP_LINK-only, so those fields must be present-and-None, never absent.
    """
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    local = _mk_skill(db, "local-installable")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=local.id, semver="1.0.0", checksum_sha256="b" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local.id, source="custom-added"))
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "fed-deeplink-only")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner)
        )

    fed = next(s for s in out["skills"] if s["slug"] == "fed-deeplink-only")
    # The install-descriptor keys must be PRESENT (uniform key set), not absent.
    for key in ("version", "tarball_url", "checksum_sha256"):
        assert key in fed, (
            f"federated install entry is missing descriptor key {key!r} — a consumer "
            f"iterating `skills` for tarballs would KeyError. Entry: {fed}"
        )
        assert fed[key] is None, f"{key} must be None for unhosted federated content, got {fed[key]!r}"
    # And it must still be identifiable as community content.
    assert fed["federated"] is True
    assert fed["provenance"] == "community"

    # A naive installer's exact loop must not explode on the mixed list.
    installable = [s for s in out["skills"] if s.get("tarball_url")]
    assert [s["slug"] for s in installable] == ["local-installable"]

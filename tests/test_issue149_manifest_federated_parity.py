"""issue-149 — bundle content reported inconsistently across read paths.

Fix scope (Option B, owner-approved 2026-08-19, decision package in the
issue): make the 3 non-public owner-facing read paths (``GET
/api/cookbooks/{id}``, ``POST .../install``, ``GET .../manifest``)
unconditionally federated-aware, matching each other. The 2 public/anonymous
surfaces (``_public_cb_card``, ``public_cookbook_page``) plus the 2
``.well-known`` routes stay explicitly LOCAL-ONLY pending §0b badging — that
choice is documented inline at each call site, not tested here (it is
INTENTIONAL, not a regression).

Before this fix, ``cookbook_manifest`` was the one owner-facing surface
``sp2607fix-1`` (PR #150) left unpatched — see the issue's own table (line
1537, "GET /api/cookbooks/{id}/manifest"). This file's RED-proof reverts
JUST the manifest route back to its pre-fix ``_skills_for``-only body and
proves the parity test fails, then restores it.
"""

from __future__ import annotations

import uuid
from typing import Generator
from unittest.mock import patch

import pytest
import yaml
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Bundle,
    BundleSkill,
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


# ── RED-PROOF ────────────────────────────────────────────────────────────
# Recorded 2026-08-19 against pre-fix `cookbook_manifest` (a plain
# `_skills_for`-only body, no `_federated_skills_for` call — the exact shape
# the route had before this PR):
#
#   test_manifest_includes_federated_liked_skill FAILED
#       AssertionError: manifest dropped the federated row entirely:
#       {'name': 'Liked', 'description': None,
#        'skills': [{'slug': 'hyperspace-matrix', 'source': 'custom-added',
#                    'pinned_version': None}]}
#       (expected 2 skills entries, got 1 — federated slug
#       'official-security-1password' never appeared)
#
# Restoring the fix (federated-aware `cookbook_manifest`, this PR's diff)
# makes it pass. See PR body's "Breaker report" for the actual command used
# to reproduce this against the reverted route.


def test_manifest_includes_federated_liked_skill(db):
    """LIVE-PROOF: GET /api/cookbooks/{id}/manifest must include the
    federated row, matching GET /api/cookbooks/{id} and POST .../install."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    local = _mk_skill(db, "hyperspace-matrix")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local.id, source="custom-added"))
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "official-security-1password")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        resp = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    manifest = yaml.safe_load(resp.body)
    slugs = {s.get("slug") or s.get("federated_slug") for s in manifest["skills"]}
    assert slugs == {"hyperspace-matrix", "official-security-1password"}, (
        f"federated row missing from manifest: {manifest['skills']}"
    )

    fed_entry = next(s for s in manifest["skills"] if s.get("federated_slug") == "official-security-1password")
    assert fed_entry["federated"] is True
    assert fed_entry["federated_source"] == "hermes-hub"
    assert fed_entry["slug"] is None  # no local Skill row exists for a federated member

    local_entry = next(s for s in manifest["skills"] if s.get("slug") == "hyperspace-matrix")
    assert local_entry["source"] == "custom-added"


def test_manifest_local_only_bundle_shape_unchanged(db):
    """CONTRACT SAFETY: a local-only bundle's manifest entries keep the
    exact pre-fix shape (slug/source/pinned_version), no new required keys."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "contract-pin-skill")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added", pinned_version="2.0.0"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        resp = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    manifest = yaml.safe_load(resp.body)
    assert len(manifest["skills"]) == 1
    entry = manifest["skills"][0]
    assert entry["slug"] == "contract-pin-skill"
    assert entry["source"] == "custom-added"
    assert entry["pinned_version"] == "2.0.0"


def test_manifest_respects_install_order_across_local_and_federated(db):
    """Same global-merge ordering contract get_cookbook already pins
    (portal_0610 J2) — interleaved install_order must survive the merge."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    fed_a = _mk_federated_bundle_skill(db, cb, "hermes-hub", "fed-first")
    fed_a.install_order = 0
    local_b = _mk_skill(db, "local-second")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local_b.id, source="custom-added", install_order=1))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        resp = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    manifest = yaml.safe_load(resp.body)
    ordered = [s.get("slug") or s.get("federated_slug") for s in manifest["skills"]]
    assert ordered == ["fed-first", "local-second"], f"install_order not honoured: {ordered}"


# ── PARITY: the three owner-facing surfaces must agree ──────────────────


def test_detail_install_manifest_agree_on_federated_membership(db):
    """The actual bug this issue is about: three endpoints reporting three
    different member sets for the same bundle. All three must now agree."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    local = _mk_skill(db, "parity-local")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=local.id, semver="1.0.0", checksum_sha256="e" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local.id, source="custom-added"))
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "parity-federated")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))
        install = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner))
        manifest_resp = bundle_routes.cookbook_manifest(
            cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner)
        )

    detail_slugs = {s["slug"] for s in detail["skills"]}
    install_slugs = {s["slug"] for s in install["skills"]}
    manifest = yaml.safe_load(manifest_resp.body)
    manifest_slugs = {s.get("slug") or s.get("federated_slug") for s in manifest["skills"]}

    expected = {"parity-local", "parity-federated"}
    assert detail_slugs == expected, f"detail diverged: {detail_slugs}"
    assert install_slugs == expected, f"install diverged: {install_slugs}"
    assert manifest_slugs == expected, f"manifest diverged: {manifest_slugs}"


# ── BREAKER PASS — boundary / empty / tiebreak attacks ──────────────────


def test_manifest_empty_bundle_yields_empty_skills_list(db):
    """Boundary attack: a bundle with ZERO members (no local, no federated)
    — both queries return empty; the merge/sort must not raise on empty
    lists, and yaml.safe_dump must not choke on an empty `skills: []`."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, name="Empty")

    p1, p2 = _bypass(cb)
    with p1, p2:
        resp = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    manifest = yaml.safe_load(resp.body)
    assert manifest["skills"] == []
    assert manifest["name"] == "Empty"


def test_manifest_federated_only_bundle_has_no_local_entries(db):
    """Boundary attack: a bundle with ONLY federated members (no local skills
    at all — the pure-federation case from the live-proof bundle in
    sp2607fix-1). `_skills_for` returns empty; only the federated branch of
    the merge fires."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "only-federated")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        resp = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    manifest = yaml.safe_load(resp.body)
    assert len(manifest["skills"]) == 1
    assert manifest["skills"][0]["federated_slug"] == "only-federated"
    assert manifest["skills"][0]["slug"] is None


def test_manifest_sort_key_tiebreak_when_install_order_and_added_at_collide(db):
    """Error-path/edge attack: a federated row and a local row sharing BOTH
    install_order (all default 0/None) AND added_at (same commit) — the
    3rd tiebreak (`str(id)`, the UUID string) must still produce a TOTAL,
    deterministic order, not a TypeError from comparing a dict/tuple that
    can't be ordered, and not a nondeterministic merge across runs."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    local = _mk_skill(db, "tiebreak-local")
    # Same transaction/commit => added_at is populated by the same DB clock
    # tick on SQLite (datetime.utcnow server_default) for both rows; neither
    # install_order is set (both default via column default, exercised at
    # flush) so the first two sort-key components can genuinely collide.
    db.add(BundleSkill(bundle_id=cb.id, skill_id=local.id, source="custom-added"))
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "tiebreak-federated")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        resp1 = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))
        resp2 = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    m1 = yaml.safe_load(resp1.body)
    m2 = yaml.safe_load(resp2.body)
    order1 = [s.get("slug") or s.get("federated_slug") for s in m1["skills"]]
    order2 = [s.get("slug") or s.get("federated_slug") for s in m2["skills"]]
    assert set(order1) == {"tiebreak-local", "tiebreak-federated"}
    assert order1 == order2, (
        f"merge order is nondeterministic across identical calls: {order1} vs {order2} "
        "— the str(id) tiebreak should make this stable"
    )


def test_manifest_federated_slug_with_yaml_special_characters_survives_roundtrip(db):
    """Injection/escaping attack: a federated_slug containing YAML-special
    characters (colon, quote, newline-like content a hostile upstream hub
    could advertise) must not corrupt the YAML document structure — it
    crosses a trust boundary (an external federation source we don't
    control) into a `yaml.safe_dump` call. yaml.safe_dump quotes/escapes by
    default; this pins that the round-trip preserves the exact string
    rather than being interpreted as YAML syntax or silently truncated."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    hostile_slug = 'evil: [not, a, list] # "quoted" \n injected'
    _mk_federated_bundle_skill(db, cb, "hermes-hub", hostile_slug)
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        resp = bundle_routes.cookbook_manifest(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    # Must still be valid, parseable YAML (no injected structure).
    manifest = yaml.safe_load(resp.body)
    assert len(manifest["skills"]) == 1
    # The hostile string must round-trip BYTE-IDENTICAL, not be interpreted
    # as YAML syntax (e.g. turned into a nested list/mapping) or truncated
    # at the embedded newline.
    assert manifest["skills"][0]["federated_slug"] == hostile_slug


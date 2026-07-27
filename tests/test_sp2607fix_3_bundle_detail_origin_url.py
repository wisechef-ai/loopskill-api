"""sp2607fix-3 — bundle DETAIL must expose ``origin_url`` for federated rows.

Symmetry bug found by render-verifying the live Liked bundle page (2026-07-27,
Adam's own bundle 7bbe6076). ``POST /api/bundles/{id}/install`` already emits
``origin_url`` for every federated entry — an installer cannot fetch content we
do not host without it. ``GET /api/bundles/{id}`` resolved the SAME
``FederationHubSkill`` row (it needs it for the title) and then dropped the URL
on the floor.

Consequence, verified in a real browser against prod: the portal's bundle-detail
table had no upstream link to render, so every federated row degraded to a
``/browse?type=skills&q=<slug>`` search — a second-best link for a URL the
server already had in hand. Two read surfaces resolving one hub row disagreed
about what that row says.

This is the same defect CLASS as sp2607fix-1 (one surface taught about
federation, its sibling not), which is why it gets its own RED-proof rather
than a silent field addition.

RED-PROOF (recorded 2026-07-27, before the fix):
    Neutralised by reverting ``_federated_cookbook_skill_out`` to its 2-arg
    form (dropping the ``origin_url`` parameter and the ``CookbookSkillOut``
    field), i.e. restoring bundle_routes.py as of 147b251. Result:

        test_federated_bundle_detail_exposes_origin_url                  FAILED
            KeyError: 'origin_url'
        test_federated_detail_and_install_agree_on_origin_url            FAILED
            KeyError: 'origin_url'
        test_local_skill_detail_entry_has_null_origin_url                FAILED
            KeyError: 'origin_url'

    Restored the fix; all 5 pass.
"""

from __future__ import annotations

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
    User,
)


@pytest.fixture(scope="module")
def engine_fixture():
    """In-memory SQLite engine shared by this module's tests."""
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
    """Per-test session rolled back so tests never leak state into each other."""
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


def _mk_skill(db, slug):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, tier="free")
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


ORIGIN = "https://github.com/dietrichgebert/ponytail/tree/main/skills/ponytail"


def test_federated_bundle_detail_exposes_origin_url(db):
    """The bug: detail resolved the hub row for its title but dropped the URL."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "ponytail")
    db.add(
        FederationHubSkill(
            slug="ponytail",
            title="ponytail",
            source="hermes-hub",
            origin_url=ORIGIN,
        )
    )
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    entry = next(s for s in detail["skills"] if s["slug"] == "ponytail")
    assert entry["origin_url"] == ORIGIN
    # The discriminators must still be intact — this is additive, not a reshape.
    assert entry["federated"] is True
    assert entry["provenance"] == "community"


def test_federated_detail_and_install_agree_on_origin_url(db):
    """Two surfaces reading ONE hub row must never disagree about it.

    This is the actual regression guard: sp2607fix-1 taught install about
    origin_url and left detail behind. Pin them together so a future change to
    either one cannot silently re-open the gap.
    """
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "hermes-hub", "ponytail")
    db.add(FederationHubSkill(slug="ponytail", title="ponytail", source="hermes-hub", origin_url=ORIGIN))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))
        install = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_PostReq(), db=db, ctx=_ctx(owner)
        )

    detail_entry = next(s for s in detail["skills"] if s["slug"] == "ponytail")
    install_entry = next(s for s in install["skills"] if s["slug"] == "ponytail")
    assert detail_entry["origin_url"] == install_entry["origin_url"] == ORIGIN


def test_federated_origin_url_is_none_when_hub_row_unresolvable(db):
    """Fail SOFT. Never fabricate a URL for an unindexed federated slug."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_federated_bundle_skill(db, cb, "clawhub", "some-unindexed-skill")
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    entry = next(s for s in detail["skills"] if s["slug"] == "some-unindexed-skill")
    assert entry["origin_url"] is None
    # Title still fails soft to the slug — unchanged sp2607fix-1 behaviour.
    assert entry["title"] == "some-unindexed-skill"


def test_local_skill_detail_entry_has_null_origin_url(db):
    """A LOCAL skill has a first-party detail page, so origin_url stays None.

    Guards against a future change that back-fills origin_url for hosted
    skills and sends the portal off-site for content we actually own.
    """
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    skill = _mk_skill(db, "hyperspace-matrix")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    entry = next(s for s in detail["skills"] if s["slug"] == "hyperspace-matrix")
    assert entry["origin_url"] is None
    assert entry["federated"] is False
    assert entry["provenance"] == "vetted"


def test_local_detail_entry_key_set_is_additive_only(db):
    """Contract pin (AGENTS.md rule): additive keys are safe, reshapes are not.

    ``origin_url`` must be a NEW key on the local entry — every pre-existing
    key keeps its name and value semantics.
    """
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    skill = _mk_skill(db, "hyperspace-matrix")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))

    entry = next(s for s in detail["skills"] if s["slug"] == "hyperspace-matrix")
    required = {
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
        "federated",
        "federated_source",
        "provenance",
        "origin_url",
    }
    assert required.issubset(set(entry.keys()))
    assert entry["slug"] == "hyperspace-matrix"
    assert entry["source"] == "custom-added"

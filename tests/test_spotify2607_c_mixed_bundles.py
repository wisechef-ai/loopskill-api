"""spotify_2607 Phase C — bundles hold skills + personalities + loops,
installable in one pass.

Deletion pass (musk-5-step): BundlePersonality and BundleCompositeLoop
already exist with prod rows (see app/models.py:2513/2540) and the READ path
(_artifacts_for / liked_library shelves) already serves them. This sprint
ONLY adds the missing WRITE routes (POST/DELETE .../{personalities|loops}/
{slug}) and extends install_cookbook's payload additively. No new tables,
no new route MODULE (the existing `_h` router in bundle_routes.py is
dual-mounted at /api/bundles and /api/cookbooks already, so new handlers
registered there get the compat-alias symmetry for free — verified by
test_loopskill_bundle_surface_symmetry.py).

Deleted requirement: the MCP `loopskill_bundle_install` tool's payload is
NOT extended in this phase (only the REST /install contract, which is what
every acceptance gate names). MCP parity is a tracked fast-follow — adding
it here would touch a module no acceptance gate requires and widen the
diff for zero required test coverage.

CONTRACT RISK (byte-identical `skills` key): a baseline `skills` payload is
captured BEFORE any personalities/loops exist on the bundle, then re-captured
AFTER a personality+loop are added and installed. The two must be byte
identical — this is `test_skills_key_byte_identical_after_mixed_install`.
"""

from __future__ import annotations

import json
import uuid
from typing import Generator
from unittest.mock import ANY, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Bundle,
    BundleCompositeLoop,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    Personality,
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


def _mk_cookbook(db, owner, name="CB"):
    cb = Bundle(id=uuid.uuid4(), name=name, bundle_owner=owner.id)
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db, slug, tier=None):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, tier=tier)
    db.add(s)
    db.commit()
    return s


def _mk_personality(db, slug, tier=None):
    p = Personality(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        system_prompt="Be helpful.",
        tier=tier,
    )
    db.add(p)
    db.commit()
    return p


def _mk_loop(db, slug, tier=None):
    cl = CompositeLoop(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        schedule="1h",
        skills=[],
        connectors=[],
        subagents_config={},
        verifier_slug="check",
        state_seed={},
        prompt="do the thing",
        tier=tier,
    )
    db.add(cl)
    db.commit()
    return cl


class _State:
    api_key_id = None


class _Req:
    client = None
    state = _State()
    method = "POST"
    url = type("U", (), {"path": "/api/cookbooks/x/personalities/y"})()


class _GetReq:
    client = None
    state = _State()
    method = "GET"
    url = type("U", (), {"path": "/api/cookbooks/x"})()


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
    """Patch the two mandatory-per-AGENTS.md gates to no-ops / direct resolution."""
    from app import bundle_routes

    return (
        patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route", return_value=None),
        patch.object(bundle_routes, "_resolve_owned_cookbook", return_value=cb),
    )


# ── mutation routes: add/remove personality + loop ─────────────────────────


def test_add_personality_to_cookbook_and_it_appears_in_detail(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    p = _mk_personality(db, "helpful-persona")

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.add_personality_to_cookbook(
            cookbook_id=str(cb.id), slug=p.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
    assert out["slug"] == "helpful-persona"
    assert out["added"] is True

    row = (
        db.query(BundlePersonality)
        .filter(BundlePersonality.bundle_id == cb.id, BundlePersonality.personality_id == p.id)
        .first()
    )
    assert row is not None

    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))
    assert any(row["slug"] == "helpful-persona" for row in detail["personalities"])


def test_add_loop_to_cookbook_and_it_appears_in_detail(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    cl = _mk_loop(db, "daily-brief-loop")

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.add_loop_to_cookbook(
            cookbook_id=str(cb.id), slug=cl.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
    assert out["slug"] == "daily-brief-loop"
    assert out["added"] is True

    row = (
        db.query(BundleCompositeLoop)
        .filter(BundleCompositeLoop.bundle_id == cb.id, BundleCompositeLoop.composite_loop_id == cl.id)
        .first()
    )
    assert row is not None

    with p1, p2:
        detail = bundle_routes.get_cookbook(cookbook_id=str(cb.id), request=_GetReq(), db=db, ctx=_ctx(owner))
    assert any(row["slug"] == "daily-brief-loop" for row in detail["composite_loops"])


def test_adding_personality_twice_is_idempotent_not_a_500(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    p = _mk_personality(db, "idem-persona")

    p1, p2 = _bypass(cb)
    with p1, p2:
        first = bundle_routes.add_personality_to_cookbook(
            cookbook_id=str(cb.id), slug=p.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
        second = bundle_routes.add_personality_to_cookbook(
            cookbook_id=str(cb.id), slug=p.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
    assert first["added"] is True
    assert second["added"] is False
    count = (
        db.query(BundlePersonality)
        .filter(BundlePersonality.bundle_id == cb.id, BundlePersonality.personality_id == p.id)
        .count()
    )
    assert count == 1


def test_remove_personality_from_cookbook(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    p = _mk_personality(db, "removable-persona")
    db.add(BundlePersonality(bundle_id=cb.id, personality_id=p.id))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.remove_personality_from_cookbook(
            cookbook_id=str(cb.id), slug=p.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
    assert out["deleted"] is True
    row = (
        db.query(BundlePersonality)
        .filter(BundlePersonality.bundle_id == cb.id, BundlePersonality.personality_id == p.id)
        .first()
    )
    assert row is None


def test_remove_loop_from_cookbook(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    cl = _mk_loop(db, "removable-loop")
    db.add(BundleCompositeLoop(bundle_id=cb.id, composite_loop_id=cl.id))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.remove_loop_from_cookbook(
            cookbook_id=str(cb.id), slug=cl.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
    assert out["deleted"] is True
    row = (
        db.query(BundleCompositeLoop)
        .filter(BundleCompositeLoop.bundle_id == cb.id, BundleCompositeLoop.composite_loop_id == cl.id)
        .first()
    )
    assert row is None


def test_remove_unknown_personality_404s(db):
    from fastapi import HTTPException

    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    p1, p2 = _bypass(cb)
    with p1, p2, pytest.raises(HTTPException) as exc_info:
        bundle_routes.remove_personality_from_cookbook(
            cookbook_id=str(cb.id), slug="ghost", request=_Req(), db=db, ctx=_ctx(owner)
        )
    assert exc_info.value.status_code == 404


def test_add_personality_calls_mandatory_cbt_scope_guard(db):
    """AGENTS.md mandate: every new cookbook mutation route MUST call
    _enforce_cbt_scope_for_cookbook_route. This asserts the call actually
    happens (not just that a bypassed test passes)."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    p = _mk_personality(db, "scope-guard-persona")

    with (
        patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route") as guard,
        patch.object(bundle_routes, "_resolve_owned_cookbook", return_value=cb),
    ):
        bundle_routes.add_personality_to_cookbook(
            cookbook_id=str(cb.id), slug=p.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
    guard.assert_called_once_with(ANY, str(cb.id))


def test_add_loop_calls_mandatory_cbt_scope_guard(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    cl = _mk_loop(db, "scope-guard-loop")

    with (
        patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route") as guard,
        patch.object(bundle_routes, "_resolve_owned_cookbook", return_value=cb),
    ):
        bundle_routes.add_loop_to_cookbook(
            cookbook_id=str(cb.id), slug=cl.slug, request=_Req(), db=db, ctx=_ctx(owner)
        )
    guard.assert_called_once_with(ANY, str(cb.id))


# ── install_cookbook: one-pass payload, byte-identical skills key ──────────


def test_install_cookbook_returns_skills_personalities_and_loops(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "install-skill")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="a" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    p = _mk_personality(db, "install-persona")
    db.add(BundlePersonality(bundle_id=cb.id, personality_id=p.id))
    cl = _mk_loop(db, "install-loop")
    db.add(BundleCompositeLoop(bundle_id=cb.id, composite_loop_id=cl.id))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))

    assert len(out["skills"]) == 1
    assert out["skills"][0]["slug"] == "install-skill"
    assert any(row["slug"] == "install-persona" for row in out["personalities"])
    assert any(row["slug"] == "install-loop" for row in out["loops"])
    assert isinstance(out["vetted"], int)
    assert isinstance(out["community"], int)


def test_skills_key_byte_identical_after_mixed_install(db):
    """The pre-existing `skills` key must be byte-identical to the captured
    baseline (skills-only bundle) even after personalities+loops are added.
    This is the ponytail_0724-lesson contract test.

    provenance_id is EXCLUDED from the comparison: it is a pre-existing,
    intentionally-fresh-per-call mint (see app/services/provenance.py) with
    no relationship to this PR's change — asserting on its literal value
    would make this test flaky by design, not a real contract check. Every
    OTHER field (slug/version/tarball_url/checksum_sha256/source) — the
    fields that actually identify "what would install" — must match
    exactly, in the same order.
    """
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "byte-identical-skill")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="2.0.0", checksum_sha256="b" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        baseline = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner)
        )

    def _strip_provenance(skills):
        return [{k: v for k, v in entry.items() if k != "provenance_id"} for entry in skills]

    baseline_skills_json = json.dumps(_strip_provenance(baseline["skills"]), sort_keys=True)

    p = _mk_personality(db, "byte-identical-persona")
    db.add(BundlePersonality(bundle_id=cb.id, personality_id=p.id))
    cl = _mk_loop(db, "byte-identical-loop")
    db.add(BundleCompositeLoop(bundle_id=cb.id, composite_loop_id=cl.id))
    db.commit()

    with p1, p2:
        after = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))
    after_skills_json = json.dumps(_strip_provenance(after["skills"]), sort_keys=True)

    assert baseline_skills_json == after_skills_json, (
        f"skills key drifted:\nBASELINE: {baseline_skills_json}\nAFTER:    {after_skills_json}"
    )
    # New keys (personalities/loops/vetted/community) are ALWAYS present now
    # (empty list / 0 when the bundle has none) — a consistent shape is
    # easier for clients to code against than a conditionally-appearing key,
    # and it costs nothing: the pre-existing `skills` key (asserted above) is
    # untouched either way.
    assert (
        set(baseline.keys())
        == set(after.keys())
        == {
            "cookbook_id",
            "name",
            "skills",
            "personalities",
            "loops",
            "vetted",
            "community",
        }
    )


# ── tier gating: over-tier artifacts of EVERY type are skipped ─────────────


def test_over_tier_skill_skipped_in_install(db):
    from app import bundle_routes

    owner = _mk_user(db, tier="free")
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "pro-only-skill", tier="pro")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="c" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))
    assert out["skills"] == []


def test_over_tier_personality_skipped_in_install(db):
    from app import bundle_routes

    owner = _mk_user(db, tier="free")
    cb = _mk_cookbook(db, owner)
    p = _mk_personality(db, "pro-only-persona", tier="pro")
    db.add(BundlePersonality(bundle_id=cb.id, personality_id=p.id))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))
    assert out["personalities"] == [], "over-tier personality leaked into install payload"


def test_over_tier_loop_skipped_in_install(db):
    from app import bundle_routes

    owner = _mk_user(db, tier="free")
    cb = _mk_cookbook(db, owner)
    cl = _mk_loop(db, "pro-only-loop", tier="pro")
    db.add(BundleCompositeLoop(bundle_id=cb.id, composite_loop_id=cl.id))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))
    assert out["loops"] == [], "over-tier loop leaked into install payload"


def test_under_tier_owner_still_gets_free_personality_and_loop(db):
    """Sanity: the gate is a SKIP, not a blanket empty — free-tier artifacts
    still install for a free owner."""
    from app import bundle_routes

    owner = _mk_user(db, tier="free")
    cb = _mk_cookbook(db, owner)
    p = _mk_personality(db, "free-persona", tier="free")
    db.add(BundlePersonality(bundle_id=cb.id, personality_id=p.id))
    cl = _mk_loop(db, "free-loop", tier="free")
    db.add(BundleCompositeLoop(bundle_id=cb.id, composite_loop_id=cl.id))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))
    assert any(row["slug"] == "free-persona" for row in out["personalities"])
    assert any(row["slug"] == "free-loop" for row in out["loops"])


def test_vetted_community_counts_reflect_external_skills(db):
    """§0b — the install payload must separate vetted from community/federated
    counts so a fleet operator can see what they're pulling."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "vetted-skill")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="d" * 64))
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.commit()

    # Materialize a federated pointer skill directly (mirrors what
    # materialize_external_skill actually writes) rather than hitting the
    # network in a unit test.
    ext = Skill(
        id=uuid.uuid4(),
        slug="ext:github-marketing:copywriting",
        title="copywriting",
        is_public=False,
        skill_variant="external",
        tier="external",
    )
    db.add(ext)
    db.commit()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=ext.id, source="custom-added"))
    db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        out = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))

    assert out["community"] == 1
    assert out["vetted"] == 1


def test_multi_artifact_install_payload_is_deterministic_and_byte_identical(db):
    """Codex R2 finding (nondeterministic ordering): a bundle with MULTIPLE
    personalities and MULTIPLE loops must emit them in a stable order across
    repeat installs, and the full personalities+loops payload must be
    byte-identical on re-run. The single-artifact
    test_skills_key_byte_identical_after_mixed_install cannot catch ordering
    drift because a 1-element list is always "ordered".

    Deterministic order is added_at ASC (mirrors _skills_for's contract at
    bundle_routes.py:348). Ties (same added_at microsecond) break by the
    artifact's UUID, which is stable per-row.

    NOTE (bug-class 7, green-on-SQLite): SQLite returns rows in rowid/insertion
    order even WITHOUT order_by, so this test passes on SQLite regardless of
    the fix. The order_by clauses in bundle_routes.py are required for
    PostgreSQL (prod), which does NOT guarantee row order without ORDER BY.
    This test still proves the byte-identical-on-re-run contract on SQLite;
    the fix itself is the prod protection.

    We set added_at EXPLICITLY (descending insertion order, reversed from the
    slug sort) rather than sleeping — a time.sleep would advance the wall clock
    and make the sibling test_skills_key_byte_identical_after_mixed_install
    flaky (its signed-token timestamp would straddle a second boundary).
    """
    from datetime import datetime, timedelta, timezone

    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Insert in REVERSE of the expected emit order, with explicit descending
    # added_at. If order_by is absent, SQLite returns insertion order
    # (gamma, beta, alpha); if present, it returns added_at-ASC (alpha, beta,
    # gamma). Either way the byte-identical-on-re-run contract holds; the
    # order assertion below documents the intended contract.
    for i, slug in enumerate(["gamma-persona", "beta-persona", "alpha-persona"]):
        p = _mk_personality(db, slug)
        db.add(
            BundlePersonality(
                bundle_id=cb.id,
                personality_id=p.id,
                added_at=base - timedelta(seconds=i),
            )
        )
        db.commit()
    for i, slug in enumerate(["gamma-loop", "beta-loop", "alpha-loop"]):
        cl = _mk_loop(db, slug)
        db.add(
            BundleCompositeLoop(
                bundle_id=cb.id,
                composite_loop_id=cl.id,
                added_at=base - timedelta(seconds=i),
            )
        )
        db.commit()

    p1, p2 = _bypass(cb)
    with p1, p2:
        first = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner))
    with p1, p2:
        second = bundle_routes.install_cookbook(
            cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_ctx(owner)
        )

    first_p = [row["slug"] for row in first["personalities"]]
    second_p = [row["slug"] for row in second["personalities"]]
    first_l = [row["slug"] for row in first["loops"]]
    second_l = [row["slug"] for row in second["loops"]]

    # Deterministic order = added_at ascending = alpha, beta, gamma here
    # (we inserted gamma-first with the LATEST added_at).
    assert first_p == ["alpha-persona", "beta-persona", "gamma-persona"], (
        f"personalities not in added_at-asc order: {first_p}"
    )
    assert first_l == ["alpha-loop", "beta-loop", "gamma-loop"], f"loops not in added_at-asc order: {first_l}"
    # Byte-identical on re-run (the contract the byte-identical test pins for
    # skills, extended to the new artifact arrays).
    assert first_p == second_p, f"personalities order drifted on re-run: {first_p} vs {second_p}"
    assert first_l == second_l, f"loops order drifted on re-run: {first_l} vs {second_l}"

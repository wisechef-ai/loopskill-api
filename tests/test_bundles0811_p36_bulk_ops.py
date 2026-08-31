"""bundles0811 P3.6 — bulk bundle operations (manage-at-scale surfaces).

Lock #9 (plan §0): "the platform's job is MANAGING LARGE NUMBERS of skills /
bundles / personalities / MCPs — not curating a catalog." With ~154,000
indexed skills, one-click-per-skill does not scale.

Covers:
  - bulk add of >=25 MIXED local+federated entries in ONE call, asserting one
    operation (one commit / one lock-sync) and correct membership
  - partial-failure: one bad entry does not lose the good ones, and per-item
    results are returned
  - idempotency: repeating the bulk add does not duplicate members or error
  - authorization: a non-owner is rejected (reuses the SAME predicate every
    other bundle-mutation route uses — no new authz surface)
  - bulk remove, including the federated branch and unknown-slug no-op
  - the TIMED >=25-skill acceptance-gate demo itself
"""

from __future__ import annotations

import time
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
    Skill,
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


def _mk_user(db, tier="pro_plus"):
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


class _State:
    api_key_id = None


class _Req:
    client = None
    state = _State()
    method = "POST"
    url = type("U", (), {"path": "/api/cookbooks/x/skills/bulk"})()


def _ctx(owner, tier="pro_plus"):
    class _Ctx:
        pass

    c = _Ctx()
    c.is_master = False
    c.user_id = owner.id
    c.tier = tier
    c.cbt_cookbook_id = None
    c.org_id = None
    return c


def _bypass_scope_guard():
    """Bypass ONLY the cbt-scope guard (AGENTS.md mandatory call) — leaves
    `_resolve_owned_cookbook` REAL so authz tests exercise the actual
    ownership predicate, not a stub."""
    from app import bundle_routes

    return patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route", return_value=None)


# ── bulk add: >=25 mixed local+federated in ONE operation ──────────────────


def test_bulk_add_25_mixed_local_and_federated_one_operation(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    # 15 local skills + 12 federated identities = 27 items (>= 25 gate).
    local_slugs = [f"local-skill-{i}" for i in range(15)]
    for slug in local_slugs:
        _mk_skill(db, slug)
    fed_pairs = [("hermes-hub", f"fed-skill-{i}") for i in range(12)]

    items = [{"slug": s, "source": "custom-added"} for s in local_slugs] + [
        {"federated_source": src, "federated_slug": slug} for src, slug in fed_pairs
    ]
    assert len(items) == 27

    with _bypass_scope_guard():
        out = bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 27
    assert out["added"] == 27
    assert out["failed"] == 0
    assert len(out["results"]) == 27
    assert all(r["ok"] for r in out["results"])

    # Correct membership: exactly one BundleSkill row per item, right shape.
    local_rows = (
        db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id, BundleSkill.skill_id.isnot(None)).all()
    )
    fed_rows = (
        db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id, BundleSkill.skill_id.is_(None)).all()
    )
    assert len(local_rows) == 15
    assert len(fed_rows) == 12
    assert {(r.federated_source, r.federated_slug) for r in fed_rows} == set(fed_pairs)


def test_bulk_add_is_one_commit_not_n(db):
    """'One operation' isn't just semantic — assert sync_bundle_lock (the
    expensive per-mutation resolver) fires exactly ONCE for a 30-item batch,
    not once per item. This is what makes it a bulk endpoint rather than a
    for-loop over the single-item route."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    for i in range(30):
        _mk_skill(db, f"count-skill-{i}")
    items = [{"slug": f"count-skill-{i}"} for i in range(30)]

    call_count = {"n": 0}
    real_sync = bundle_routes.sync_bundle_lock

    def _counting_sync(*args, **kwargs):
        call_count["n"] += 1
        return real_sync(*args, **kwargs)

    with _bypass_scope_guard(), patch.object(bundle_routes, "sync_bundle_lock", side_effect=_counting_sync):
        bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert call_count["n"] == 1, f"expected exactly one sync_bundle_lock call, got {call_count['n']}"


# ── TIMED acceptance-gate demo: create a bundle of >=25 skills in ONE op ───


def test_timed_bulk_create_25_skill_bundle_acceptance_gate(db, capsys):
    """The plan's own acceptance gate: 'Create a bundle of >=25 skills in ONE
    operation, TIMED and recorded.' Prints the measured wall-clock time so a
    CI log / PR-body paste can cite it."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    slugs = [f"timed-demo-skill-{i}" for i in range(30)]
    for slug in slugs:
        _mk_skill(db, slug)
    items = [{"slug": s} for s in slugs]

    with _bypass_scope_guard():
        start = time.perf_counter()
        out = bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
        elapsed = time.perf_counter() - start

    assert out["total"] == 30
    assert out["added"] == 30
    print(
        f"\n[P3.6 acceptance gate] bulk-added {out['total']} skills in ONE operation: {elapsed * 1000:.2f}ms"
    )
    # Sanity ceiling, not a perf assertion — a single-process SQLite unit test
    # doing 30 rows should never take anywhere near 5s; a real regression
    # (e.g. accidentally re-syncing the lock per item) would blow this.
    assert elapsed < 5.0


# ── partial failure: one bad entry does not lose the good ones ─────────────


def test_bulk_add_partial_failure_bad_slug_does_not_lose_good_items(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_skill(db, "good-skill-1")
    _mk_skill(db, "good-skill-2")
    items = [
        {"slug": "good-skill-1"},
        {"slug": "does-not-exist-anywhere"},
        {"slug": "good-skill-2"},
    ]

    with _bypass_scope_guard():
        out = bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 3
    assert out["added"] == 2
    assert out["failed"] == 1
    results_by_identity = {r["identity"]: r for r in out["results"]}
    assert results_by_identity["good-skill-1"]["ok"] is True
    assert results_by_identity["good-skill-2"]["ok"] is True
    assert results_by_identity["does-not-exist-anywhere"]["ok"] is False
    assert results_by_identity["does-not-exist-anywhere"]["error"] == "skill_not_found"

    # The good items actually landed — a failure did NOT roll back the batch.
    rows = db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).all()
    assert {r.skill_id for r in rows} == {
        db.query(Skill).filter(Skill.slug == "good-skill-1").first().id,
        db.query(Skill).filter(Skill.slug == "good-skill-2").first().id,
    }


def test_bulk_add_partial_failure_missing_slug_and_bad_source(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_skill(db, "ok-skill")
    items = [
        {"slug": "ok-skill"},
        {"slug": None},
        {"slug": "ok-skill", "source": "not-a-real-source"},
    ]

    with _bypass_scope_guard():
        out = bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 3
    assert out["failed"] == 2
    errors = [r["error"] for r in out["results"] if not r["ok"]]
    assert "missing_slug" in errors
    assert "invalid_source" in errors


def test_bulk_add_batch_over_max_size_is_a_request_level_422(db):
    """Distinguishes REQUEST-level rejection (batch too large — before any
    item is touched) from item-level partial failure."""
    from app import bundle_routes
    from fastapi import HTTPException

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    items = [{"slug": f"oversized-{i}"} for i in range(bundle_routes.BUNDLE_BULK_MAX_ITEMS + 1)]

    with _bypass_scope_guard(), pytest.raises(HTTPException) as exc_info:
        bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["reason"] == "batch_too_large"


def test_bulk_add_empty_batch_is_422(db):
    from app import bundle_routes
    from fastapi import HTTPException

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    with _bypass_scope_guard(), pytest.raises(HTTPException) as exc_info:
        bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=[]),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
    assert exc_info.value.status_code == 422


# ── idempotency: repeating a bulk add never duplicates or errors ───────────


def test_bulk_add_repeated_is_idempotent_no_duplicate_no_error(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    for i in range(5):
        _mk_skill(db, f"idem-skill-{i}")
    items = [{"slug": f"idem-skill-{i}"} for i in range(5)] + [
        {"federated_source": "hermes-hub", "federated_slug": f"idem-fed-{i}"} for i in range(3)
    ]

    with _bypass_scope_guard():
        first = bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
        second = bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert first["failed"] == 0
    assert second["failed"] == 0, f"repeating the batch must not error: {second['results']}"
    # Second pass: every item is a reactivation, none a fresh add, none a failure.
    assert second["added"] == 0
    assert second["reactivated"] == 8

    total_rows = db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).count()
    assert total_rows == 8, f"repeating the bulk add duplicated rows: {total_rows} != 8"


# ── authorization: a non-owner is rejected (REAL predicate, not bypassed) ──


def test_bulk_add_non_owner_is_rejected(db):
    """Uses the REAL `_resolve_owned_cookbook` (only the cbt-scope guard is
    stubbed) so this exercises the actual `authz.owner_match_within_tenant`
    predicate — the same one every other bundle-mutation route reuses, not a
    new one written for bulk."""
    from app import bundle_routes
    from fastapi import HTTPException

    owner = _mk_user(db)
    stranger = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_skill(db, "someones-skill")
    items = [{"slug": "someones-skill"}]

    with _bypass_scope_guard(), pytest.raises(HTTPException) as exc_info:
        bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(stranger),
        )
    # 404, not 403 — mirrors _resolve_owned_cookbook's existence-leak contract
    # for every other bundle route (see its docstring: "no existence leak").
    assert exc_info.value.status_code == 404

    # Nothing was mutated.
    rows = db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).count()
    assert rows == 0


def test_bulk_remove_non_owner_is_rejected(db):
    from app import bundle_routes
    from fastapi import HTTPException

    owner = _mk_user(db)
    stranger = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "protected-skill")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.commit()

    with _bypass_scope_guard(), pytest.raises(HTTPException) as exc_info:
        bundle_routes.remove_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkRemoveIn(items=[{"slug": "protected-skill"}]),
            request=_Req(),
            db=db,
            ctx=_ctx(stranger),
        )
    assert exc_info.value.status_code == 404

    # Still active — a rejected caller mutated nothing.
    row = db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).first()
    assert row.source == "custom-added"


# ── bulk remove ──────────────────────────────────────────────────────────


def test_bulk_remove_mixed_local_and_federated(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s1 = _mk_skill(db, "remove-me-1")
    s2 = _mk_skill(db, "remove-me-2")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s1.id, source="custom-added"))
    db.add(BundleSkill(bundle_id=cb.id, skill_id=s2.id, source="custom-added"))
    db.add(
        BundleSkill(
            bundle_id=cb.id,
            skill_id=None,
            federated_source="hermes-hub",
            federated_slug="remove-me-fed",
            source="custom-added",
        )
    )
    db.commit()

    items = [
        {"slug": "remove-me-1"},
        {"federated_source": "hermes-hub", "federated_slug": "remove-me-fed"},
    ]
    with _bypass_scope_guard():
        out = bundle_routes.remove_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkRemoveIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 2
    assert out["removed"] == 2
    assert out["failed"] == 0

    rows = {
        (r.skill_id, r.federated_slug): r.source
        for r in db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id)
    }
    assert rows[(s1.id, None)] == "disabled"
    assert rows[(s2.id, None)] == "custom-added"  # untouched
    assert rows[(None, "remove-me-fed")] == "disabled"


def test_bulk_remove_unknown_slug_is_idempotent_no_op_not_a_failure(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    with _bypass_scope_guard():
        out = bundle_routes.remove_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkRemoveIn(items=[{"slug": "never-existed"}]),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
    assert out["removed"] == 1
    assert out["failed"] == 0


# ── filter → bulk-add consumability contract ────────────────────────────────


def test_filter_result_shape_is_directly_bulk_add_consumable(db):
    """P3.6 gate 2: 'filter the federated index by source + license and act
    on the result.' Asserts the filter route's row shape needs ZERO reshaping
    before being handed to BulkSkillItem — the actionability contract."""
    from app.federation_filter_routes import _row_to_bulk_shape
    from app.models import FederationHubSkill
    from app.bundle_routes import BulkSkillItem

    row = FederationHubSkill(
        slug="filter-contract-skill",
        title="Filter Contract Skill",
        source="hermes-hub",
        upstream_source="skills.sh",
        trust_level="community",
        license="MIT",
        tags=["marketing"],
    )
    db.add(row)
    db.commit()

    shaped = _row_to_bulk_shape(row)
    # No reshape needed: construct BulkSkillItem straight from the filter dict.
    item = BulkSkillItem(**{k: v for k, v in shaped.items() if k in {"federated_source", "federated_slug"}})
    assert item.is_federated() is True
    assert item.federated_source == "hermes-hub"
    assert item.federated_slug == "filter-contract-skill"

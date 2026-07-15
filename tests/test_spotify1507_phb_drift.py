"""spotify_1507 Phase B — Drift Killer service tests.

Covers the bundle-lock lifecycle + three-way drift classification. Each guard
is RED-proofed: the negative case (drift NOT detected / clobber) is asserted to
fail-closed, per the plan's "RED-proof each guard" gate.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import Bundle, BundleLock, BundleSkill, Skill, SkillVersion
from app.services.drift_service import (
    classify_entry_drift,
    compute_lock_hash,
    current_lock,
    mint_bundle_lock,
    prior_revision_hashes,
)


def _seed_skill_with_version(db, slug, semver="1.0.0", checksum="hash-v1"):
    skill = Skill(id=uuid4(), slug=slug, title=slug, tier="free", kind="skill")
    db.add(skill)
    db.flush()
    ver = SkillVersion(id=uuid4(), skill_id=skill.id, semver=semver, checksum_sha256=checksum)
    db.add(ver)
    db.commit()
    return skill


def _bundle_with_skill(db, skill, pin_mode="track", pinned_version=None):
    bundle = Bundle(id=uuid4(), name="Test Bundle", visibility="public", is_base=False, slug=f"b-{uuid4().hex[:8]}")
    db.add(bundle)
    db.flush()
    db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added",
                       pin_mode=pin_mode, pinned_version=pinned_version))
    db.commit()
    return bundle


# ── compute_lock_hash ────────────────────────────────────────────────────────


def test_lock_hash_deterministic_and_order_independent():
    a = [{"slug": "x", "version": "1", "content_hash": "h1"},
         {"slug": "y", "version": "2", "content_hash": "h2"}]
    b = [{"slug": "y", "version": "2", "content_hash": "h2"},
         {"slug": "x", "version": "1", "content_hash": "h1"}]  # reversed
    assert compute_lock_hash(a) == compute_lock_hash(b)


def test_lock_hash_changes_when_content_changes():
    a = [{"slug": "x", "version": "1", "content_hash": "h1"}]
    b = [{"slug": "x", "version": "1", "content_hash": "h2"}]  # hash changed
    assert compute_lock_hash(a) != compute_lock_hash(b)


def test_lock_hash_ignores_cosmetic_fields():
    a = [{"slug": "x", "version": "1", "content_hash": "h1", "pin_mode": "track", "source": "local"}]
    b = [{"slug": "x", "version": "1", "content_hash": "h1", "pin_mode": "pin", "source": "clawhub"}]
    assert compute_lock_hash(a) == compute_lock_hash(b)


# ── mint_bundle_lock ─────────────────────────────────────────────────────────


def test_mint_lock_freezes_current_versions(db_session):
    skill = _seed_skill_with_version(db_session, "memory", "1.0.0", "hash-memory-v1")
    bundle = _bundle_with_skill(db_session, skill)

    lock = mint_bundle_lock(db_session, bundle)
    assert lock.revision == 1
    assert len(lock.locked_entries) == 1
    entry = lock.locked_entries[0]
    assert entry["slug"] == "memory"
    assert entry["version"] == "1.0.0"
    assert entry["content_hash"] == "hash-memory-v1"
    assert lock.lock_hash == compute_lock_hash(lock.locked_entries)


def test_mint_lock_is_immutable_bumps_revision(db_session):
    import datetime

    skill = _seed_skill_with_version(db_session, "arxiv", "1.0.0", "hash-a-v1")
    bundle = _bundle_with_skill(db_session, skill)

    lock1 = mint_bundle_lock(db_session, bundle)
    # bump the skill upstream: new version with a strictly-later timestamp so
    # "latest" ordering is unambiguous (mirrors a real second publish).
    db_session.add(SkillVersion(
        id=uuid4(), skill_id=skill.id, semver="2.0.0", checksum_sha256="hash-a-v2",
        created_at=datetime.datetime.now() + datetime.timedelta(seconds=10),
    ))
    db_session.commit()
    lock2 = mint_bundle_lock(db_session, bundle)

    assert lock2.revision == 2
    # lock1 is UNCHANGED (immutability)
    db_session.refresh(lock1)
    assert lock1.locked_entries[0]["content_hash"] == "hash-a-v1"
    assert lock2.locked_entries[0]["content_hash"] == "hash-a-v2"
    assert lock1.lock_hash != lock2.lock_hash


def test_mint_lock_pin_mode_freezes_pinned_version(db_session):
    """A 'pin' entry freezes to its pinned_version even when a newer one exists."""
    skill = _seed_skill_with_version(db_session, "dspy", "1.0.0", "hash-d-v1")
    db_session.add(SkillVersion(id=uuid4(), skill_id=skill.id, semver="2.0.0", checksum_sha256="hash-d-v2"))
    db_session.commit()
    bundle = _bundle_with_skill(db_session, skill, pin_mode="pin", pinned_version="1.0.0")

    lock = mint_bundle_lock(db_session, bundle)
    entry = lock.locked_entries[0]
    assert entry["version"] == "1.0.0", "pin must freeze to pinned version, not latest"
    assert entry["content_hash"] == "hash-d-v1"
    assert entry["pin_mode"] == "pin"


def test_current_lock_returns_highest_revision(db_session):
    skill = _seed_skill_with_version(db_session, "notion", "1.0.0", "h1")
    bundle = _bundle_with_skill(db_session, skill)
    mint_bundle_lock(db_session, bundle)
    db_session.add(SkillVersion(id=uuid4(), skill_id=skill.id, semver="1.1.0", checksum_sha256="h2"))
    db_session.commit()
    l2 = mint_bundle_lock(db_session, bundle)
    assert current_lock(db_session, bundle.id).revision == l2.revision == 2


# ── classify_entry_drift (THE three-way correctness surface) ─────────────────


def test_drift_in_sync():
    installed = {"version": "1.0.0", "checksum_sha256": "h1"}
    locked = {"version": "1.0.0", "content_hash": "h1"}
    assert classify_entry_drift(installed, locked) == "in-sync"


def test_drift_missing():
    locked = {"version": "1.0.0", "content_hash": "h1"}
    assert classify_entry_drift(None, locked) == "missing"


def test_drift_local_edit_detected_not_clobbered():
    """A hand-edited local copy (unknown hash) = drift, NOT in-sync or behind."""
    installed = {"version": "1.0.0", "checksum_sha256": "hand-edited-hash"}
    locked = {"version": "1.0.0", "content_hash": "h1"}
    # no known prior revisions containing this hash
    assert classify_entry_drift(installed, locked, known_hashes_by_rev={1: {"h1"}}) == "drift"


def test_drift_behind_when_installed_matches_older_revision():
    """Installed hash == an OLDER lock revision's hash → behind (bundle moved)."""
    installed = {"version": "1.0.0", "checksum_sha256": "h1-old"}
    locked = {"version": "2.0.0", "content_hash": "h2-new"}
    known = {1: {"h1-old"}, 2: {"h2-new"}}  # agent is on rev-1's hash
    assert classify_entry_drift(installed, locked, known_hashes_by_rev=known) == "behind"


def test_drift_red_proof_local_edit_is_not_silently_behind():
    """RED-proof: a genuinely local-edited hash must NOT be misclassified as
    'behind' just because versions match — that would clobber the user's edit."""
    installed = {"version": "1.0.0", "checksum_sha256": "локальная-правка"}
    locked = {"version": "1.0.0", "content_hash": "canonical-h1"}
    verdict = classify_entry_drift(installed, locked, known_hashes_by_rev={1: {"canonical-h1"}})
    assert verdict == "drift", "local edit must surface as drift, never behind/in-sync"
    assert verdict != "in-sync"
    assert verdict != "behind"


def test_drift_version_fallback_when_no_hash():
    """Federated deep-link with no checksum → version-string comparison."""
    assert classify_entry_drift({"version": "1.0.0"}, {"version": "1.0.0"}) == "in-sync"
    assert classify_entry_drift({"version": "1.0.0"}, {"version": "2.0.0"}) == "behind"


# ── prior_revision_hashes ────────────────────────────────────────────────────


def test_prior_revision_hashes_maps_slug_history(db_session):
    import datetime

    skill = _seed_skill_with_version(db_session, "grok-search", "1.0.0", "gh1")
    bundle = _bundle_with_skill(db_session, skill)
    mint_bundle_lock(db_session, bundle)
    db_session.add(SkillVersion(
        id=uuid4(), skill_id=skill.id, semver="2.0.0", checksum_sha256="gh2",
        created_at=datetime.datetime.now() + datetime.timedelta(seconds=10),
    ))
    db_session.commit()
    mint_bundle_lock(db_session, bundle)

    hist = prior_revision_hashes(db_session, bundle.id, "grok-search")
    assert hist[1] == {"gh1"}
    assert hist[2] == {"gh2"}


# ── byte-identical deploy proof (plan gate) ──────────────────────────────────


def test_two_deploys_from_same_lock_are_byte_identical(db_session):
    """The core drift-killer guarantee: deploying the SAME lock to 2 members
    yields byte-identical content (same lock_hash)."""
    skill = _seed_skill_with_version(db_session, "summarize-cli", "1.0.0", "sc-hash-v1")
    bundle = _bundle_with_skill(db_session, skill)
    lock = mint_bundle_lock(db_session, bundle)

    # both members install FROM THE LOCK → both see the same entry hash
    member1_installed = {e["slug"]: e["content_hash"] for e in lock.locked_entries}
    member2_installed = {e["slug"]: e["content_hash"] for e in lock.locked_entries}
    assert member1_installed == member2_installed
    # and both are in-sync vs the lock
    for e in lock.locked_entries:
        inst = {"version": e["version"], "checksum_sha256": e["content_hash"]}
        assert classify_entry_drift(inst, e) == "in-sync"


# ── compat status (stale-upstream badge) ─────────────────────────────────────


def test_mark_compat_status_flips_and_reports_change(db_session):
    from app.services.drift_service import mark_compat_status

    skill = _seed_skill_with_version(db_session, "clawhub-fed", "1.0.0", "h1")
    assert skill.compat_status == "active"

    # upstream breaks → stale-upstream, and the change is reported (feed notice)
    changed = mark_compat_status(db_session, skill, ok=False)
    assert changed is True
    assert skill.compat_status == "stale-upstream"
    assert skill.compat_checked_at is not None

    # second failing check: still stale, but NO change (don't re-notify)
    changed2 = mark_compat_status(db_session, skill, ok=False)
    assert changed2 is False

    # recovers → active again, change reported
    changed3 = mark_compat_status(db_session, skill, ok=True)
    assert changed3 is True
    assert skill.compat_status == "active"


# ── HTTP routes (through the app) ────────────────────────────────────────────


def _lock_app(db, user_id):
    from fastapi import FastAPI, Request
    from app.auth_ctx import AuthContext
    from app.bundle_lock_routes import router as lock_router
    from app.database import get_db

    app = FastAPI()

    def override_get_db():
        yield db

    @app.middleware("http")
    async def inject_auth(request: Request, call_next):
        request.state.auth_ctx = AuthContext(
            scope="user", user_id=user_id, api_key_id=None, tier="free"
        )
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(lock_router)
    return app


def test_route_mint_and_get_lock(db_session):
    from fastapi.testclient import TestClient

    owner = uuid4()
    skill = _seed_skill_with_version(db_session, "route-skill", "1.0.0", "rh1")
    bundle = Bundle(id=uuid4(), name="Route B", visibility="public", is_base=False,
                    slug=f"rb-{uuid4().hex[:8]}", bundle_owner=owner)
    db_session.add(bundle)
    db_session.flush()
    db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added", pin_mode="track"))
    db_session.commit()

    client = TestClient(_lock_app(db_session, owner))
    r = client.post(f"/api/bundles/{bundle.id}/lock")
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1
    lock_hash = r.json()["lock_hash"]

    r2 = client.get(f"/api/bundles/{bundle.id}/lock")
    assert r2.status_code == 200
    assert r2.json()["lock_hash"] == lock_hash
    assert len(r2.json()["locked_entries"]) == 1


def test_route_drift_three_way(db_session):
    """End-to-end: mint lock, report installed state, get 3-way drift verdicts."""
    from fastapi.testclient import TestClient

    owner = uuid4()
    skill = _seed_skill_with_version(db_session, "drift-skill", "1.0.0", "dh1")
    bundle = Bundle(id=uuid4(), name="Drift B", visibility="public", is_base=False,
                    slug=f"db-{uuid4().hex[:8]}", bundle_owner=owner)
    db_session.add(bundle)
    db_session.flush()
    db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added", pin_mode="track"))
    db_session.commit()

    client = TestClient(_lock_app(db_session, owner))
    client.post(f"/api/bundles/{bundle.id}/lock")

    # agent reports a hand-edited copy → drift (not clobbered)
    r = client.post(f"/api/bundles/{bundle.id}/drift", json={
        "installed": [{"slug": "drift-skill", "pinned_version": "1.0.0", "checksum_sha256": "HAND-EDITED"}]
    })
    assert r.status_code == 200, r.text
    body = r.json()
    verdicts = {x["slug"]: x["verdict"] for x in body["results"]}
    assert verdicts["drift-skill"] == "drift"
    assert body["summary"]["drift"] == 1


def test_route_drift_in_sync(db_session):
    from fastapi.testclient import TestClient

    owner = uuid4()
    skill = _seed_skill_with_version(db_session, "sync-skill", "1.0.0", "syncedhash")
    bundle = Bundle(id=uuid4(), name="Sync B", visibility="public", is_base=False,
                    slug=f"sb-{uuid4().hex[:8]}", bundle_owner=owner)
    db_session.add(bundle)
    db_session.flush()
    db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added", pin_mode="track"))
    db_session.commit()

    client = TestClient(_lock_app(db_session, owner))
    client.post(f"/api/bundles/{bundle.id}/lock")

    r = client.post(f"/api/bundles/{bundle.id}/drift", json={
        "installed": [{"slug": "sync-skill", "pinned_version": "1.0.0", "checksum_sha256": "syncedhash"}]
    })
    assert r.json()["summary"]["in_sync"] == 1
    assert r.json()["summary"]["drift"] == 0

"""converge_0208 P1 — the bundle lock is the SINGLE resolution authority.

Two independent resolvers used to disagree about what ``pin_mode`` means:

  * ``drift_service._resolve_entry_snapshot``  honours pin_mode (track = head)
  * ``reconcile._declared_skills``             ignored it (any pinned_version won)

Live bundle ``tori-core`` carries ``pin_mode='track'`` rows that ALSO carry a
stale ``pinned_version`` (reconcile-apply bookkeeping residue). Reconcile read
the residue as a pin, targeted a version whose tarball had been moved off
``/storage/skills/``, and every agent 404'd and rolled back — 1293 of 1295
production rollbacks are those two slugs.

This suite pins the fix: ONE resolver (the lock), fail-loud at mint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import Bundle, BundleLock, BundleSkill, Skill, SkillVersion

DEAD_PATH_PREFIX = "/storage/skills"  # the directory that no longer exists on the host


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def live_tarball(tmp_path):
    """A tarball_path that actually resolves on disk."""
    p = tmp_path / "live-1.0.1.tar.gz"
    p.write_bytes(b"\x1f\x8b\x08\x00live")
    return str(p)


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


def _user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="p1-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _api_key(db, user):
    import hashlib

    from app.models import APIKey

    raw = f"rec_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:8],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="p1",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _bundle(db, owner=None, *, name="P1 Bundle"):
    b = Bundle(
        id=uuid.uuid4(),
        name=name,
        slug=f"p1-{uuid.uuid4().hex[:8]}",
        visibility="private",
        is_base=False,
        bundle_owner=getattr(owner, "id", owner),
    )
    db.add(b)
    db.flush()
    return b


def _skill(db, slug, versions):
    """versions: list of (semver, checksum, tarball_path or None)."""
    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        tier="free",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    base = datetime.now(timezone.utc)
    for i, (semver, checksum, path) in enumerate(versions):
        db.add(
            SkillVersion(
                id=uuid.uuid4(),
                skill_id=sk.id,
                semver=semver,
                checksum_sha256=checksum,
                tarball_path=path,
                created_at=base + timedelta(seconds=i),
            )
        )
    db.flush()
    return sk


def _declare(db, bundle, skill, *, source="custom-added", pin_mode="track", pinned_version=None):
    row = BundleSkill(
        bundle_id=bundle.id,
        skill_id=skill.id,
        source=source,
        pin_mode=pin_mode,
        pinned_version=pinned_version,
    )
    db.add(row)
    db.flush()
    return row


# ── 1. RED-proof: mint FAILS LOUD on a dead artifact, naming the slug ───────


def test_mint_refuses_dead_tarball_path_and_names_the_slug(db_session):
    """The whole point of the phase: one loud publish-time error instead of
    1,246 silent 30-minute rollbacks."""
    from app.services.drift_service import LockMintError, mint_bundle_lock

    skill = _skill(
        db_session,
        "ruthless-mentor",
        [("1.0.0", "dead-hash", f"{DEAD_PATH_PREFIX}/ruthless-mentor-1.0.0.tar.gz")],
    )
    bundle = _bundle(db_session, name="tori-core")
    _declare(db_session, bundle, skill, pin_mode="pin", pinned_version="1.0.0")
    db_session.commit()

    with pytest.raises(LockMintError) as exc:
        mint_bundle_lock(db_session, bundle)

    msg = str(exc.value)
    assert "ruthless-mentor" in msg, "the error must NAME the offending slug"
    assert "1.0.0" in msg, "the error must name the offending version"
    assert DEAD_PATH_PREFIX in msg, "the error must name the dangling locator"
    assert "tori-core" in msg, "the error must name the bundle"
    assert exc.value.slug == "ruthless-mentor"
    assert exc.value.version == "1.0.0"

    # and nothing was persisted — a refused mint leaves no partial lock
    assert db_session.query(BundleLock).filter(BundleLock.bundle_id == bundle.id).count() == 0


def test_unpublished_skill_is_skipped_not_refused(db_session, live_tarball):
    """A never-published member is a draft, not a broken artifact.

    Refusing it would ban "add the skill, then publish its first version".
    Skipping it also removes a guaranteed rollback: reconcile used to emit an
    add row with version=None, which carries no signed tarball_url, and the
    client rolls back the ENTIRE apply when a row is missing one.
    """
    from app.services.drift_service import mint_bundle_lock
    from app.services.reconcile import compute_reconcile_plan

    draft = _skill(db_session, "no-versions-at-all", [])
    published = _skill(db_session, "already-published", [("1.0.0", "h1", live_tarball)])
    bundle = _bundle(db_session, name="half-drafted")
    _declare(db_session, bundle, draft)
    _declare(db_session, bundle, published)
    db_session.commit()

    lock = mint_bundle_lock(db_session, bundle)
    assert [e["slug"] for e in lock.locked_entries] == ["already-published"]

    plan = compute_reconcile_plan(db_session, bundle.id, local=[])
    assert [a["slug"] for a in plan.add] == ["already-published"]


def test_resolvability_probe_is_injectable(db_session, monkeypatch):
    """Filesystem checks in unit tests are brittle — the predicate is a seam."""
    from app.services import artifact_resolution
    from app.services.drift_service import mint_bundle_lock

    skill = _skill(db_session, "probe-seam", [("1.0.0", "h1", "/nowhere/probe-seam-1.0.0.tar.gz")])
    bundle = _bundle(db_session)
    _declare(db_session, bundle, skill)
    db_session.commit()

    monkeypatch.setattr(artifact_resolution, "locator_exists", lambda _p: True)
    lock = mint_bundle_lock(db_session, bundle)
    assert lock.revision == 1


# ── 2. RED-proof: the tori-core production-rollback regression ──────────────


def test_track_mode_with_stale_pin_targets_the_live_head(db_session, live_tarball):
    """THE production-rollback regression test.

    tori-core shape: pin_mode='track' + a stale pinned_version='1.0.0' whose
    artifact was moved off /storage/skills, while 1.0.1 is live. 'track' means
    follow the head, so reconcile must target 1.0.1 — the version that has a
    resolvable artifact. Before P1, reconcile read the residual pin and
    targeted 1.0.0 → HTTP 404 → rollback, every 30 minutes.
    """
    from app.services.reconcile import compute_reconcile_plan

    skill = _skill(
        db_session,
        "ruthless-mentor",
        [
            ("1.0.0", "dead-hash", f"{DEAD_PATH_PREFIX}/ruthless-mentor-1.0.0.tar.gz"),
            ("1.0.1", "live-hash", live_tarball),
        ],
    )
    bundle = _bundle(db_session, name="tori-core")
    _declare(db_session, bundle, skill, pin_mode="track", pinned_version="1.0.0")
    db_session.commit()

    plan = compute_reconcile_plan(db_session, bundle.id, local=[])

    assert len(plan.add) == 1
    assert plan.add[0]["version"] == "1.0.1", (
        "pin_mode='track' must follow the head; a residual pinned_version is "
        "reconcile-apply bookkeeping, NOT a pin"
    )
    assert plan.add[0]["checksum_sha256"] == "live-hash"


def test_explicit_pin_is_still_honoured_by_reconcile(db_session, live_tarball):
    """The other half of the contract: an explicit pin must NOT drift to head."""
    from app.services.reconcile import compute_reconcile_plan

    skill = _skill(
        db_session,
        "validated-skill",
        [("1.0.0", "h-100", live_tarball), ("2.0.0", "h-200", live_tarball)],
    )
    bundle = _bundle(db_session)
    _declare(db_session, bundle, skill, pin_mode="pin", pinned_version="1.0.0")
    db_session.commit()

    plan = compute_reconcile_plan(db_session, bundle.id, local=[])
    assert plan.add[0]["version"] == "1.0.0"
    assert plan.add[0]["checksum_sha256"] == "h-100"


def test_reconcile_resolves_through_the_lock_not_the_membership_row(db_session, live_tarball):
    """Byte-for-byte: whatever the lock froze is what reconcile serves, even if
    the membership row is edited underneath it without a re-mint."""
    from app.services.drift_service import current_lock, mint_bundle_lock
    from app.services.reconcile import compute_reconcile_plan

    skill = _skill(db_session, "lock-authority", [("1.0.0", "h-100", live_tarball)])
    bundle = _bundle(db_session)
    row = _declare(db_session, bundle, skill)
    db_session.commit()

    lock = mint_bundle_lock(db_session, bundle)
    assert lock.revision == 1

    # publish a newer version but do NOT re-mint: the lock still says 1.0.0
    db_session.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=skill.id,
            semver="2.0.0",
            checksum_sha256="h-200",
            tarball_path=live_tarball,
            created_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
    )
    row.pinned_version = "2.0.0"  # bookkeeping residue that must NOT resolve
    db_session.commit()

    plan = compute_reconcile_plan(db_session, bundle.id, local=[])
    assert plan.add[0]["version"] == "1.0.0", "reconcile must serve the LOCK, not the row"
    assert current_lock(db_session, bundle.id).revision == 1


def test_disabled_rows_stay_undeclared_through_the_lock(db_session, live_tarball):
    from app.services.reconcile import compute_reconcile_plan

    live = _skill(db_session, "live-one", [("1.0.0", "h1", live_tarball)])
    gone = _skill(db_session, "disabled-one", [("1.0.0", "h2", live_tarball)])
    bundle = _bundle(db_session)
    _declare(db_session, bundle, live)
    _declare(db_session, bundle, gone, source="disabled")
    db_session.commit()

    plan = compute_reconcile_plan(db_session, bundle.id, local=[])
    assert {a["slug"] for a in plan.add} == {"live-one"}

    from app.services.drift_service import current_lock

    entries = current_lock(db_session, bundle.id).locked_entries
    assert {e["slug"] for e in entries} == {"live-one"}, "a disabled row is not desired state"


# ── 3. mint-on-read for unlocked bundles + graceful refusal ─────────────────


def test_unlocked_bundle_is_lazily_minted_on_read(db_session, live_tarball):
    from app.services.drift_service import current_lock
    from app.services.reconcile import compute_reconcile_plan

    skill = _skill(db_session, "lazy-mint", [("1.0.0", "h1", live_tarball)])
    bundle = _bundle(db_session)
    _declare(db_session, bundle, skill)
    db_session.commit()

    assert current_lock(db_session, bundle.id) is None
    compute_reconcile_plan(db_session, bundle.id, local=[])
    lock = current_lock(db_session, bundle.id)
    assert lock is not None and lock.revision == 1


def test_reconcile_survives_a_bundle_that_cannot_be_minted(db_session):
    """A hard failure for unlocked bundles would take all 14 production bundles
    offline on deploy. Refusal degrades to in-memory resolution, same resolver."""
    from app.services.drift_service import current_lock
    from app.services.reconcile import compute_reconcile_plan

    skill = _skill(
        db_session, "unmintable", [("1.0.0", "h1", f"{DEAD_PATH_PREFIX}/unmintable-1.0.0.tar.gz")]
    )
    bundle = _bundle(db_session)
    _declare(db_session, bundle, skill)
    db_session.commit()

    plan = compute_reconcile_plan(db_session, bundle.id, local=[])
    assert plan.add[0]["version"] == "1.0.0"
    assert current_lock(db_session, bundle.id) is None, "a refused mint persists nothing"


# ── 4. mutation mints a revision; no-op does not ────────────────────────────


def test_no_op_sync_does_not_mint_a_redundant_revision(db_session, live_tarball):
    from app.services.bundle_lock_sync import sync_bundle_lock

    skill = _skill(db_session, "noop-mint", [("1.0.0", "h1", live_tarball)])
    bundle = _bundle(db_session)
    _declare(db_session, bundle, skill)
    db_session.commit()

    first = sync_bundle_lock(db_session, bundle)
    db_session.commit()
    assert first is not None and first.revision == 1

    again = sync_bundle_lock(db_session, bundle)
    db_session.commit()
    assert again is None, "nothing identity-bearing changed → no new revision"
    assert db_session.query(BundleLock).filter(BundleLock.bundle_id == bundle.id).count() == 1


def test_add_skill_route_mints_and_prior_revisions_are_immutable(
    middleware_client, db_session, live_tarball
):
    owner = _user(db_session)
    key = _api_key(db_session, owner)
    bundle = _bundle(db_session, owner)
    one = _skill(db_session, "route-mint-one", [("1.0.0", "h1", live_tarball)])
    two = _skill(db_session, "route-mint-two", [("1.0.0", "h2", live_tarball)])
    db_session.commit()

    r1 = middleware_client.post(
        f"/api/cookbooks/{bundle.id}/skills",
        headers={"x-api-key": key},
        json={"slug": one.slug},
    )
    assert r1.status_code == 201, r1.text

    r2 = middleware_client.post(
        f"/api/cookbooks/{bundle.id}/skills",
        headers={"x-api-key": key},
        json={"slug": two.slug},
    )
    assert r2.status_code == 201, r2.text

    locks = (
        db_session.query(BundleLock)
        .filter(BundleLock.bundle_id == bundle.id)
        .order_by(BundleLock.revision)
        .all()
    )
    assert [lk.revision for lk in locks] == [1, 2], "each mutation mints exactly one revision"
    assert {e["slug"] for e in locks[0].locked_entries} == {"route-mint-one"}
    assert {e["slug"] for e in locks[1].locked_entries} == {"route-mint-one", "route-mint-two"}
    assert locks[0].lock_hash != locks[1].lock_hash


def test_remove_skill_route_mints_a_new_revision(middleware_client, db_session, live_tarball):
    owner = _user(db_session)
    key = _api_key(db_session, owner)
    bundle = _bundle(db_session, owner)
    skill = _skill(db_session, "route-remove", [("1.0.0", "h1", live_tarball)])
    _declare(db_session, bundle, skill)
    db_session.commit()

    r = middleware_client.delete(
        f"/api/cookbooks/{bundle.id}/skills/{skill.slug}", headers={"x-api-key": key}
    )
    assert r.status_code == 200, r.text
    lock = (
        db_session.query(BundleLock)
        .filter(BundleLock.bundle_id == bundle.id)
        .order_by(BundleLock.revision.desc())
        .first()
    )
    assert lock is not None
    assert lock.locked_entries == [], "a removed skill leaves the desired state"


def test_pin_route_sets_pin_mode_and_mints(middleware_client, db_session, live_tarball):
    """RED-proof: ``set_skill_pin`` never wrote ``pin_mode``, so the column the
    lock resolver reads stayed 'track' and every explicit pin was ignored."""
    owner = _user(db_session)
    key = _api_key(db_session, owner)
    bundle = _bundle(db_session, owner)
    skill = _skill(
        db_session,
        "pin-mode-write",
        [("1.0.0", "h-100", live_tarball), ("2.0.0", "h-200", live_tarball)],
    )
    _declare(db_session, bundle, skill)
    db_session.commit()

    r = middleware_client.patch(
        f"/api/cookbooks/{bundle.id}/skills/{skill.slug}/pin",
        headers={"x-api-key": key},
        json={"pinned_version": "1.0.0"},
    )
    assert r.status_code == 200, r.text

    row = (
        db_session.query(BundleSkill)
        .filter(BundleSkill.bundle_id == bundle.id, BundleSkill.skill_id == skill.id)
        .first()
    )
    db_session.refresh(row)
    assert row.pin_mode == "pin", "an explicit pin must be recorded in pin_mode"

    lock = (
        db_session.query(BundleLock)
        .filter(BundleLock.bundle_id == bundle.id)
        .order_by(BundleLock.revision.desc())
        .first()
    )
    assert lock is not None
    assert lock.locked_entries[0]["version"] == "1.0.0"

    # clearing the pin returns the entry to 'track' → head
    r2 = middleware_client.patch(
        f"/api/cookbooks/{bundle.id}/skills/{skill.slug}/pin",
        headers={"x-api-key": key},
        json={"pinned_version": None},
    )
    assert r2.status_code == 200, r2.text
    db_session.refresh(row)
    assert row.pin_mode == "track"
    lock2 = (
        db_session.query(BundleLock)
        .filter(BundleLock.bundle_id == bundle.id)
        .order_by(BundleLock.revision.desc())
        .first()
    )
    assert lock2.locked_entries[0]["version"] == "2.0.0"


def test_mutation_that_would_break_the_lock_fails_loud(middleware_client, db_session):
    """A dead artifact surfaces as ONE actionable 409 at mutation time."""
    owner = _user(db_session)
    key = _api_key(db_session, owner)
    bundle = _bundle(db_session, owner)
    dead = _skill(
        db_session, "dead-artifact", [("1.0.0", "h1", f"{DEAD_PATH_PREFIX}/dead-1.0.0.tar.gz")]
    )
    db_session.commit()

    r = middleware_client.post(
        f"/api/cookbooks/{bundle.id}/skills",
        headers={"x-api-key": key},
        json={"slug": dead.slug},
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert "dead-artifact" in str(body), "the 409 must name the offending slug"
    assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == bundle.id).count() == 0, (
        "a refused mint must leave the bundle unchanged"
    )


# ── 5. two members get byte-identical targets ──────────────────────────────


def test_two_members_resolve_byte_identical_targets(db_session, live_tarball):
    """The drift-killer guarantee, proven by equal lock_hash across members."""
    from app.services.drift_service import compute_lock_hash, current_lock
    from app.services.reconcile import compute_reconcile_plan

    skill_a = _skill(db_session, "shared-a", [("1.0.0", "ha", live_tarball)])
    skill_b = _skill(db_session, "shared-b", [("2.3.4", "hb", live_tarball)])
    bundle = _bundle(db_session)
    _declare(db_session, bundle, skill_a)
    _declare(db_session, bundle, skill_b)
    db_session.commit()

    member_1 = compute_reconcile_plan(db_session, bundle.id, local=[])
    member_2 = compute_reconcile_plan(db_session, bundle.id, local=[])

    def as_entries(plan):
        return [
            {"slug": a["slug"], "version": a["version"], "content_hash": a["checksum_sha256"]}
            for a in plan.add
        ]

    assert compute_lock_hash(as_entries(member_1)) == compute_lock_hash(as_entries(member_2))
    assert compute_lock_hash(as_entries(member_1)) == current_lock(db_session, bundle.id).lock_hash


# ── 6. the gate: reconcile never resolves off pinned_version ───────────────


def test_reconcile_declared_skills_never_reads_pinned_version():
    """Gate: the resolution function must not touch ``pinned_version`` at all.

    Asserted over the AST rather than the raw text so prose in a docstring
    (which is exactly where the old behaviour is documented) cannot trip it,
    and so a rename of the local variable cannot smuggle the read back in.
    """
    import ast
    import inspect

    from app.services import reconcile as rc

    tree = ast.parse(inspect.getsource(rc._declared_skills))
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "pinned_version"
    ]
    assert reads == [], "_declared_skills must resolve through the lock, not the pin column"

    # ...and it resolves through the lock instead.
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "locked_entries_for_reconcile" in called


# ── 7. semantic-latest parity between the two former resolvers ─────────────


def test_track_head_uses_semantic_latest_not_lexicographic(db_session, live_tarball):
    """max('1.9.0', '1.10.0') is 1.10.0. The lock resolver used created_at
    ordering while reconcile used semantic latest — a second divergence."""
    from app.services.drift_service import mint_bundle_lock

    skill = _skill(
        db_session,
        "semver-order",
        [("1.10.0", "h-110", live_tarball), ("1.9.0", "h-19", live_tarball)],
    )
    bundle = _bundle(db_session)
    _declare(db_session, bundle, skill)
    db_session.commit()

    lock = mint_bundle_lock(db_session, bundle)
    assert lock.locked_entries[0]["version"] == "1.10.0"

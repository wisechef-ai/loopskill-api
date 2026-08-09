"""Bundle rollback — the recovery path W5 left out.

mesh0408-W5 gave the bundle-apply path a real terminal state
(``applying -> converged | failed``), closing the "status that cannot go
red" gap. But it opened a new one: nothing ever moved a bundle OFF
``failed``. An operator had to notice and manually re-POST ``/apply``.

This pins the fix: ``POST /api/cookbook-deploy/{id}/rollback``.

  - rolling back a bundle whose latest job is ``failed`` opens a fresh job
    (idempotent terminal-state clear)
  - rollback re-resolves CURRENT targets, so a patch published between the
    failure and the rollback call is picked up, not frozen out
  - rollback on a bundle with no job yet, or whose latest job is
    ``applying``/``converged``, is a 409 — never silently no-ops, never
    stacks a spurious retry on top of a job that might still converge
  - a second rollback call after the first succeeds is also a 409 (the new
    job is ``applying``, not ``failed``) — this makes retries observably
    safe rather than silently duplicating jobs
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Bundle, BundleDeployment, BundleSkill, Skill, SkillVersion, User


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
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


def _mk_user(db: Session, tier: str = "pro") -> User:
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"rb-{uuid.uuid4().hex[:8]}@test.loopskill.io",
        display_name="rollback-tester",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_bundle(db: Session, owner: User, **kw) -> Bundle:
    cb = Bundle(
        id=uuid.uuid4(),
        name=kw.pop("name", "Rollback Bundle"),
        slug=kw.pop("slug", f"rb-{uuid.uuid4().hex[:8]}"),
        bundle_owner=owner.id,
        visibility=kw.pop("visibility", "private"),
        **kw,
    )
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db: Session, slug: str) -> Skill:
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        is_public=True,
        tier="free",
        install_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.commit()
    return s


def _mk_version(db: Session, skill: Skill, semver: str) -> SkillVersion:
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        semver=semver,
        checksum_sha256="c" * 64,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.commit()
    return v


def _deploy(db: Session, cb: Bundle, skill: Skill) -> BundleDeployment:
    row = BundleDeployment(
        id=uuid.uuid4(),
        bundle_id=cb.id,
        skill_id=skill.id,
        version_pin=None,
        install_order=100,
    )
    db.add(row)
    db.add(BundleSkill(id=uuid.uuid4(), bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db.commit()
    return row


def _portal_client(db: Session, user: User | None) -> TestClient:
    from app import auth_routes
    from app.bundle_deployment_routes import router as deploy_router

    app = FastAPI()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[auth_routes.get_current_user_optional] = lambda: user
    app.include_router(deploy_router)
    return TestClient(app)


def _fail_latest_job(db: Session, portal: TestClient, cb: Bundle, skill_slug: str) -> str:
    """Apply, then directly force the resulting job to FAILED (no agent in this test)."""
    from app.services.bundle_apply import STATUS_FAILED

    resp = portal.post(f"/api/cookbook-deploy/{cb.id}/apply")
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    from app.models import BundleApplyJob, BundleApplyJobItem

    job = db.query(BundleApplyJob).filter(BundleApplyJob.id == uuid.UUID(job_id)).first()
    items = db.query(BundleApplyJobItem).filter(BundleApplyJobItem.job_id == job.id).all()
    for item in items:
        item.outcome = "failed"
        item.failure_reason = "simulated failure for rollback test"
    job.status = STATUS_FAILED
    job.terminal_at = datetime.now(timezone.utc)
    db.commit()
    return job_id


# ═══════════════════════════════════════════════════════════════════════
# rollback clears a genuinely FAILED bundle
# ═══════════════════════════════════════════════════════════════════════


def test_rollback_opens_a_fresh_job_when_latest_is_failed(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "rb-basic")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    portal = _portal_client(db, owner)
    failed_job_id = _fail_latest_job(db, portal, cb, "rb-basic")

    resp = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applying"
    assert body["job_id"] != failed_job_id, "rollback must open a NEW job, not touch the failed one"
    assert body["targets"] == [{"slug": "rb-basic", "semver": "1.0.0"}]

    # the failed job stays failed — rollback never mutates history
    status = portal.get(f"/api/cookbook-deploy/{cb.id}/jobs/{failed_job_id}")
    assert status.json()["status"] == "failed"


def test_rollback_after_a_patch_targets_the_patched_version(db):
    """The whole point: a rollback issued after a fix ships should retry the
    FIX, not blindly replay the exact broken versions that just failed."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "rb-patched")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    portal = _portal_client(db, owner)
    _fail_latest_job(db, portal, cb, "rb-patched")

    # the fix ships
    _mk_version(db, skill, "1.0.1")

    resp = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert resp.status_code == 200, resp.text
    assert resp.json()["targets"] == [{"slug": "rb-patched", "semver": "1.0.1"}], (
        "rollback must re-resolve to the CURRENT (patched) version, not replay the broken one"
    )


# ═══════════════════════════════════════════════════════════════════════
# rollback refuses to touch anything that isn't genuinely FAILED
# ═══════════════════════════════════════════════════════════════════════


def test_rollback_409s_when_no_job_has_ever_run(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "rb-none")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    portal = _portal_client(db, owner)
    resp = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["latest_status"] is None


def test_rollback_409s_when_latest_job_is_still_applying(db):
    """An applying job might still converge on its own — rollback must not
    race it or stack a duplicate retry underneath it."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "rb-applying")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    portal = _portal_client(db, owner)
    resp = portal.post(f"/api/cookbook-deploy/{cb.id}/apply")
    assert resp.status_code == 200
    assert resp.json()["status"] == "applying"

    rb = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert rb.status_code == 409, rb.text
    assert rb.json()["detail"]["latest_status"] == "applying"


def test_rollback_409s_when_latest_job_already_converged(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "rb-converged")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    from app.services.bundle_apply import STATUS_CONVERGED

    portal = _portal_client(db, owner)
    resp = portal.post(f"/api/cookbook-deploy/{cb.id}/apply")
    job_id = resp.json()["job_id"]

    from app.models import BundleApplyJob, BundleApplyJobItem

    job = db.query(BundleApplyJob).filter(BundleApplyJob.id == uuid.UUID(job_id)).first()
    items = db.query(BundleApplyJobItem).filter(BundleApplyJobItem.job_id == job.id).all()
    for item in items:
        item.outcome = "success"
        item.reported_semver = item.expected_semver
    job.status = STATUS_CONVERGED
    job.terminal_at = datetime.now(timezone.utc)
    db.commit()

    rb = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert rb.status_code == 409, rb.text
    assert rb.json()["detail"]["latest_status"] == "converged"


def test_second_rollback_call_is_safely_rejected_not_a_silent_duplicate(db):
    """After rollback opens a new (applying) job, calling rollback again must
    409 — not stack a second retry underneath the first."""
    from datetime import timedelta

    from app.models import BundleApplyJob

    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "rb-double")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    portal = _portal_client(db, owner)
    failed_job_id = _fail_latest_job(db, portal, cb, "rb-double")
    # SQLite's CURRENT_TIMESTAMP (server_default=func.now()) has 1s
    # resolution, so two jobs created inside the same test can tie on
    # created_at — backdate the failed job the same way the W5 suite
    # backdates SkillVersion rows (age_seconds) to make "latest" deterministic
    # without touching the real Postgres-backed ordering logic.
    failed_row = db.query(BundleApplyJob).filter(BundleApplyJob.id == uuid.UUID(failed_job_id)).first()
    failed_row.created_at = failed_row.created_at - timedelta(seconds=5)
    db.commit()

    first = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert first.status_code == 200, first.text

    second = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["latest_status"] == "applying"


def test_rollback_requires_pro_tier(db):
    owner = _mk_user(db, tier="free")
    cb = _mk_bundle(db, owner)
    portal = _portal_client(db, owner)
    resp = portal.post(f"/api/cookbook-deploy/{cb.id}/rollback")
    assert resp.status_code == 402

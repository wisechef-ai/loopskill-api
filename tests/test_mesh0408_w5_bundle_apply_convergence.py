"""mesh_0408 W5 — the bundle deploy path reaches a TERMINAL state.

The gap this closes (recorded honestly on 2026-08-07):

    bundle_deployments = 0 and app/bundle_deployment_routes.py:326,348 returned
    a permanent {"status": "applying"}. No terminal state existed and no code
    path reached one. A composite LOOP genuinely deploys onto a member; the
    BUNDLE path did not. A status that cannot go red is decoration (trap V1).

Worse, the old ``GET /{cookbook_id}/jobs/{job_id}`` fabricated
``{"status": "applying"}`` for **any** job_id — including ids that were never
issued — because ``apply`` synthesized ``uuid.uuid4()`` and threw it away.

What is pinned here:

  - apply PERSISTS a job with per-skill EXPECTED semvers resolved from the
    bundle at apply time
  - an unknown job_id is 404, never a fabricated "applying"
  - a member report drives the status: all-success-at-expected -> ``converged``
  - ANY failure report -> ``failed``  (the status CAN go red)
  - THE REDEPLOY INTEGRITY TEST: a member reporting success at a STALE semver
    does NOT converge the job. Convergence means "the member is running the
    version the bundle currently resolves to", not "the member said ok".
  - after a patch publishes a new version, a freshly started job resolves to
    the NEW semver — this is step 4 of the moat loop
  - tenant boundary: a member whose fleet is not subscribed to the bundle
    cannot start or report against it (404, no existence leak)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import (
    APIKey,
    Base,
    Bundle,
    BundleDeployment,
    BundleSkill,
    Fleet,
    FleetMember,
    FleetSubscription,
    Skill,
    SkillVersion,
    User,
)
from app.services.bundle_apply import (
    STATUS_APPLYING,
    STATUS_CONVERGED,
    STATUS_FAILED,
)


# ── DB fixtures ──────────────────────────────────────────────────────────


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


# ── builders ─────────────────────────────────────────────────────────────


def _mk_user(db: Session, tier: str = "pro") -> User:
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"w5-{uuid.uuid4().hex[:8]}@test.loopskill.io",
        display_name="w5",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_bundle(db: Session, owner: User, **kw) -> Bundle:
    cb = Bundle(
        id=uuid.uuid4(),
        name=kw.pop("name", "W5 Bundle"),
        slug=kw.pop("slug", f"w5-{uuid.uuid4().hex[:8]}"),
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


def _mk_version(db: Session, skill: Skill, semver: str, *, age_seconds: int = 0) -> SkillVersion:
    """Publish a version. ``age_seconds`` back-dates it so ``created_at desc``
    ordering (Skill.versions) is deterministic instead of clock-resolution
    dependent — two rows inserted in the same millisecond would otherwise tie."""
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        semver=semver,
        checksum_sha256="b" * 64,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )
    db.add(v)
    db.commit()
    return v


def _deploy(db: Session, cb: Bundle, skill: Skill, *, version_pin: str | None = None) -> BundleDeployment:
    row = BundleDeployment(
        id=uuid.uuid4(),
        bundle_id=cb.id,
        skill_id=skill.id,
        version_pin=version_pin,
        install_order=100,
    )
    db.add(row)
    # The membership layer is a separate table; the converge surface reads the
    # DEPLOYMENT layer, but keep both consistent so the fixture mirrors prod.
    db.add(BundleSkill(id=uuid.uuid4(), bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db.commit()
    return row


def _mk_member(
    db: Session, owner: User, cb: Bundle | None, *, host: str = "client-box"
) -> tuple[FleetMember, APIKey]:
    """An enrolled agent identified by its own api-key (lock #13).

    ``cb`` None → the fleet is subscribed to nothing (the negative-authz case).
    """
    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name=f"fleet-{uuid.uuid4().hex[:6]}",
        fleet_api_key_hash=uuid.uuid4().hex + uuid.uuid4().hex[:0],
    )
    db.add(fleet)
    key = APIKey(
        id=uuid.uuid4(),
        user_id=owner.id,
        key_prefix="lsk_w5",
        key_hash=uuid.uuid4().hex,
        name="member key",
        is_test=True,
        is_active=True,
    )
    db.add(key)
    db.commit()
    member = FleetMember(
        id=uuid.uuid4(),
        fleet_id=fleet.id,
        host=host,
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key.id,
        is_active=True,
    )
    db.add(member)
    if cb is not None:
        db.add(FleetSubscription(fleet_id=fleet.id, bundle_id=cb.id, channel="stable"))
    db.commit()
    return member, key


# ── app builders ─────────────────────────────────────────────────────────


def _agent_client(
    db: Session, key: APIKey | None, user: User | None = None, *, bundle_scope=None
) -> TestClient:
    """The AGENT surface (/api/bundle-apply) — authenticated by api-key, i.e.
    ``request.state.{auth_ctx,api_key_id,api_key_user_id}`` as the api-key
    middleware would stamp them."""
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.auth_ctx import AuthContext
    from app.bundle_converge_routes import router as converge_router

    app = FastAPI()

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_db
    app.include_router(converge_router)

    uid = user.id if user is not None else (key.user_id if key is not None else None)

    class InjectKeyState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if key is None:
                request.state.auth_ctx = AuthContext.anonymous()
                request.state.api_key_id = None
                request.state.api_key_user_id = None
            else:
                request.state.auth_ctx = AuthContext(
                    scope="user", user_id=uid, api_key_id=key.id, bundle_scope=bundle_scope
                )
                request.state.api_key_id = key.id
                request.state.api_key_user_id = uid
            return await call_next(request)

    app.add_middleware(InjectKeyState)
    return TestClient(app)


def _portal_client(db: Session, user: User | None) -> TestClient:
    """The CONTROL-PLANE surface (/api/bundle-deploy) — JWT/portal auth."""
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


def _start_job(agent: TestClient, slug: str) -> dict:
    resp = agent.post(f"/api/bundle-apply/{slug}/start")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════
# 1. apply persists a REAL job (was: synthesized uuid, thrown away)
# ═══════════════════════════════════════════════════════════════════════


def test_apply_persists_a_real_job_readable_by_the_control_plane(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-apply-persist")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    portal = _portal_client(db, owner)
    resp = portal.post(f"/api/bundle-deploy/{cb.id}/apply")
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == STATUS_APPLYING

    # The job must actually EXIST in the DB — not be a fabricated uuid.
    from app.models import BundleApplyJob, BundleApplyJobItem

    row = db.query(BundleApplyJob).filter(BundleApplyJob.id == uuid.UUID(job_id)).first()
    assert row is not None, "apply must PERSIST the job, not synthesize a throwaway uuid"
    assert row.bundle_id == cb.id
    items = db.query(BundleApplyJobItem).filter(BundleApplyJobItem.job_id == row.id).all()
    assert [i.expected_semver for i in items] == ["1.0.0"]

    status = portal.get(f"/api/bundle-deploy/{cb.id}/jobs/{job_id}")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == STATUS_APPLYING


def test_unknown_job_id_is_404_not_a_fabricated_applying(db):
    """The old handler returned {"status": "applying"} for ANY job_id — a
    status that is invented rather than observed."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    portal = _portal_client(db, owner)

    resp = portal.get(f"/api/bundle-deploy/{cb.id}/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404, f"unknown job must 404, got {resp.status_code}: {resp.text}"


# ═══════════════════════════════════════════════════════════════════════
# 2. the member drives the terminal state
# ═══════════════════════════════════════════════════════════════════════


def test_member_report_success_at_expected_semver_converges(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-converge")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)
    _member, key = _mk_member(db, owner, cb)

    agent = _agent_client(db, key)
    job = _start_job(agent, cb.slug)
    assert job["status"] == STATUS_APPLYING
    assert job["targets"] == [{"slug": "w5-converge", "semver": "1.0.0"}]

    resp = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-converge", "semver": "1.0.0", "outcome": "success"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == STATUS_CONVERGED
    assert resp.json()["terminal"] is True

    # And it is observable from the CONTROL PLANE, not just the agent's echo.
    portal = _portal_client(db, owner)
    seen = portal.get(f"/api/bundle-deploy/{cb.id}/jobs/{job['job_id']}")
    assert seen.status_code == 200, seen.text
    assert seen.json()["status"] == STATUS_CONVERGED


def test_member_report_failure_makes_the_status_go_RED(db):
    """A status that cannot go red is decoration (trap V1)."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-red")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)
    _member, key = _mk_member(db, owner, cb)

    agent = _agent_client(db, key)
    job = _start_job(agent, cb.slug)

    resp = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={
            "slug": "w5-red",
            "semver": "1.0.0",
            "outcome": "failed",
            "failure_reason": "pkg-config missing on a minimal host",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == STATUS_FAILED
    assert resp.json()["terminal"] is True

    portal = _portal_client(db, owner)
    seen = portal.get(f"/api/bundle-deploy/{cb.id}/jobs/{job['job_id']}").json()
    assert seen["status"] == STATUS_FAILED
    assert seen["items"][0]["failure_reason"] == "pkg-config missing on a minimal host"


def test_partial_report_stays_applying_until_every_item_reports(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    a = _mk_skill(db, "w5-partial-a")
    b = _mk_skill(db, "w5-partial-b")
    _mk_version(db, a, "1.0.0")
    _mk_version(db, b, "2.0.0")
    _deploy(db, cb, a)
    _deploy(db, cb, b)
    _member, key = _mk_member(db, owner, cb)

    agent = _agent_client(db, key)
    job = _start_job(agent, cb.slug)

    r = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-partial-a", "semver": "1.0.0", "outcome": "success"},
    )
    assert r.json()["status"] == STATUS_APPLYING
    assert r.json()["terminal"] is False

    r = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-partial-b", "semver": "2.0.0", "outcome": "success"},
    )
    assert r.json()["status"] == STATUS_CONVERGED


# ═══════════════════════════════════════════════════════════════════════
# 3. THE REDEPLOY INTEGRITY TEST — the heart of the moat loop
# ═══════════════════════════════════════════════════════════════════════


def test_success_reported_at_a_STALE_semver_does_not_converge(db):
    """Convergence means the member runs the version the bundle CURRENTLY
    resolves to — not merely that the member said "ok".

    Without this, the redeploy half of the loop is unfalsifiable: an agent
    still sitting on the defective 1.0.0 could report success and the control
    plane would show green while the patch was never applied.
    """
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-stale")
    _mk_version(db, skill, "1.0.0", age_seconds=60)
    _mk_version(db, skill, "1.0.1")  # the PATCH
    _deploy(db, cb, skill)
    _member, key = _mk_member(db, owner, cb)

    agent = _agent_client(db, key)
    job = _start_job(agent, cb.slug)
    assert job["targets"] == [{"slug": "w5-stale", "semver": "1.0.1"}], (
        "a freshly started job must resolve to the PATCHED version"
    )

    resp = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-stale", "semver": "1.0.0", "outcome": "success"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] != STATUS_CONVERGED, (
        "reporting success at a stale semver must NOT converge the job"
    )
    assert resp.json()["status"] == STATUS_APPLYING
    assert resp.json()["items"][0]["reported_semver"] == "1.0.0"
    assert resp.json()["items"][0]["expected_semver"] == "1.0.1"

    # ...and reporting the CORRECT version afterwards does converge it.
    resp = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-stale", "semver": "1.0.1", "outcome": "success"},
    )
    assert resp.json()["status"] == STATUS_CONVERGED


def test_patch_publish_makes_a_new_job_resolve_to_the_new_version(db):
    """Steps 3+4 of the moat loop: patch published as a new version, and the
    member's bundle now resolves to it."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-redeploy")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)
    _member, key = _mk_member(db, owner, cb)
    agent = _agent_client(db, key)

    first = _start_job(agent, cb.slug)
    assert first["targets"] == [{"slug": "w5-redeploy", "semver": "1.0.0"}]

    # --- the defect is patched and republished ---
    _mk_version(db, skill, "1.0.1")

    second = _start_job(agent, cb.slug)
    assert second["job_id"] != first["job_id"]
    assert second["targets"] == [{"slug": "w5-redeploy", "semver": "1.0.1"}]


def test_version_pin_wins_over_latest(db):
    """A pinned deployment must resolve to its pin, not to whatever is newest —
    otherwise 'frozen' bundles would silently drift."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-pinned")
    _mk_version(db, skill, "1.0.0", age_seconds=60)
    _mk_version(db, skill, "2.0.0")
    _deploy(db, cb, skill, version_pin="1.0.0")
    _member, key = _mk_member(db, owner, cb)

    job = _start_job(_agent_client(db, key), cb.slug)
    assert job["targets"] == [{"slug": "w5-pinned", "semver": "1.0.0"}]


# ═══════════════════════════════════════════════════════════════════════
# 4. terminal really is terminal
# ═══════════════════════════════════════════════════════════════════════


def test_reporting_into_a_terminal_job_is_rejected(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-terminal")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)
    _member, key = _mk_member(db, owner, cb)
    agent = _agent_client(db, key)

    job = _start_job(agent, cb.slug)
    ok = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-terminal", "semver": "1.0.0", "outcome": "success"},
    )
    assert ok.json()["status"] == STATUS_CONVERGED

    again = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-terminal", "semver": "1.0.0", "outcome": "failed"},
    )
    assert again.status_code == 409, again.text

    # the terminal state is NOT rewritten by the rejected report
    portal = _portal_client(db, owner)
    assert portal.get(f"/api/bundle-deploy/{cb.id}/jobs/{job['job_id']}").json()["status"] == (
        STATUS_CONVERGED
    )


def test_bundle_with_no_resolvable_version_cannot_open_a_vacuous_job(db):
    """A zero-item job would converge vacuously (``all([]) is True``) — a green
    that proves nothing. Refuse to open it instead."""
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-noversions")  # published nothing
    _deploy(db, cb, skill)
    _member, key = _mk_member(db, owner, cb)

    resp = _agent_client(db, key).post(f"/api/bundle-apply/{cb.slug}/start")
    assert resp.status_code == 409, resp.text
    assert resp.json()["unresolvable"] == ["w5-noversions"]


# ═══════════════════════════════════════════════════════════════════════
# 5. tenant boundary
# ═══════════════════════════════════════════════════════════════════════


def test_member_of_an_unsubscribed_fleet_cannot_start_a_job(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-authz-start")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    stranger = _mk_user(db)
    _member, key = _mk_member(db, stranger, None)  # subscribed to nothing

    resp = _agent_client(db, key).post(f"/api/bundle-apply/{cb.slug}/start")
    assert resp.status_code == 404, f"expected no-existence-leak 404, got {resp.status_code}"


def test_member_cannot_report_into_another_fleets_job(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-authz-report")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)
    _mine, my_key = _mk_member(db, owner, cb)

    job = _start_job(_agent_client(db, my_key), cb.slug)

    stranger = _mk_user(db)
    _theirs, their_key = _mk_member(db, stranger, None, host="stranger-box")

    resp = _agent_client(db, their_key).post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-authz-report", "semver": "1.0.0", "outcome": "success"},
    )
    assert resp.status_code == 404, resp.text

    # V2: the two principals must be genuinely distinct, or this proves nothing.
    assert owner.id != stranger.id
    assert my_key.user_id != their_key.user_id


def test_bundle_scoped_key_cannot_converge_a_DIFFERENT_bundle(db):
    """A key restricted to one bundle must stay there — even when its fleet is
    subscribed to the other bundle and its user OWNS the other bundle. Both of
    the entitlement arms would otherwise say yes."""
    owner = _mk_user(db)
    mine = _mk_bundle(db, owner, name="Scoped To This")
    other = _mk_bundle(db, owner, name="Not This One")
    skill = _mk_skill(db, "w5-scoped-key")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, other, skill)
    _member, key = _mk_member(db, owner, other)  # fleet IS subscribed to `other`

    scoped = _agent_client(db, key, bundle_scope=mine.id)
    resp = scoped.post(f"/api/bundle-apply/{other.slug}/start")
    assert resp.status_code == 404, resp.text

    # Control: the SAME key, scoped to `other`, is allowed — so the 404 above is
    # the scope restriction and not some unrelated setup failure.
    allowed = _agent_client(db, key, bundle_scope=other.id)
    assert allowed.post(f"/api/bundle-apply/{other.slug}/start").status_code == 200


def test_anonymous_caller_cannot_start_a_job(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-anon")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)

    resp = _agent_client(db, None).post(f"/api/bundle-apply/{cb.slug}/start")
    assert resp.status_code == 401, resp.text


def test_report_for_a_skill_not_in_the_job_is_rejected(db):
    owner = _mk_user(db)
    cb = _mk_bundle(db, owner)
    skill = _mk_skill(db, "w5-scope-in")
    _mk_version(db, skill, "1.0.0")
    _deploy(db, cb, skill)
    _mk_skill(db, "w5-scope-out")
    _member, key = _mk_member(db, owner, cb)
    agent = _agent_client(db, key)

    job = _start_job(agent, cb.slug)
    resp = agent.post(
        f"/api/bundle-apply/jobs/{job['job_id']}/report",
        json={"slug": "w5-scope-out", "semver": "1.0.0", "outcome": "success"},
    )
    assert resp.status_code == 404, resp.text

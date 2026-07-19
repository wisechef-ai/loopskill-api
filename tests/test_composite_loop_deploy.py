"""feat/composite-loop-deploy — tests for POST /api/composite-loops/{slug}/deploy.

Full-chain kill-test pattern mirrors tests/test_member_loop_apply.py: real
User/APIKey/Fleet/FleetMember rows through the middleware app factory, then
GET /api/my/loop-assignments verifies the deployed manifest+placement are
actually visible to the member (the sync-tick materialization contract).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="deploy-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, label="owner-key"):
    from app.models import APIKey

    raw = f"rec_live_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name=label,
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_fleet(db, owner, *, org_id=None):
    from app.models import Fleet

    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name="deploy-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        org_id=org_id,
    )
    db.add(fleet)
    db.flush()
    return fleet


def _enroll_member(client, db, fleet, owner_key, *, host="agent-host", profile="default"):
    r = client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": owner_key},
        json={"host": host, "profile": profile, "skills_dir": "~/.hermes/loopskill"},
    )
    assert r.status_code == 201, r.text
    return r.json()["member_id"], r.json()["api_key"]


def _ping_member(db, member_id, *, provides=None):
    from app.models import FleetMemberLiveness

    lv = FleetMemberLiveness(
        member_id=uuid.UUID(str(member_id)),
        provides=provides or {"os": "linux", "arch": "x86_64"},
    )
    db.add(lv)
    db.flush()
    return lv


def _mk_verifier(db, *, slug="deploy-test-verifier"):
    from app.models import Verifier

    v = db.query(Verifier).filter(Verifier.slug == slug).first()
    if v is not None:
        return v
    v = Verifier(
        id=uuid.uuid4(),
        slug=slug,
        title="Deploy Test Verifier",
        description="verifier for composite-loop-deploy tests",
        is_public=True,
        success_condition="the daily brief was written",
        verification_script="true",
        max_turns=25,
        stopping_criteria={"success": "done", "failure": "error", "budget": None},
        tool_allowlist=[],
        system_prompt="You are a verifier.",
    )
    db.add(v)
    db.flush()
    return v


def _mk_composite_loop(
    db,
    *,
    slug="brief-loop",
    schedule="0 7 * * *",
    prompt="Write the daily brief.",
    verifier_slug=None,
    with_version=True,
):
    from app.models import CompositeLoop, CompositeLoopVersion

    verifier = _mk_verifier(db, slug=verifier_slug or f"{slug}-verifier")
    cl = CompositeLoop(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="test composite loop",
        tier="free",
        is_public=True,
        schedule=schedule,
        skills=[{"slug": "discord-post-from-cron"}],
        connectors=[],
        subagents_config={"maker": {"model_tier": "sonnet", "toolsets": []}},
        verifier_slug=verifier.slug,
        state_seed={},
        budget_usd=None,
        prompt=prompt,
    )
    db.add(cl)
    db.flush()
    if with_version:
        manifest = {
            "slug": cl.slug,
            "title": cl.title,
            "schedule": schedule,
            "skills": cl.skills,
            "connectors": [],
            "subagents_config": cl.subagents_config,
            "verifier_slug": verifier.slug,
            "state_seed": {},
            "budget_usd": None,
            "prompt": prompt,
            "residency": None,
        }
        version = CompositeLoopVersion(
            id=uuid.uuid4(),
            composite_loop_id=cl.id,
            semver="1.0.0",
            manifest=manifest,
            changelog="initial",
        )
        db.add(version)
        db.flush()
    return cl


def _deploy_body(fleet, member_id):
    return {"fleet_id": str(fleet.id), "member_id": str(member_id)}


class TestDeployHappyPath:
    def test_deploy_creates_manifest_and_placement_then_member_sees_it(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _ping_member(db_session, member_id)
        cl = _mk_composite_loop(db_session, slug="brief-loop")
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deployed"] is True
        assert body["loop_id"] == "brief-loop"
        assert body["fleet_id"] == str(fleet.id)
        assert body["member_id"] == str(member_id)
        assert body["epoch"] == 1
        assert body["status"] == "active"
        assert "placement_id" in body
        assert body["note"] == "member applies on its next sync tick"

        from app.models import LoopManifest, LoopPlacement

        manifest = db_session.query(LoopManifest).filter(LoopManifest.loop_id == "brief-loop").one()
        assert manifest.owner_user_id == owner.id
        assert manifest.org_id == fleet.org_id  # both stamped from the fleet
        assert manifest.schedule == "0 7 * * *"
        assert manifest.prompt == "Write the daily brief."

        placement = (
            db_session.query(LoopPlacement)
            .filter(LoopPlacement.fleet_id == fleet.id, LoopPlacement.loop_key == "brief-loop")
            .one()
        )
        assert placement.member_id == uuid.UUID(str(member_id))
        assert placement.status == "active"

        # Full-chain kill-test: the member's own sync-tick surface sees it.
        r2 = middleware_client.get("/api/my/loop-assignments", headers={"x-api-key": member_key})
        assert r2.status_code == 200, r2.text
        assignments = r2.json()["assignments"]
        assert len(assignments) == 1
        a = assignments[0]
        assert a["loop_key"] == "brief-loop"
        assert a["manifest"] is not None
        assert a["manifest"]["schedule"] == "0 7 * * *"
        assert a["manifest"]["prompt"] == "Write the daily brief."


class TestDeployAuth:
    def test_anonymous_401(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        fleet = _mk_fleet(db_session, owner)
        cl = _mk_composite_loop(db_session, slug="anon-loop")
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            json=_deploy_body(fleet, uuid.uuid4()),
        )
        assert r.status_code == 401

    def test_non_manager_403(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, _member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _ping_member(db_session, member_id)
        cl = _mk_composite_loop(db_session, slug="notmine-loop")

        stranger = _mk_user(db_session, tier="free")
        stranger_key = _mk_key(db_session, stranger, label="stranger-key")
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": stranger_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r.status_code == 403


class TestDeployNotFound:
    def test_unknown_slug_404(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, _member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        db_session.commit()

        r = middleware_client.post(
            "/api/composite-loops/does-not-exist/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r.status_code == 404

    def test_unknown_fleet_404(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        cl = _mk_composite_loop(db_session, slug="fleet-404-loop")
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json={"fleet_id": str(uuid.uuid4()), "member_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404

    def test_unknown_member_404(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        cl = _mk_composite_loop(db_session, slug="member-404-loop")
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, uuid.uuid4()),
        )
        assert r.status_code == 404


class TestDeployPreflight:
    def test_never_pinged_member_400_with_reason(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, _member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        # deliberately NOT pinging the member
        cl = _mk_composite_loop(db_session, slug="never-pinged-loop")
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "member-never-pinged" in detail["reason"]


class TestDeployIdempotent:
    def test_redeploy_same_loop_same_member_is_idempotent(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, _member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _ping_member(db_session, member_id)
        cl = _mk_composite_loop(db_session, slug="idempotent-loop")
        db_session.commit()

        r1 = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r1.status_code == 200, r1.text
        r2 = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["placement_id"] == r1.json()["placement_id"]
        assert r2.json()["epoch"] == r1.json()["epoch"]

        from app.models import LoopPlacement

        count = (
            db_session.query(LoopPlacement)
            .filter(LoopPlacement.fleet_id == fleet.id, LoopPlacement.loop_key == "idempotent-loop")
            .count()
        )
        assert count == 1


class TestDeployNotDeployable:
    def test_versionless_loop_deploys_from_row(self, middleware_client, db_session):
        """Seeded/v0 loops (versions=[]) are deployable from the ROW's own
        schedule/prompt columns — the live 'atomic-habits' shape. Requiring a
        version row would have 409'd the only live composite loop
        (live-found pre-merge, supervisor review)."""
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, _member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _ping_member(db_session, member_id)
        cl = _mk_composite_loop(db_session, slug="no-version-loop", with_version=False)
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r.status_code == 200, r.text
        assert r.json()["deployed"] is True

        from app.models import LoopManifest

        m = db_session.query(LoopManifest).filter(LoopManifest.loop_id == "no-version-loop").first()
        assert m is not None
        assert m.schedule == "0 7 * * *"
        assert m.prompt == "Write the daily brief."

    def test_version_with_empty_schedule_409(self, middleware_client, db_session):
        from app.models import CompositeLoopVersion

        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, _member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _ping_member(db_session, member_id)
        cl = _mk_composite_loop(db_session, slug="bad-manifest-loop", with_version=False)
        version = CompositeLoopVersion(
            id=uuid.uuid4(),
            composite_loop_id=cl.id,
            semver="1.0.0",
            manifest={
                "slug": cl.slug,
                "title": cl.title,
                "schedule": "",
                "skills": [],
                "connectors": [],
                "subagents_config": {},
                "verifier_slug": "x",
                "state_seed": {},
                "budget_usd": None,
                "prompt": "has a prompt but no schedule",
                "residency": None,
            },
            changelog="bad",
        )
        db_session.add(version)
        db_session.commit()

        r = middleware_client.post(
            f"/api/composite-loops/{cl.slug}/deploy",
            headers={"x-api-key": owner_key},
            json=_deploy_body(fleet, member_id),
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["reason"] == "not_deployable"

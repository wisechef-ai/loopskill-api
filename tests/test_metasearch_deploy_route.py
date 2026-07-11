"""Integration tests for the fleet-deploy route (metasearch_0710 P3).

The north-star acceptance: an external metasearch result deploys to a fleet
(bundle) with a deploy-time content pin, the BundleSkill desired-state row is
written, the fleet_deploy funnel event fires. ClawHub/unresolvable → fail closed.
Cap enforced. Curated → use the normal bundle-add route."""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

from app.database import get_db
from app.models import Bundle, BundleSkill, Skill, TelemetryEvent, User


def _make_user(db, *, tier="pro_plus"):
    uid = uuid4()
    u = User(
        id=uid,
        display_name="T",
        email=f"{uid}@t.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _make_app(db, *, user_id):
    app = FastAPI()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    class InjectAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = user_id
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuth)
    from app.bundle_routes import router as cookbook_router
    from app.metasearch_deploy_routes import router as deploy_router

    app.include_router(deploy_router)
    app.include_router(cookbook_router)
    return app


def _mock_resolvable(monkeypatch, body="---\nname: x\n---\n# body"):
    import app.services.bundle_external as be

    monkeypatch.setattr(
        be,
        "resolve_external_install",
        lambda s, sl: {
            "content": body,
            "raw_url": "https://raw.githubusercontent.com/o/r/main/s/SKILL.md",
            "scan_status": "clean",
            "origin_url": "https://github.com/o/r",
        },
    )

    class _Ext:
        title, description, license = "X", "d", "MIT"
        origin_url = "https://github.com/o/r"
        install_path = type("IP", (), {"value": "fetch_origin"})()
        redistributable = True

    monkeypatch.setattr(be, "_resolve_external", lambda s, sl: _Ext())
    monkeypatch.setattr(
        be,
        "scan_on_add",
        lambda ext, f, slug: type(
            "V", (), {"badge": "clean", "scannable": True, "findings": [], "warnings": []}
        )(),
    )


# ── the north-star: external result deploys to a fleet ───────────────────────


def test_external_skill_deploys_to_fleet_with_pin(db_session, monkeypatch):
    _mock_resolvable(monkeypatch)
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="My Fleet", bundle_owner=user.id)
    db_session.add(cb)
    db_session.commit()

    app = _make_app(db_session, user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/skills/metasearch/deploy", json={"install_ref": "skills-sh:o--r--s", "fleet_id": str(cb.id)}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deployed"] is True
    assert body["pinned_sha"].startswith("sha256:")
    assert body["redeployed"] is False

    # desired-state row exists, pinned to the content SHA (agents reconcile against THIS)
    cs = (
        db_session.query(BundleSkill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cb.id, Skill.slug == "ext:skills-sh:o--r--s")
        .first()
    )
    assert cs is not None, "external skill entered desired-state (FK satisfied via materialized row)"
    assert cs.pinned_version == body["pinned_sha"], "pinned to the deploy-time content SHA"

    # north-star funnel event fired
    ev = (
        db_session.query(TelemetryEvent)
        .filter(TelemetryEvent.event_type == "metasearch.fleet_deploy")
        .first()
    )
    assert ev is not None
    assert json.loads(ev.payload)["fleet_id"] == str(cb.id)


def test_redeploy_advances_pin(db_session, monkeypatch):
    _mock_resolvable(monkeypatch, body="---\nname: x\n---\n# v1")
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="F", bundle_owner=user.id)
    db_session.add(cb)
    db_session.commit()
    app = _make_app(db_session, user_id=user.id)
    with TestClient(app) as client:
        r1 = client.post(
            "/api/skills/metasearch/deploy", json={"install_ref": "skills-sh:o--r--s", "fleet_id": str(cb.id)}
        )
        _mock_resolvable(monkeypatch, body="---\nname: x\n---\n# v2")
        r2 = client.post(
            "/api/skills/metasearch/deploy", json={"install_ref": "skills-sh:o--r--s", "fleet_id": str(cb.id)}
        )
    assert r1.json()["pinned_sha"] != r2.json()["pinned_sha"]
    assert r2.json()["redeployed"] is True


def test_clawhub_not_fleet_deployable_fails_closed(db_session, monkeypatch):
    """decision #6 / condition 2b: ClawHub resolves to no content → not deployable."""
    import app.services.bundle_external as be

    monkeypatch.setattr(be, "resolve_external_install", lambda s, sl: None)
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="F", bundle_owner=user.id)
    db_session.add(cb)
    db_session.commit()
    app = _make_app(db_session, user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/skills/metasearch/deploy",
            json={"install_ref": "clawhub:some-skill", "fleet_id": str(cb.id)},
        )
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "not_pinnable_no_content"


def test_curated_ref_rejected_use_bundle_add(db_session, monkeypatch):
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="F", bundle_owner=user.id)
    db_session.add(cb)
    db_session.commit()
    app = _make_app(db_session, user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/skills/metasearch/deploy", json={"install_ref": "recipes:mine", "fleet_id": str(cb.id)}
        )
    assert r.status_code == 422
    assert r.json()["detail"] == "use_bundle_add_for_curated"


def test_malformed_ref_422(db_session):
    user = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="F", bundle_owner=user.id)
    db_session.add(cb)
    db_session.commit()
    app = _make_app(db_session, user_id=user.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/skills/metasearch/deploy", json={"install_ref": "garbage", "fleet_id": str(cb.id)}
        )
    assert r.status_code == 422


def test_deploy_to_unowned_fleet_rejected(db_session, monkeypatch):
    _mock_resolvable(monkeypatch)
    owner = _make_user(db_session)
    other = _make_user(db_session)
    cb = Bundle(id=uuid4(), name="Owner's", bundle_owner=owner.id)
    db_session.add(cb)
    db_session.commit()
    # a DIFFERENT user tries to deploy to owner's fleet
    app = _make_app(db_session, user_id=other.id)
    with TestClient(app) as client:
        r = client.post(
            "/api/skills/metasearch/deploy", json={"install_ref": "skills-sh:o--r--s", "fleet_id": str(cb.id)}
        )
    assert r.status_code in (403, 404), "must not deploy to a fleet you don't own"

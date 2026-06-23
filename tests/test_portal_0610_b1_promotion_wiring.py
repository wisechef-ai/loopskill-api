"""portal_0610 B1 — wire the dead `stable` channel (§6.6).

Before this fix the promotion engine had ZERO callers: nothing wrote
`promoted_to_stable_at`, so `channel_select(stable)` always returned None and a
`stable` fleet/cookbook silently skipped every skill forever (stable == frozen).

These tests prove the wiring is live END TO END:
  1. A canary agent reports a clean apply via POST /reconcile-report.
  2. The report opportunistically promotes the version (gate met).
  3. channel_select(stable) now ADVANCES to that version (was None before).
  4. A canary FAILURE blocks promotion (the bad version never reaches stable).
  5. The admin sweep batch-promotes; non-master is 403'd.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.services.channel_select import latest_version_for_channel


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, *, tier="pro", status="active"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user):
    from app.models import APIKey

    raw = f"rec_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:8],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="b1",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_skill_version(db, slug, semver="1.0.0"):
    from app.models import Skill, SkillVersion

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="t",
        tier="free",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=sk.id,
        semver=semver,
        tarball_size_bytes=1,
        checksum_sha256="x" * 8,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return sk, v


def _mk_cookbook_with_skill(db, owner, skill):
    from app.models import Cookbook, CookbookSkill

    cb = Cookbook(id=uuid.uuid4(), name="cb", bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    db.add(CookbookSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db.flush()
    return cb


def test_stable_dead_before_any_report(db_session):
    """Baseline: with no promotion, stable channel returns None (the bug)."""
    sk, _ = _mk_skill_version(db_session, "b1-baseline")
    assert latest_version_for_channel(db_session, sk.id, "stable") is None
    # canary always sees it
    assert latest_version_for_channel(db_session, sk.id, "canary") == "1.0.0"


def test_clean_report_promotes_and_stable_advances(middleware_client, db_session):
    """A clean canary report promotes the version; stable channel then advances."""
    owner = _mk_user(db_session, tier="pro")
    key = _mk_key(db_session, owner)
    sk, _ = _mk_skill_version(db_session, "b1-promote", "1.0.0")
    cb = _mk_cookbook_with_skill(db_session, owner, sk)

    resp = middleware_client.post(
        f"/api/cookbooks/{cb.id}/reconcile-report",
        headers={"x-api-key": key},
        json={"slug": "b1-promote", "semver": "1.0.0", "outcome": "success"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recorded"] is True
    assert body["promoted_to_stable"] is True, f"gate: {body.get('gate_reason')}"

    # The dead channel is now live — stable advances to the promoted version.
    assert latest_version_for_channel(db_session, sk.id, "stable") == "1.0.0"


def test_canary_failure_blocks_promotion(middleware_client, db_session):
    """A reported failure must BLOCK promotion — the bad version never reaches stable."""
    owner = _mk_user(db_session, tier="pro")
    key = _mk_key(db_session, owner)
    sk, _ = _mk_skill_version(db_session, "b1-block", "2.0.0")
    cb = _mk_cookbook_with_skill(db_session, owner, sk)

    resp = middleware_client.post(
        f"/api/cookbooks/{cb.id}/reconcile-report",
        headers={"x-api-key": key},
        json={"slug": "b1-block", "semver": "2.0.0", "outcome": "reconcile_failed",
              "failure_reason": "boom"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted_to_stable"] is False
    # stable stays dead for this version (correctly — it failed canary).
    assert latest_version_for_channel(db_session, sk.id, "stable") is None


def test_invalid_outcome_422(middleware_client, db_session):
    owner = _mk_user(db_session, tier="pro")
    key = _mk_key(db_session, owner)
    sk, _ = _mk_skill_version(db_session, "b1-bad-outcome")
    cb = _mk_cookbook_with_skill(db_session, owner, sk)

    resp = middleware_client.post(
        f"/api/cookbooks/{cb.id}/reconcile-report",
        headers={"x-api-key": key},
        json={"slug": "b1-bad-outcome", "semver": "1.0.0", "outcome": "turbo"},
    )
    assert resp.status_code == 422


def test_non_owner_report_404(middleware_client, db_session):
    owner = _mk_user(db_session, tier="pro")
    other = _mk_user(db_session, tier="pro")
    other_key = _mk_key(db_session, other)
    sk, _ = _mk_skill_version(db_session, "b1-tenant")
    cb = _mk_cookbook_with_skill(db_session, owner, sk)

    resp = middleware_client.post(
        f"/api/cookbooks/{cb.id}/reconcile-report",
        headers={"x-api-key": other_key},
        json={"slug": "b1-tenant", "semver": "1.0.0", "outcome": "success"},
    )
    assert resp.status_code == 404


def test_admin_sweep_promotes_batch(db_session):
    """The sweep service batch-promotes everything with a passing gate."""
    from app.services.promotion import record_reconcile_event
    from app.services.promotion_sweep import run_promotion_sweep

    sk, _ = _mk_skill_version(db_session, "b1-sweep", "3.0.0")
    # Record a clean canary success directly (no request path).
    record_reconcile_event(db_session, skill_id=sk.id, semver="3.0.0", outcome="success")

    assert latest_version_for_channel(db_session, sk.id, "stable") is None
    result = run_promotion_sweep(db_session)
    assert result.promoted >= 1
    assert latest_version_for_channel(db_session, sk.id, "stable") == "3.0.0"
    # Idempotent: a second sweep promotes nothing new.
    result2 = run_promotion_sweep(db_session)
    assert result2.promoted == 0


def test_admin_sweep_route_requires_master(middleware_client, db_session):
    owner = _mk_user(db_session, tier="pro")
    key = _mk_key(db_session, owner)
    resp = middleware_client.post("/api/admin/promotion-sweep", headers={"x-api-key": key})
    assert resp.status_code == 403

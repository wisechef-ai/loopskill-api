"""bhint0823 (t_8ccbdbc5) — bundle fast-path onboarding hint.

Acceptance cases from the task spec:
  1. 3 same-IP same-bundle direct installs → hint appears with correct
     matched count (and X-LoopSkill-Bundle-Hint header on the 3rd response).
  2. Installs spread across bundles → no hint.
  3. Bundle-attributed installs (bundle_id NOT NULL) → no hint (they are
     already on the fast path; and they never count toward the trigger).

Additional guards:
  - <3 distinct skills → no hint (even same bundle).
  - Private/unpublished bundles never qualify (no data leak).
  - Events older than 24h don't count.
  - Response schema stays additive: every pre-existing InstallResponse field
    is still present and unchanged when the hint fires.
  - A direct install after two bundle installs from the same IP → no hint
    (bundle-attributed installs don't feed the direct pattern).
  - Smallest covering bundle wins; deterministic alphabetical tiebreak.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Bundle, BundleSkill, InstallEvent
from tests._app_factory import build_test_app
from tests.conftest import make_skill

HINT_IP = "203.0.113.77"  # TEST-NET-3 — never a real client


def _make_version(db: Session, skill, semver: str = "1.0.0"):
    from app.models import SkillVersion

    v = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver=semver,
        tarball_size_bytes=100,
        checksum_sha256="a" * 64,
    )
    db.add(v)
    db.flush()
    db.refresh(skill)
    return v


def _make_bundle(
    db: Session,
    slug: str | None,
    skills,
    *,
    visibility: str = "public",
):
    b = Bundle(
        id=uuid4(),
        name=slug or "unnamed",
        slug=slug,
        visibility=visibility,
    )
    db.add(b)
    db.flush()
    for s in skills:
        db.add(BundleSkill(id=uuid4(), bundle_id=b.id, skill_id=s.id, source="forked"))
    db.flush()
    return b


def _direct_install(client: TestClient, slug: str):
    """Fire a direct GET /api/skills/install (no bundle_id param)."""
    return client.get(f"/api/skills/install?slug={slug}")


@pytest.fixture()
def hint_client(db_session, monkeypatch):
    """TestClient on the canonical app factory (install route properly mounted)."""
    from app.config import settings

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, headers={"x-api-key": settings.API_KEY}, raise_server_exceptions=True)


# ── Acceptance 1: 3 same-IP same-bundle direct installs → hint ──────────────


def test_third_direct_install_same_bundle_returns_hint(db_session, hint_client):
    skills = [make_skill(db_session, slug=f"hint-a-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "loopskill-essentials", skills)

    # 1st and 2nd: no hint yet
    for i in range(2):
        r = _direct_install(hint_client, skills[i].slug)
        assert r.status_code == 200, r.text
        assert r.json().get("bundle_hint") is None
        assert "X-LoopSkill-Bundle-Hint" not in r.headers

    # 3rd crosses the >=3 threshold — hint appears
    r = _direct_install(hint_client, skills[2].slug)
    assert r.status_code == 200, r.text
    body = r.json()
    hint = body["bundle_hint"]
    assert hint is not None
    assert hint["slug"] == "loopskill-essentials"
    assert hint["matched"] == "3 of 3"
    assert "loopskill-essentials" in hint["install_all"]
    assert "install.sh" in hint["install_all"]
    # Header mirrors the slug
    assert r.headers.get("X-LoopSkill-Bundle-Hint") == "loopskill-essentials"

    # Additive schema: all pre-existing fields still present
    for field in ("slug", "version", "tarball_url", "expires_at", "manifest", "provenance_id"):
        assert field in body, f"missing pre-existing field {field}"


def test_matched_count_reflects_bundle_total(db_session, hint_client):
    """matched is 'N of TOTAL' where TOTAL = bundle members, not the hint set."""
    skills = [make_skill(db_session, slug=f"hint-b-{i}") for i in range(6)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "big-bundle", skills)

    for i in range(3):
        r = _direct_install(hint_client, skills[i].slug)
        assert r.status_code == 200, r.text
    hint = r.json()["bundle_hint"]
    assert hint["matched"] == "3 of 6"


# ── Acceptance 2: installs spread across bundles → no hint ──────────────────


def test_installs_spread_across_bundles_no_hint(db_session, hint_client):
    """3 direct installs where no single bundle contains ALL of them."""
    sa = [make_skill(db_session, slug=f"spread-a-{i}") for i in range(2)]
    sb = [make_skill(db_session, slug=f"spread-b-{i}") for i in range(2)]
    for s in sa + sb:
        _make_version(db_session, s)
    # bundle-one has a0,a1; bundle-two has b0,b1 — install a0,a1,b0
    _make_bundle(db_session, "bundle-one", sa)
    _make_bundle(db_session, "bundle-two", sb)

    for s in (sa[0], sa[1], sb[0]):
        r = _direct_install(hint_client, s.slug)
        assert r.status_code == 200, r.text
        assert r.json().get("bundle_hint") is None, "no single bundle covers all installs"
    assert "X-LoopSkill-Bundle-Hint" not in r.headers


# ── Acceptance 3: bundle-attributed installs → no hint ──────────────────────


def test_bundle_attribution_never_hints(db_session, hint_client):
    skills = [make_skill(db_session, slug=f"hint-c-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    bundle = _make_bundle(db_session, "loopskill-essentials", skills)

    for s in skills:
        r = hint_client.get(f"/api/skills/install?slug={s.slug}&bundle_id={bundle.id}")
        assert r.status_code == 200, r.text
        assert r.json().get("bundle_hint") is None
        assert "X-LoopSkill-Bundle-Hint" not in r.headers
    # And the events themselves are bundle-attributed, excluded from the trigger
    events = db_session.query(InstallEvent).filter(InstallEvent.client_ip.isnot(None)).all()
    assert events, "test setup error: no events recorded"
    assert all(e.bundle_id == bundle.id for e in events)


def test_mixed_direct_and_bundle_installs_no_hint(db_session, hint_client):
    """2 bundle installs + 1 direct from same IP → direct count = 1 < 3."""
    skills = [make_skill(db_session, slug=f"hint-d-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    bundle = _make_bundle(db_session, "loopskill-essentials", skills)

    for s in skills[:2]:
        r = hint_client.get(f"/api/skills/install?slug={s.slug}&bundle_id={bundle.id}")
        assert r.status_code == 200, r.text
        assert r.json().get("bundle_hint") is None
    r = _direct_install(hint_client, skills[2].slug)
    assert r.status_code == 200, r.text
    assert r.json().get("bundle_hint") is None


# ── Extra guards ────────────────────────────────────────────────────────────


def test_two_installs_only_no_hint(db_session, hint_client):
    skills = [make_skill(db_session, slug=f"hint-e-{i}") for i in range(2)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "loopskill-essentials", skills)

    for s in skills:
        r = _direct_install(hint_client, s.slug)
        assert r.status_code == 200, r.text
        assert r.json().get("bundle_hint") is None


def test_private_bundle_never_hints(db_session, hint_client):
    skills = [make_skill(db_session, slug=f"hint-f-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, None, skills, visibility="private")

    for s in skills:
        r = _direct_install(hint_client, s.slug)
        assert r.status_code == 200, r.text
        assert r.json().get("bundle_hint") is None


def test_superset_bundle_smallest_wins(db_session, hint_client):
    """Essentials (3) ⊂ mega (5): both cover the install set; smallest wins."""
    core = [make_skill(db_session, slug=f"hint-g-{i}") for i in range(3)]
    extra = [make_skill(db_session, slug=f"hint-gx-{i}") for i in range(2)]
    for s in core + extra:
        _make_version(db_session, s)
    _make_bundle(db_session, "essentials", core)
    _make_bundle(db_session, "mega-pack", core + extra)

    for s in core:
        r = _direct_install(hint_client, s.slug)
        assert r.status_code == 200, r.text
    hint = r.json()["bundle_hint"]
    assert hint is not None
    assert hint["slug"] == "essentials", "most specific (smallest) bundle must win"


def test_window_24h_only(db_session, hint_client):
    """Old events (>24h) don't satisfy the >=3 threshold."""
    from app.services.bundle_hint import compute_bundle_hint

    skills = [make_skill(db_session, slug=f"hint-h-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "loopskill-essentials", skills)

    now = datetime.now(UTC)
    two_days_ago = now - timedelta(hours=48)
    for s in skills:
        db_session.add(
            InstallEvent(
                id=uuid4(),
                skill_id=s.id,
                skill_slug=s.slug,
                version_semver="1.0.0",
                client_ip=HINT_IP,
                status="ok",
                created_at=two_days_ago,  # outside the window
            )
        )
    db_session.flush()

    hint = compute_bundle_hint(db_session, client_ip=HINT_IP, now=now)
    assert hint is None


# ── Unit-level: compute_bundle_hint ─────────────────────────────────────────


def test_compute_hint_none_for_missing_ip(db_session):
    from app.services.bundle_hint import compute_bundle_hint

    assert compute_bundle_hint(db_session, client_ip=None) is None
    assert compute_bundle_hint(db_session, client_ip="") is None


def test_smallest_bundle_tiebreak_deterministic(db_session, hint_client):
    """Equal-size covering bundles → alphabetical slug tiebreak, deterministic."""
    skills = [make_skill(db_session, slug=f"hint-i-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "zzz-bundle", skills)
    _make_bundle(db_session, "aaa-bundle", skills)

    for s in skills:
        r = _direct_install(hint_client, s.slug)
        assert r.status_code == 200, r.text
    hint = r.json()["bundle_hint"]
    assert hint is not None
    assert hint["slug"] == "aaa-bundle"

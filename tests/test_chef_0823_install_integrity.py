"""CHEF-2026-08-23-A (t_4a38fed9) — install-integrity: internal traffic must
not count as organic installs.

The B3 filter (APIKey.is_test) missed two classes of self-traffic that
/api/stats was counting as organic:

1. CI self-installs — the deploy pipeline's self-hosted runner executes on
   the production host, so its installs arrive anonymous with
   client_ip = the server's own public IPv4. 118 of 432 installs in the
   2026-08-16→2026-08-23 analysis window.
2. Agent-probe installs — self-serve agent registration mints shadow
   User(is_agent=True) + APIKey(name='agent:<name>') keys with is_test=false.
   The dogfood window's 255 internal installs came from adam-xps
   (195.128.172.227) through both anonymous requests and such keys.

Ground truth from prod (2026-08-23, verified via SSH psql):
  - installs_last_7d reported 432; true external 59 (13.7%)
  - super-memory lifetime "366" → 12 organic external

These tests pin the ONE shared predicate (app/install_integrity.py) across
every surface that adopts it: /api/stats totals + top_installed, search-card
counts (_install_counts_for), cookbook cards, creator external split, the
denormalised counter bump on all three write paths, and the boot-time
fail-closed config gate.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

# Internal IPs used across these tests. SERVER_PUBLIC_IP models the server's
# own address (CI runner); KNOWN_INTERNAL_IPS models dogfood boxes.
_SERVER_IP = "77.42.92.141"
_DOGFOOD_IP = "195.128.172.227"
_EXTERNAL_IP = "54.83.240.58"  # real AWS external installer from the window


@pytest.fixture
def integrity_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    monkeypatch.setattr("app.config.settings.SERVER_PUBLIC_IP", _SERVER_IP)
    monkeypatch.setattr("app.config.settings.KNOWN_INTERNAL_IPS", [_DOGFOOD_IP])
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_skill(db, slug, tier="free"):
    from app.models import Skill

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="t",
        category="devops",
        tier=tier,
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    return sk


def _mk_user(db, *, is_agent=False, email="human@example.com"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name=f"u-{uuid.uuid4().hex[:6]}",
        email=None if is_agent else email,
        is_agent=is_agent,
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, is_test=False, name="my-key"):
    """Create an APIKey row; returns (key_row, raw_key_string)."""
    import hashlib

    from app.models import APIKey

    raw = f"rec_live_{uuid.uuid4().hex}"
    k = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_prefix=raw[:12],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        is_active=True,
        is_test=is_test,
        name=name,
    )
    db.add(k)
    db.flush()
    return k, raw


def _mk_install(db, skill, *, key=None, client_ip: str | None = _EXTERNAL_IP, bundle_id=None):
    from app.models import InstallEvent

    db.add(
        InstallEvent(
            id=uuid.uuid4(),
            skill_id=skill.id,
            skill_slug=skill.slug,
            api_key_id=key.id if key else None,
            version_semver="1.0.0",
            client_ip=client_ip,
            bundle_id=bundle_id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()


# ── Acceptance case 1: anonymous install from internal IP excluded ─────────


def test_stats_excludes_anonymous_ci_install_from_server_ip(integrity_client, db_session):
    """The exact CI signature: no API key + client_ip == the server's own
    public IPv4. Must NOT count as organic (was 118 installs/7d on prod)."""
    sk = _mk_skill(db_session, "ci-smoke-skill")
    _mk_install(db_session, sk, key=None, client_ip=_SERVER_IP)  # CI self-install
    _mk_install(db_session, sk, key=None, client_ip=_EXTERNAL_IP)  # real user

    resp = integrity_client.get("/api/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_installs_lifetime"] == 1
    assert body["installs_last_7d"] == 1
    top = {t["slug"]: t["installs"] for t in body["top_installed"]}
    assert top.get("ci-smoke-skill") == 1


def test_stats_excludes_anonymous_install_from_dogfood_ip(integrity_client, db_session):
    """adam-xps dogfood traffic (195.128.172.227): 255 anonymous installs in
    the window, all previously counted organic. Must be excluded."""
    sk = _mk_skill(db_session, "dogfood-skill")
    _mk_install(db_session, sk, key=None, client_ip=_DOGFOOD_IP)
    _mk_install(db_session, sk, key=None, client_ip=_EXTERNAL_IP)

    body = integrity_client.get("/api/stats").json()
    assert body["total_installs_lifetime"] == 1
    assert body["installs_last_7d"] == 1


# ── Acceptance case 2: probe-key install excluded ───────────────────────────


def test_stats_excludes_agent_probe_key_install(integrity_client, db_session):
    """Self-registered agent keys (User.is_agent=True, name='agent:*',
    is_test=false) are not humans — their installs are not organic."""
    sk = _mk_skill(db_session, "probe-skill")
    agent_user = _mk_user(db_session, is_agent=True)
    probe_key, _raw = _mk_key(db_session, agent_user, name="agent:cold-agent-probe")
    human_user = _mk_user(db_session)
    human_key, _raw = _mk_key(db_session, human_user)

    _mk_install(db_session, sk, key=probe_key)  # probe — excluded
    _mk_install(db_session, sk, key=human_key)  # human — counted

    body = integrity_client.get("/api/stats").json()
    assert body["total_installs_lifetime"] == 1
    top = {t["slug"]: t["installs"] for t in body["top_installed"]}
    assert top.get("probe-skill") == 1


def test_stats_excludes_test_key_install(integrity_client, db_session):
    """Pre-existing B3 behaviour must keep holding (is_test keys excluded)."""
    sk = _mk_skill(db_session, "b3-still-works")
    tu = _mk_user(db_session)
    tk, _ = _mk_key(db_session, tu, is_test=True)
    hu = _mk_user(db_session)
    hk, _ = _mk_key(db_session, hu)
    _mk_install(db_session, sk, key=tk)
    _mk_install(db_session, sk, key=hk)
    body = integrity_client.get("/api/stats").json()
    assert body["total_installs_lifetime"] == 1


# ── Acceptance case 3: real external AWS install counted ───────────────────


def test_stats_counts_real_external_install(integrity_client, db_session):
    """The 30 distinct AWS us-east singles: anonymous, external IP, no key —
    these ARE organic and must keep counting."""
    sk = _mk_skill(db_session, "real-external")
    for ip in ("54.83.240.58", "98.83.72.38", "3.226.34.98"):
        _mk_install(db_session, sk, key=None, client_ip=ip)
    _mk_install(db_session, sk, key=None, client_ip=None)  # legacy NULL ip row

    body = integrity_client.get("/api/stats").json()
    assert body["total_installs_lifetime"] == 4
    assert body["installs_last_7d"] == 4


# ── Super-memory rank case: manufactured #1 must fall ──────────────────────


def test_manufactured_rank_falls(integrity_client, db_session):
    """Prod shape: super-memory 366 lifetime (12 organic), other skill 25
    organic. Post-fix, the other skill must outrank super-memory."""
    sm = _mk_skill(db_session, "super-memory")
    other = _mk_skill(db_session, "web-scraper-pro")
    for _ in range(36):
        _mk_install(db_session, sm, key=None, client_ip=_SERVER_IP)  # CI
        _mk_install(db_session, sm, key=None, client_ip=_DOGFOOD_IP)  # dogfood
    for _ in range(12):
        _mk_install(db_session, sm, key=None, client_ip=_EXTERNAL_IP)  # organic
    for _ in range(25):
        _mk_install(db_session, other, key=None, client_ip="52.1.0.{}".format(9))

    body = integrity_client.get("/api/stats").json()
    top = body["top_installed"]
    assert top[0]["slug"] == "web-scraper-pro", "organic leader must rank #1"
    sm_entry = next(t for t in top if t["slug"] == "super-memory")
    assert sm_entry["installs"] == 12


# ── Search-card counts (_install_counts_for) share the predicate ───────────


def test_install_counts_for_shares_predicate(integrity_client, db_session):
    from app._skill_helpers import _install_counts_for

    sk = _mk_skill(db_session, "counts-skill")
    agent_user = _mk_user(db_session, is_agent=True)
    probe_key, _raw = _mk_key(db_session, agent_user, name="agent:probe")
    _mk_install(db_session, sk, key=None, client_ip=_SERVER_IP)
    _mk_install(db_session, sk, key=probe_key, client_ip=_EXTERNAL_IP)
    _mk_install(db_session, sk, key=None, client_ip=_EXTERNAL_IP)
    db_session.commit()

    total, last_7d = _install_counts_for(db_session, [sk.id])[sk.id]
    assert total == 1
    assert last_7d == 1


# ── Write path: the counter bump honours the same definition ───────────────


def test_counter_bump_skips_internal_ip(integrity_client, db_session):
    """record_install_with_provenance from the server's own IP must record
    the InstallEvent but NOT bump Skill.install_count."""
    from app.models import Skill
    from app.services.provenance import record_install_with_provenance

    sk = _mk_skill(db_session, "bump-skill")
    before = db_session.query(Skill.install_count).filter(Skill.id == sk.id).scalar()

    from unittest.mock import MagicMock

    req = MagicMock()
    req.state.api_key_id = None
    req.client.host = _SERVER_IP
    # _real_client_ip with an untrusted (non-proxy) peer returns the peer itself.
    from app.utils.client_ip import _real_client_ip
    from app.config import settings as cfg

    assert _real_client_ip(req, cfg.TRUSTED_PROXY_CIDRS) == _SERVER_IP

    event, _prov = record_install_with_provenance(
        db_session, skill=sk, version_semver="1.0.0", request=req, source="direct", commit=False
    )
    assert event.client_ip == _SERVER_IP
    db_session.flush()
    after = db_session.query(Skill.install_count).filter(Skill.id == sk.id).scalar()
    assert after == before, "internal-IP install must not bump the public counter"


def test_counter_bump_skips_agent_probe_key(integrity_client, db_session):
    from app.models import Skill
    from app.services.provenance import record_install_with_provenance

    sk = _mk_skill(db_session, "bump-skill-2")
    agent_user = _mk_user(db_session, is_agent=True)
    probe_key, _raw = _mk_key(db_session, agent_user, name="agent:pkg-smoke")
    before = db_session.query(Skill.install_count).filter(Skill.id == sk.id).scalar()

    from unittest.mock import MagicMock

    req = MagicMock()
    req.state.api_key_id = probe_key.id
    req.client.host = _EXTERNAL_IP

    event, _prov = record_install_with_provenance(
        db_session, skill=sk, version_semver="1.0.0", request=req, source="direct", commit=False
    )
    db_session.flush()
    after = db_session.query(Skill.install_count).filter(Skill.id == sk.id).scalar()
    assert after == before, "agent-probe install must not bump the public counter"


def test_counter_bump_counts_external(integrity_client, db_session):
    from app.models import Skill
    from app.services.provenance import record_install_with_provenance

    sk = _mk_skill(db_session, "bump-skill-3")
    before = db_session.query(Skill.install_count).filter(Skill.id == sk.id).scalar()

    from unittest.mock import MagicMock

    req = MagicMock()
    req.state.api_key_id = None
    req.client.host = _EXTERNAL_IP

    record_install_with_provenance(
        db_session, skill=sk, version_semver="1.0.0", request=req, source="direct", commit=False
    )
    db_session.flush()
    after = (
        db_session.query(Skill.install_count).filter(Skill.id == sk.id).scalar()
    )
    assert after == before + 1, "external anonymous install must bump the counter"


# ── Cookbook counts + creator split share the predicate ─────────────────────


def test_cookbook_counts_share_predicate(integrity_client, db_session):
    from app._skill_helpers import _cookbook_install_counts
    from app.models import Bundle

    sk = _mk_skill(db_session, "cb-skill")
    cb = Bundle(
        id=uuid.uuid4(),
        name="cb",
        slug="cb-1",
        bundle_owner=None,
        visibility="public",
    )
    db_session.add(cb)
    db_session.flush()
    _mk_install(db_session, sk, key=None, client_ip=_SERVER_IP, bundle_id=cb.id)
    _mk_install(db_session, sk, key=None, client_ip=_EXTERNAL_IP, bundle_id=cb.id)
    db_session.commit()

    total, last_7d = _cookbook_install_counts(db_session, cb.id)
    assert total == 1 and last_7d == 1


def test_creator_external_split_shares_predicate(integrity_client, db_session):
    """GET /api/creators/me/stats with a user key: the bundle's
    installs_external must exclude internal-IP installs (raw total keeps 2)."""
    from app.models import Bundle

    owner = _mk_user(db_session)
    sk = _mk_skill(db_session, "creator-skill")
    cb = Bundle(
        id=uuid.uuid4(),
        name="ccb",
        slug="ccb-1",
        bundle_owner=owner.id,
        visibility="public",
    )
    db_session.add(cb)
    db_session.flush()
    _mk_install(db_session, sk, key=None, client_ip=_SERVER_IP, bundle_id=cb.id)
    _mk_install(db_session, sk, key=None, client_ip=_EXTERNAL_IP, bundle_id=cb.id)
    db_session.commit()

    owner_key_row, owner_raw_key = _mk_key(db_session, owner)
    db_session.commit()

    resp = integrity_client.get(
        "/api/creators/me/stats", headers={"x-api-key": owner_raw_key}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    entry = next(b for b in body["bundles"] if b["slug"] == "ccb-1")
    assert entry["installs_total"] == 2
    assert entry["installs_external"] == 1
    assert "is_agent" in body["internal_exclusion_rule"]


# ── Config gate: fail-closed boot ───────────────────────────────────────────


def test_config_gate_missing_server_ip_raises_in_prod():
    from app.config import Settings

    with pytest.raises(RuntimeError, match="SERVER_PUBLIC_IP"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes",
            API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
            JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
            OAUTH_REDIRECT_BASE="https://app.loopskill.io",
            COOKIES_SECURE=True,
            SERVER_PUBLIC_IP="",
        )


def test_config_gate_sqlite_dev_boot_untouched():
    from app.config import Settings

    s = Settings(_env_file=None, DATABASE_URL="sqlite:///./t.db", COOKIES_SECURE=False)
    assert s.KNOWN_INTERNAL_IPS == []


# ── Drift probe shares the organic truth ────────────────────────────────────


def test_drift_probe_truth_is_organic(integrity_client, db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.SERVER_PUBLIC_IP", _SERVER_IP)
    monkeypatch.setattr("app.config.settings.KNOWN_INTERNAL_IPS", [_DOGFOOD_IP])
    import scripts.install_count_drift_probe as probe

    sk = _mk_skill(db_session, "drift-skill")
    _mk_install(db_session, sk, key=None, client_ip=_SERVER_IP)
    _mk_install(db_session, sk, key=None, client_ip=_EXTERNAL_IP)
    db_session.commit()

    truth = probe.compute_truth(db_session)
    assert truth.get("drift-skill") == 1


# ── Migration fail-closed gate ──────────────────────────────────────────────


def test_migration_refuses_without_server_ip(monkeypatch):
    """The re-sync migration must refuse to run with no internal-IP config —
    otherwise it would bake the polluted counts in as organic."""
    import importlib.util
    import sys
    from pathlib import Path

    # Block app.config so the migration exercises its env fallback path.
    monkeypatch.delenv("WR_SERVER_PUBLIC_IP", raising=False)
    monkeypatch.delenv("WR_KNOWN_INTERNAL_IPS", raising=False)
    monkeypatch.setitem(sys.modules, "app.config", None)

    spec = importlib.util.spec_from_file_location(
        "chef_0823_resync_install_counts",
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "chef_0823_resync_install_counts.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with pytest.raises(RuntimeError, match="WR_SERVER_PUBLIC_IP"):
        mod._internal_ips()

"""Tests for spotify_0608 Ph E — install provenance + feedback routing.

Covers:
  - ProvenanceRecord model + migration columns (cookbook_id, attribution)
  - app/services/provenance.py: mint, record_install_with_provenance (counter +
    is_test integrity), resolve, route_targets_for_provenance
  - provenance_id returned on EVERY install transport (direct, cookbook single +
    bulk, MCP single + bulk)
  - per-skill provenance in bulk envelopes (R4 nit a)
  - deep-link 'unattributed' honest stamp vs attributed fetch-origin
  - feedback._resolve_feedback_target deterministic provenance routing (the
    "first cookbook" guess is DELETED)
  - skill-error provenance routing to curator repo
"""

from __future__ import annotations

import uuid
from typing import Generator
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import (
    APIKey,
    Base,
    Bundle,
    BundleSkill,
    InstallEvent,
    ProvenanceRecord,
    Skill,
    SkillVersion,
    User,
)
from app.services.provenance import (
    ATTR_ATTRIBUTED,
    ATTR_UNATTRIBUTED,
    mint_provenance,
    record_install_with_provenance,
    resolve_provenance,
    route_targets_for_provenance,
)


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


def _mk_user(db, tier="pro"):
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


def _mk_cookbook(db, owner, slug=None, **kw):
    cb = Bundle(
        id=uuid.uuid4(),
        name=kw.pop("name", "CB"),
        bundle_owner=owner.id if owner else None,
        slug=slug,
        visibility=kw.pop("visibility", "private"),
        **kw,
    )
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db, slug):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, install_count=0)
    db.add(s)
    db.commit()
    return s


def _mk_apikey(db, *, is_test=False, user=None):
    if user is None:
        user = _mk_user(db)
    k = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_prefix="rec_test",
        key_hash=uuid.uuid4().hex,
        name="k",
        is_test=is_test,
    )
    db.add(k)
    db.commit()
    return k


# ── model + service ──────────────────────────────────────────────────────────


def test_mint_and_resolve_roundtrip(db):
    s = _mk_skill(db, "alpha")
    ev = InstallEvent(skill_id=s.id, skill_slug=s.slug, version_semver="1.0.0")
    db.add(ev)
    db.flush()
    pid = mint_provenance(db, ev)
    db.commit()
    assert isinstance(pid, str) and len(pid) > 20
    resolved = resolve_provenance(db, pid)
    assert resolved is not None
    assert resolved.skill_id == s.id
    assert resolved.skill_slug == "alpha"
    assert resolved.attribution == ATTR_ATTRIBUTED


def test_resolve_unknown_returns_none(db):
    assert resolve_provenance(db, "nope-not-a-token") is None
    assert resolve_provenance(db, "") is None


def test_record_install_with_provenance_bumps_counter(db):
    s = _mk_skill(db, "counted")
    ev, pid = record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", request=None, source="cookbook", commit=True
    )
    db.refresh(s)
    assert s.install_count == 1
    assert ev.attribution == ATTR_ATTRIBUTED
    # provenance row exists
    assert db.query(ProvenanceRecord).filter(ProvenanceRecord.provenance_id == pid).first() is not None


def test_record_install_stamps_cookbook_id(db):
    owner = _mk_user(db)
    s = _mk_skill(db, "cb-skill")
    cb = _mk_cookbook(db, owner)
    ev, pid = record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", source="cookbook", cookbook_id=cb.id, commit=True
    )
    resolved = resolve_provenance(db, pid)
    assert resolved.bundle_id == cb.id


def test_unattributed_stamp(db):
    s = _mk_skill(db, "deep-link")
    ev, pid = record_install_with_provenance(
        db,
        skill=s,
        version_semver="external",
        source="external",
        attribution=ATTR_UNATTRIBUTED,
        commit=True,
    )
    resolved = resolve_provenance(db, pid)
    assert resolved.attribution == ATTR_UNATTRIBUTED


def test_is_test_key_does_not_inflate_counter(db):
    """Ph B §4.2 integrity preserved through the provenance path."""
    s = _mk_skill(db, "testkey-skill")
    tk = _mk_apikey(db, is_test=True)

    class _Req:
        client = None

        class state:
            api_key_id = tk.id

    ev, pid = record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", request=_Req(), source="direct", commit=True
    )
    db.refresh(s)
    assert s.install_count == 0  # test key recorded the event but did NOT bump
    # but the event + provenance still exist (audit trail)
    assert resolve_provenance(db, pid) is not None


def test_organic_key_bumps_counter(db):
    s = _mk_skill(db, "organic-skill")
    ok = _mk_apikey(db, is_test=False)

    class _Req:
        client = None

        class state:
            api_key_id = ok.id

    record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", request=_Req(), source="direct", commit=True
    )
    db.refresh(s)
    assert s.install_count == 1


# ── feedback routing ─────────────────────────────────────────────────────────


def test_route_targets_resolves_curator_repo(db):
    owner = _mk_user(db)
    s = _mk_skill(db, "routed")
    cb = _mk_cookbook(db, owner)
    cb.feedback_repo = "owner/their-repo"
    cb.feedback_mode = "pat"
    cb.feedback_pat_enc = "enc-blob"
    db.commit()
    _ev, pid = record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", source="cookbook", cookbook_id=cb.id, commit=True
    )
    targets = route_targets_for_provenance(db, pid)
    assert len(targets) == 1
    assert targets[0].repo == "owner/their-repo"
    assert targets[0].kind == "curator"
    assert targets[0].mode == "pat"


def test_route_targets_no_repo_configured_returns_empty(db):
    owner = _mk_user(db)
    s = _mk_skill(db, "noroute")
    cb = _mk_cookbook(db, owner)  # no feedback_repo
    _ev, pid = record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", source="cookbook", cookbook_id=cb.id, commit=True
    )
    assert route_targets_for_provenance(db, pid) == []


def test_route_targets_no_provenance_returns_empty(db):
    assert route_targets_for_provenance(db, None) == []
    assert route_targets_for_provenance(db, "unknown") == []


def test_resolve_feedback_target_uses_provenance_not_guess(db):
    """The old 'first cookbook the user owns' guess is GONE — routing is by
    provenance only. A user with a repo-configured cookbook gets NO routing
    unless they pass the provenance_id of an install from THAT cookbook."""
    from app.mcp.tools.feedback import _resolve_feedback_target

    owner = _mk_user(db)
    s = _mk_skill(db, "guess-skill")
    cb = _mk_cookbook(db, owner)
    cb.feedback_repo = "owner/repo-a"
    cb.feedback_mode = "pat"
    cb.feedback_pat_enc = "enc"
    db.commit()
    ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")

    # No provenance → no routing (the guess is deleted).
    assert _resolve_feedback_target(db, None, ctx, None) == (None, None, None)

    # With provenance from an install in that cookbook → deterministic route.
    _ev, pid = record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", source="cookbook", cookbook_id=cb.id, commit=True
    )
    repo, mode, enc = _resolve_feedback_target(db, None, ctx, pid)
    assert repo == "owner/repo-a"
    assert mode == "pat"


# ── transport: direct install returns provenance_id ──────────────────────────


def test_direct_install_returns_provenance(db):
    """install_routes returns a provenance_id in InstallResponse."""
    from app import install_routes

    s = _mk_skill(db, "direct-prov")
    v = SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="a" * 64)
    db.add(v)
    db.commit()

    class _State:
        api_key_id = None
        is_anonymous_free_install = False
        auth_ctx = None
        api_key_user_id = None

    class _Req:
        client = None
        state = _State()

    # patch the tier resolver + IP helper to keep the unit hermetic
    with (
        patch.object(install_routes, "_resolve_caller_tier_for_install", return_value="pro_plus"),
        patch.object(install_routes, "_count_today_installs", return_value=0),
        patch("app.utils.client_ip._real_client_ip", return_value=None),
    ):
        # s is public + has a version; pro_plus tier = unlimited installs
        resp = install_routes.install_skill(
            request=_Req(), slug="direct-prov", mode="files", version=None, ref=None, db=db
        )
    pid = resp.provenance_id if hasattr(resp, "provenance_id") else None
    assert pid, "direct install must return a provenance_id"
    assert resolve_provenance(db, pid) is not None


# ── transport: MCP cookbook install returns per-skill provenance ─────────────


def _attach(db, cb, skill, source="custom-added", pinned=None):
    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source=source, pinned_version=pinned))
    db.commit()


def test_mcp_cookbook_install_single_returns_provenance(db):
    from app.mcp.tools.bundle_install import recipes_cookbook_install

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "mcp-single")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="a" * 64))
    db.commit()
    _attach(db, cb, s)
    ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")
    out = recipes_cookbook_install(db=db, ctx=ctx, cookbook_id=str(cb.id), slug="mcp-single")
    assert out.get("provenance_id"), "MCP single install must return provenance_id"
    resolved = resolve_provenance(db, out["provenance_id"])
    assert resolved.bundle_id == cb.id  # stamped with the cookbook


def test_mcp_cookbook_install_bulk_per_skill_provenance(db):
    """R4 nit (a): provenance rides PER-SKILL under skills[], not top-level."""
    from app.mcp.tools.bundle_install import recipes_cookbook_install

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    for slug in ("bulk-a", "bulk-b"):
        sk = _mk_skill(db, slug)
        db.add(SkillVersion(id=uuid.uuid4(), skill_id=sk.id, semver="1.0.0", checksum_sha256="a" * 64))
        db.commit()
        _attach(db, cb, sk)
    ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")
    out = recipes_cookbook_install(db=db, ctx=ctx, cookbook_id=str(cb.id))
    assert "provenance_id" not in out  # NOT cookbook-top-level
    skills = out["skills"]
    assert len(skills) == 2
    pids = {s["provenance_id"] for s in skills}
    assert len(pids) == 2  # distinct per-skill provenance
    for s in skills:
        assert resolve_provenance(db, s["provenance_id"]) is not None


# ── transport: cookbook REST bulk + single ──────────────────────────────────


def test_cookbook_rest_bulk_install_per_skill_provenance(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    s = _mk_skill(db, "rest-bulk")
    db.add(SkillVersion(id=uuid.uuid4(), skill_id=s.id, semver="1.0.0", checksum_sha256="a" * 64))
    db.commit()
    _attach(db, cb, s)

    class _State:
        api_key_id = None

    class _Req:
        client = None
        state = _State()

    class _Ctx:
        is_master = False
        user_id = owner.id
        tier = "pro"

    # Bypass the cbt-scope + ownership resolution with direct stubs.
    with (
        patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route", return_value=None),
        patch.object(bundle_routes, "_resolve_owned_cookbook", return_value=cb),
    ):
        out = bundle_routes.install_cookbook(cookbook_id=str(cb.id), request=_Req(), db=db, ctx=_Ctx())
    assert out["skills"][0]["provenance_id"]
    resolved = resolve_provenance(db, out["skills"][0]["provenance_id"])
    assert resolved.bundle_id == cb.id


# ── feedback tool e2e: provenance routes to curator repo ─────────────────────


def test_feedback_tool_routes_via_provenance(db):
    from app.mcp.tools import feedback as fb

    owner = _mk_user(db)
    s = _mk_skill(db, "fb-skill")
    cb = _mk_cookbook(db, owner)
    cb.feedback_repo = "owner/fb-repo"
    cb.feedback_mode = "pat"
    cb.feedback_pat_enc = "enc"
    db.commit()
    _ev, pid = record_install_with_provenance(
        db, skill=s, version_semver="1.0.0", source="cookbook", cookbook_id=cb.id, commit=True
    )
    ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")

    captured = {}

    def _fake_dispatch_issue(repo, token, *, title, body, labels):
        captured["repo"] = repo
        return f"https://github.com/{repo}/issues/1"

    with (
        patch.object(fb.github_dispatch, "dispatch_issue", side_effect=_fake_dispatch_issue),
        patch("app.feedback_cred_vault.decrypt_pat", return_value="ghp_fake"),
    ):
        out = fb.recipes_feedback(
            db,
            category="install",
            message="skill broke on cold start",
            ctx=ctx,
            provenance_id=pid,
        )
    assert out["ok"] is True
    assert captured.get("repo") == "owner/fb-repo"  # routed to curator, not default


def test_feedback_tool_no_provenance_uses_default(db):
    from app.mcp.tools import feedback as fb

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    cb.feedback_repo = "owner/should-not-be-used"
    cb.feedback_mode = "pat"
    cb.feedback_pat_enc = "enc"
    db.commit()
    ctx = AuthContext(scope="user", user_id=owner.id, tier="pro")

    used_default = {"v": False}

    def _fake_dispatch_event(event, payload):
        used_default["v"] = True
        return True

    with (
        patch.object(fb.github_dispatch, "dispatch_event", side_effect=_fake_dispatch_event),
        patch.object(
            fb.github_dispatch, "dispatch_issue", side_effect=AssertionError("must not route to curator")
        ),
    ):
        out = fb.recipes_feedback(
            db,
            category="install",
            message="no provenance supplied here",
            ctx=ctx,
            provenance_id=None,
        )
    assert out["ok"] is True
    assert used_default["v"] is True  # fell through to default repo (no guess)

"""Every install rail refuses archived skills — #469 follow-up (2026-11-25).

PR #469 made the REST single-skill rail (``GET /api/skills/install``) 404 an
archived skill for EVERY caller, but the guard never left that one route: the
MCP twin ``loopskill_install`` still resolved archived public skills by slug
and handed out signed tarball URLs, and so did every other surface that
resolves a skill to hand out install artifacts. A withdrawn skill (archived
for a security problem with ``is_public`` left True) stayed installable by any
agent key over MCP.

This suite RED-proofs each surface found in the sweep and pins the ONE shared
predicate (``authz.skill_gone_for_install`` / ``authz.can_install``):

  * MCP ``loopskill_install``        → {"error": "not_found", "slug": ...}
    (the tool's existing no-access shape — no existence oracle), including
    for MASTER ctx (admin parity with the REST rail, pinned by
    test_archived_skill_guard_1116.py).
  * The federated branch is UNREACHABLE for an archived internal slug: the
    internal exact-match still wins, the row just answers not_found — it can
    never fall through to the external resolver (no re-resolution of a
    withdrawn ``github--*`` internal slug via federation).
  * MCP ``loopskill_bundle_install`` (single + bulk) — via the same
    ``authz.can_install`` call both paths already make.
  * REST cookbook rails: ``GET /api/cookbooks/{id}/skills/{slug}/install``
    and ``POST /api/cookbooks/{id}/install`` (dual-mounted at /api/bundles).
  * The well-known bridge (``hermes skills install well-known:`` / install.sh):
    index omits the entry, SKILL.md answers 404.
  * MCP ``loopskill_install_from_bundle`` (streaming verb 1): the entry is
    skipped and no InstallEvent is recorded for it.
  * The one-shot URL minters (``loopskill_sync._build_install_urls``,
    ``reconcile._attach_tarball_urls``): no fresh tarball URL for an archived
    skill — both callers already handle a missing URL per-slug.
  * Streaming compose (``_resolve_one_link_to_skills``): ``skill://<archived>``
    resolves as skill_not_found.

Controls in every section: the LIVE skill on the same rail keeps working.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_skill

# ── helpers ────────────────────────────────────────────────────────────────


def _seed_with_version(
    db,
    *,
    slug: str,
    is_public: bool = True,
    is_archived: bool = False,
    tier: str | None = None,
    readme: str | None = None,
):
    """Skill + one SkillVersion row — the minimum an install rail needs."""
    from app.models import SkillVersion

    sk = make_skill(db, slug=slug, is_public=is_public, tier=tier, is_archived=is_archived)
    if readme is not None:
        sk.readme = readme
    db.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=sk.id,
            semver="1.0.0",
            tarball_size_bytes=1024,
            checksum_sha256="deadbeef" * 8,
            created_at=datetime.now(UTC),
        )
    )
    db.flush()
    return sk


def _mk_bundle(db, *, slug: str | None = None, visibility: str = "private", owner_id=None):
    from app.models import Bundle

    cb = Bundle(
        id=uuid.uuid4(),
        name=f"CB-{uuid.uuid4().hex[:6]}",
        bundle_owner=owner_id,
        slug=slug,
        visibility=visibility,
    )
    db.add(cb)
    db.flush()
    return cb


def _attach(db, cb, skill, source: str = "custom-added"):
    from app.models import BundleSkill

    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source=source))
    db.flush()


@pytest.fixture
def client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


@pytest.fixture
def master_headers(monkeypatch):
    """REST rails sit behind APIKeyMiddleware; authenticate as the master key."""
    from app.config import settings

    key = getattr(settings, "API_KEY", None) or "archived-rails-master-key"
    monkeypatch.setattr(settings, "API_KEY", key, raising=False)
    return {"x-api-key": key}


def _master_ctx():
    from app.auth_ctx import AuthContext

    return AuthContext(scope="master")


# ── 1. the shared predicate ────────────────────────────────────────────────


def test_can_install_refuses_archived_even_for_master_ctx(db_session):
    """The predicate is scope-independent: archived == gone, admin included.

    RED on 8e67d78: can_install(master, archived public skill) returned True.
    """
    from app.authz import can_install

    sk = _seed_with_version(db_session, slug="rail-pred-archived", is_archived=True)
    assert can_install(_master_ctx(), sk, db=db_session) is False


def test_can_install_still_true_for_live_master_ctx(db_session):
    from app.authz import can_install

    sk = _seed_with_version(db_session, slug="rail-pred-live")
    assert can_install(_master_ctx(), sk, db=db_session) is True


# ── 2. MCP loopskill_install (the reported defect) ─────────────────────────


def test_mcp_install_archived_public_skill_answers_not_found(db_session):
    """RED on 8e67d78: returned a payload with a signed tarball_url."""
    from app.mcp.tools.install import loopskill_install

    _seed_with_version(db_session, slug="rail-mcp-archived", is_archived=True)
    out = loopskill_install(db_session, slug="rail-mcp-archived", ctx=_master_ctx())
    assert out == {"error": "not_found", "slug": "rail-mcp-archived"}


def test_mcp_install_archived_public_skill_refused_for_user_ctx_too(db_session):
    from app.auth_ctx import AuthContext
    from app.mcp.tools.install import loopskill_install

    _seed_with_version(db_session, slug="rail-mcp-archived-user", is_archived=True)
    out = loopskill_install(
        db_session,
        slug="rail-mcp-archived-user",
        ctx=AuthContext(scope="user", user_id=uuid.uuid4()),
    )
    assert out == {"error": "not_found", "slug": "rail-mcp-archived-user"}


def test_mcp_install_live_public_skill_still_works(db_session):
    from app.mcp.tools.install import loopskill_install

    _seed_with_version(db_session, slug="rail-mcp-live")
    out = loopskill_install(db_session, slug="rail-mcp-live", ctx=_master_ctx())
    assert "tarball_url" in out, out


def test_mcp_install_archived_slug_never_reaches_federated_resolver(db_session, monkeypatch):
    """An archived INTERNAL slug that parses as a federated ref (``github--*``)
    must answer not_found — the internal exact-match still wins, so the row can
    never fall through into the external resolver (no re-resolution of a
    withdrawn slug via federation)."""
    from app.mcp.tools.install import loopskill_install

    calls: list[tuple] = []

    def _explode(*a, **kw):  # pragma: no cover - must never run
        calls.append((a, kw))
        raise AssertionError("federated resolver reached for an archived internal slug")

    monkeypatch.setattr("app.services.external_install_resolver.resolve_external_install_full", _explode)

    _seed_with_version(db_session, slug="github--rail-pulled", is_archived=True)
    out = loopskill_install(db_session, slug="github--rail-pulled", ctx=_master_ctx())
    assert out == {"error": "not_found", "slug": "github--rail-pulled"}
    assert calls == []


# ── 3. MCP loopskill_bundle_install ────────────────────────────────────────


def test_mcp_bundle_install_single_archived_refused(db_session):
    """RED on 8e67d78: can_install passed for the archived public member and
    the tool returned a signed tarball_url."""
    from app.mcp.tools.bundle_install import (
        CookbookInstallError,
        loopskill_bundle_install,
    )

    cb = _mk_bundle(db_session)
    archived = _seed_with_version(db_session, slug="rail-bi-archived", is_archived=True)
    _attach(db_session, cb, archived)

    with pytest.raises(CookbookInstallError) as ei:
        loopskill_bundle_install(
            db=db_session, ctx=_master_ctx(), cookbook_id=str(cb.id), slug="rail-bi-archived"
        )
    # Same no-access shape the tool already uses (404, no oracle).
    assert ei.value.status == 404


def test_mcp_bundle_install_bulk_skips_archived_keeps_live(db_session):
    from app.mcp.tools.bundle_install import loopskill_bundle_install

    cb = _mk_bundle(db_session)
    archived = _seed_with_version(db_session, slug="rail-bib-archived", is_archived=True)
    live = _seed_with_version(db_session, slug="rail-bib-live")
    _attach(db_session, cb, archived)
    _attach(db_session, cb, live)

    out = loopskill_bundle_install(db=db_session, ctx=_master_ctx(), cookbook_id=str(cb.id))
    slugs = [s["slug"] for s in out["skills"]]
    assert "rail-bib-live" in slugs
    assert "rail-bib-archived" not in slugs


def test_mcp_bundle_install_single_live_still_works(db_session):
    from app.mcp.tools.bundle_install import loopskill_bundle_install

    cb = _mk_bundle(db_session)
    live = _seed_with_version(db_session, slug="rail-bi-live")
    _attach(db_session, cb, live)

    out = loopskill_bundle_install(
        db=db_session, ctx=_master_ctx(), cookbook_id=str(cb.id), slug="rail-bi-live"
    )
    assert out["slug"] == "rail-bi-live"
    assert "tarball_url" in out


# ── 4. REST cookbook rails ──────────────────────────────────────────────────


def test_rest_cookbook_single_skill_install_archived_404(client, db_session, master_headers):
    """RED on 8e67d78: 200 with a signed tarball_url."""
    cb = _mk_bundle(db_session)
    archived = _seed_with_version(db_session, slug="rail-rest-archived", is_archived=True)
    _attach(db_session, cb, archived)

    resp = client.get(f"/api/cookbooks/{cb.id}/skills/rail-rest-archived/install", headers=master_headers)
    assert resp.status_code == 404, resp.text


def test_rest_cookbook_single_skill_install_live_200(client, db_session, master_headers):
    cb = _mk_bundle(db_session)
    live = _seed_with_version(db_session, slug="rail-rest-live")
    _attach(db_session, cb, live)

    resp = client.get(f"/api/cookbooks/{cb.id}/skills/rail-rest-live/install", headers=master_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "rail-rest-live"


def test_rest_cookbook_bulk_install_skips_archived(client, db_session, master_headers):
    cb = _mk_bundle(db_session)
    archived = _seed_with_version(db_session, slug="rail-rest-bulk-archived", is_archived=True)
    live = _seed_with_version(db_session, slug="rail-rest-bulk-live")
    _attach(db_session, cb, archived)
    _attach(db_session, cb, live)

    resp = client.post(f"/api/cookbooks/{cb.id}/install", headers=master_headers)
    assert resp.status_code == 200, resp.text
    slugs = [s["slug"] for s in resp.json()["skills"]]
    assert "rail-rest-bulk-live" in slugs
    assert "rail-rest-bulk-archived" not in slugs


# ── 5. well-known bridge (agentskills.io / install.sh) ─────────────────────


def _mk_public_cb_with_members(db):
    cb = _mk_bundle(db, slug=f"rail-wk-{uuid.uuid4().hex[:8]}", visibility="public")
    archived = _seed_with_version(
        db, slug="rail-wk-archived", is_archived=True, tier="free", readme="# archived body\n"
    )
    live = _seed_with_version(db, slug="rail-wk-live", tier="free", readme="# live body\n")
    _attach(db, cb, archived)
    _attach(db, cb, live)
    return cb


def test_wellknown_index_omits_archived(client, db_session):
    cb = _mk_public_cb_with_members(db_session)

    resp = client.get(f"/api/cookbooks/public/{cb.slug}/.well-known/skills/index.json")
    assert resp.status_code == 200, resp.text
    names = [s["name"] for s in resp.json()["skills"]]
    assert "rail-wk-live" in names
    assert "rail-wk-archived" not in names


def test_wellknown_skill_md_archived_404_live_200(client, db_session):
    cb = _mk_public_cb_with_members(db_session)

    gone = client.get(f"/api/cookbooks/public/{cb.slug}/.well-known/skills/rail-wk-archived/SKILL.md")
    assert gone.status_code == 404, gone.text

    ok = client.get(f"/api/cookbooks/public/{cb.slug}/.well-known/skills/rail-wk-live/SKILL.md")
    assert ok.status_code == 200, ok.text
    assert "# live body" in ok.text


# ── 6. MCP loopskill_install_from_bundle (streaming verb 1) ────────────────


def test_install_from_bundle_skips_archived_records_only_live(db_session):
    """RED on 8e67d78: the archived member got a signed tarball_url AND an
    InstallEvent."""
    from app.models import InstallEvent
    from app.mcp.tools.bundle_stream import loopskill_install_from_bundle

    cb = _mk_bundle(db_session, slug=f"rail-stream-{uuid.uuid4().hex[:8]}", visibility="public")
    archived = _seed_with_version(db_session, slug="rail-stream-archived", is_archived=True)
    live = _seed_with_version(db_session, slug="rail-stream-live")
    _attach(db_session, cb, archived)
    _attach(db_session, cb, live)

    out = loopskill_install_from_bundle(db_session, link=f"bundle://{cb.slug}")
    slugs = [s["slug"] for s in out["skills"]]
    assert "rail-stream-live" in slugs
    assert "rail-stream-archived" not in slugs

    recorded = {e.skill_slug for e in db_session.query(InstallEvent).all()}
    assert "rail-stream-live" in recorded
    assert "rail-stream-archived" not in recorded


# ── 7. one-shot URL minters (sync / reconcile) ─────────────────────────────


def test_sync_build_install_urls_skips_archived(db_session):
    """RED on 8e67d78: minted a fresh signed tarball URL for the archived skill."""
    from app.mcp.tools.loopskill_sync import _build_install_urls

    archived = _seed_with_version(db_session, slug="rail-sync-archived", is_archived=True)
    live = _seed_with_version(db_session, slug="rail-sync-live")

    urls = _build_install_urls(
        db_session,
        [
            {"skill_id": archived.id, "slug": "rail-sync-archived", "to": "1.0.0"},
            {"skill_id": live.id, "slug": "rail-sync-live", "to": "1.0.0"},
        ],
    )
    by_slug = {u["slug"] for u in urls}
    assert "rail-sync-live" in by_slug
    assert "rail-sync-archived" not in by_slug


def test_reconcile_attach_tarball_urls_skips_archived(db_session):
    from app.services.reconcile import ReconcilePlan, _attach_tarball_urls

    _seed_with_version(db_session, slug="rail-rec-archived", is_archived=True)
    _seed_with_version(db_session, slug="rail-rec-live")

    plan = ReconcilePlan(
        update=[
            {"slug": "rail-rec-archived", "version": "1.0.0"},
            {"slug": "rail-rec-live", "version": "1.0.0"},
        ]
    )
    _attach_tarball_urls(db_session, plan)
    by_slug = {r["slug"]: r for r in plan.update}
    assert by_slug["rail-rec-live"].get("tarball_url"), "live skill must keep its URL"
    assert not by_slug["rail-rec-archived"].get("tarball_url")


# ── 8. streaming compose link resolution ───────────────────────────────────


def test_compose_link_archived_skill_unresolvable(db_session):
    """RED on 8e67d78: ``skill://<archived>`` resolved to the withdrawn skill."""
    from app.mcp.tools.bundle_stream import (
        CookbookInstallError,
        _resolve_one_link_to_skills,
    )

    _seed_with_version(db_session, slug="rail-compose-archived", is_archived=True)
    with pytest.raises(CookbookInstallError) as ei:
        _resolve_one_link_to_skills(db_session, "skill://rail-compose-archived")
    assert ei.value.status == 404

    # bare-token branch resolves the same way
    with pytest.raises(CookbookInstallError):
        _resolve_one_link_to_skills(db_session, "rail-compose-archived")


def test_compose_link_live_skill_resolves(db_session):
    from app.mcp.tools.bundle_stream import _resolve_one_link_to_skills

    live = _seed_with_version(db_session, slug="rail-compose-live")
    assert _resolve_one_link_to_skills(db_session, "skill://rail-compose-live") == [live]


# ── 9. Signed download URLs minted BEFORE the archive (PR #494 review F1) ──────


@pytest.fixture
def _dl_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


def _sign_download(version_id, slug: str) -> str:
    from itsdangerous import URLSafeTimedSerializer

    from app.config import settings

    s = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    return s.dumps({"slug": slug, "version_id": str(version_id), "mode": "install"})


def _only_version_id(db, skill):
    from app.models import SkillVersion

    return db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).one().id


def test_download_url_signed_while_live_stops_serving_after_archive(_dl_client, db_session):
    """RED on dea8b55: _download resolved the SkillVersion only, so a URL signed
    while the skill was live kept streaming after the skill was archived (until
    the token expired). Archived must answer like a missing version."""
    sk = _seed_with_version(db_session, slug="rail-dl-archived-later")
    token = _sign_download(_only_version_id(db_session, sk), sk.slug)
    sk.is_archived = True
    db_session.flush()

    resp = _dl_client.get(f"/api/skills/_download?token={token}")
    assert resp.status_code == 404
    assert resp.json().get("detail") == "Version not found"


def test_download_url_for_live_skill_passes_the_archive_gate(_dl_client, db_session):
    """Control: a live skill's token passes the archive gate and reaches the
    on-disk check (no tarball is seeded, so that check answers 404)."""
    sk = _seed_with_version(db_session, slug="rail-dl-live")
    token = _sign_download(_only_version_id(db_session, sk), sk.slug)

    resp = _dl_client.get(f"/api/skills/_download?token={token}")
    assert resp.status_code == 404
    assert "Tarball missing on disk" in str(resp.json().get("detail"))


def test_mcp_bundle_install_single_archived_answers_like_missing(db_session):
    """PR #494 review F2: an archived member must answer skill_not_found (the
    missing-slug code), not skill_not_in_bundle, so the error code reveals
    nothing about the row."""
    from app.mcp.tools.bundle_install import CookbookInstallError, loopskill_bundle_install

    cb = _mk_bundle(db_session)
    archived = _seed_with_version(db_session, slug="rail-bi-archived-code", is_archived=True)
    _attach(db_session, cb, archived)

    with pytest.raises(CookbookInstallError) as archived_err:
        loopskill_bundle_install(
            db=db_session, ctx=_master_ctx(), cookbook_id=str(cb.id), slug="rail-bi-archived-code"
        )
    with pytest.raises(CookbookInstallError) as missing_err:
        loopskill_bundle_install(
            db=db_session, ctx=_master_ctx(), cookbook_id=str(cb.id), slug="rail-bi-never-existed"
        )
    assert archived_err.value.code == missing_err.value.code == "skill_not_found"
    assert archived_err.value.status == missing_err.value.status == 404

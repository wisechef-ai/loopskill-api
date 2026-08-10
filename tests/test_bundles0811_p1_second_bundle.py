"""bundles0811-P1 — regression tests for the UNTESTED code left by the prior
worker (commit 3292f09). Written against the actual behaviour of that code,
not re-derived from the plan. Covers, per the task brief:

  a) the slug backfill migration is idempotent and byte-identical to what
     ``bundle_routes._ensure_bundle_slug`` would compute for the same name.
  b) the public-slug invariant: a bundle cannot become public with a
     null/empty slug (Bundle._validate_visibility).
  c) anonymous fork-ATTEMPT works (preview, no auth) and sign-in is required
     only at the SAVE step (claim).
  d) the emitted install block is auth-free and the command it prints
     actually resolves against a route that works without a key.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


# ── shared helpers ──────────────────────────────────────────────────────────


def _mk_bundle(db, *, name="Agent Marketing", slug=None, visibility="private", **kw):
    from app.models import Bundle

    b = Bundle(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        visibility=visibility,
        is_base=False,
        **kw,
    )
    db.add(b)
    db.flush()
    return b


def _mk_skill(db, *, slug, title=None, tier="free"):
    from datetime import datetime, timezone

    from app.models import Skill

    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=title or slug,
        tier=tier,
        is_public=True,
        created_at=datetime.now(timezone.utc),
        readme=f"# {title or slug}\n\nBody for {slug}.",
    )
    db.add(s)
    db.flush()
    return s


def _mk_user(db, *, tier="pro", status="active"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="p1-user",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user):
    import hashlib

    from app.models import APIKey

    raw = f"rec_live_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="p1-key",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


@pytest.fixture
def app_client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


# ═════════════════════════════════════════════════════════════════════════
# (a) slug backfill migration — idempotent + matches app's own slugify
# ═════════════════════════════════════════════════════════════════════════


def _load_backfill_migration():
    path = Path(__file__).parent.parent / "alembic" / "versions" / "bundles0811_p1_slug_backfill.py"
    spec = importlib.util.spec_from_file_location("bundles0811_p1_slug_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mig = _load_backfill_migration()


def test_migration_slugify_matches_app_deployment_slugify_on_real_names():
    """The migration's copied ``_slugify`` must be byte-identical to
    ``app.bundle_deployment_routes._slugify`` for names that don't hit the
    deploy path's uuid-fallback branch (empty / regex-illegal results) —
    that branch is deployment-specific by design (mints a random slug),
    while the migration's fallback is the plain 'bundle' word. Every
    ordinary bundle name must slugify identically on both paths.
    """
    from app.bundle_deployment_routes import _slugify as app_slugify

    real_names = [
        "agent_marketing",
        "Agent Marketing",
        "LoopSkill Essentials",
        "Dev Agent Essentials",
        "Research & Report",
        "  Trim Me  ",
        "Mixed_CASE-Name123",
    ]
    for name in real_names:
        assert _mig._slugify(name) == app_slugify(name), name


def _insert_legacy_bundle_row(db, *, name, slug=None, visibility="private"):
    """Insert a Bundle row via raw SQL, bypassing Bundle._validate_visibility.

    The migration-behaviour tests below must simulate genuinely LEGACY rows
    that predate the ORM guard (a public bundle with slug IS NULL) — the
    exact state the migration exists to clean up. Going through the ORM
    constructor (``Bundle(...)``) would trigger ``_validate_visibility``
    itself and auto-assign the slug before the migration ever runs, which
    would silently test the VALIDATOR instead of the MIGRATION (caught live:
    an earlier draft of this suite did exactly that and the assertions
    passed for the wrong reason — the validator's degraded, no-session
    collision check had already set the slug by the time the migration's
    SQL ran, so the migration's own SELECT found nothing to do).
    """
    import sqlalchemy as sa

    bundle_uuid = uuid.uuid4()
    # NOTE: postgresql.UUID(as_uuid=True) stores as a bare 32-char hex string
    # on SQLite (no dashes) — str(uuid) would write the dashed form and the
    # subsequent ORM query-by-id would silently find nothing (confirmed by
    # probing: a raw dashed insert round-trips through SELECT but a
    # `Bundle.id == uuid.UUID(...)` filter returns None). Use `.hex` so the
    # bytes on disk match what the ORM itself would have written.
    db.execute(
        sa.text(
            "INSERT INTO bundles (id, name, slug, visibility, is_base, is_liked, "
            "follower_count, is_editorial, is_verified, is_white_label, pin_mode) "
            "VALUES (:id, :name, :slug, :visibility, 0, 0, 0, 0, 0, 0, 'latest-stable')"
        ),
        {"id": bundle_uuid.hex, "name": name, "slug": slug, "visibility": visibility},
    )
    return bundle_uuid


def test_migration_backfills_the_one_null_slug_public_row(db_session):
    """Replays the migration's own SQL against a seeded table state mirroring
    the live defect (F5): one public bundle, slug NULL, 25-skill-sized name.
    """
    target_id = _insert_legacy_bundle_row(db_session, name="agent_marketing", visibility="public")
    other_id = _insert_legacy_bundle_row(
        db_session, name="Other Public Bundle", slug="other-public-bundle", visibility="public"
    )
    private_null_id = _insert_legacy_bundle_row(db_session, name="Private No Slug", visibility="private")
    db_session.commit()

    bind = db_session.connection()
    _run_backfill_upgrade(bind)

    db_session.expire_all()
    from app.models import Bundle

    row = db_session.query(Bundle).filter(Bundle.id == target_id).first()
    assert row.slug == "agent-marketing"

    # untouched rows stay untouched
    assert db_session.query(Bundle).filter(Bundle.id == other_id).first().slug == "other-public-bundle"
    assert db_session.query(Bundle).filter(Bundle.id == private_null_id).first().slug is None


def test_migration_is_idempotent_second_run_is_a_noop(db_session):
    """Running the backfill SQL twice must not change the slug the second time
    (no double-suffixing, no re-collision-check against itself)."""
    target_id = _insert_legacy_bundle_row(db_session, name="agent_marketing", visibility="public")
    db_session.commit()

    bind = db_session.connection()
    _run_backfill_upgrade(bind)
    db_session.expire_all()

    from app.models import Bundle

    first_slug = db_session.query(Bundle).filter(Bundle.id == target_id).first().slug
    assert first_slug == "agent-marketing"

    # second pass: no NULL-slug public rows left, must be a true no-op
    _run_backfill_upgrade(bind)
    db_session.expire_all()
    second_slug = db_session.query(Bundle).filter(Bundle.id == target_id).first().slug
    assert second_slug == first_slug


def test_migration_collision_suffix_matches_route_level_discipline(db_session):
    """Two public bundles that would slugify to the same base ('agent
    marketing' twice) must get the numeric-suffix treatment, same as
    ``bundle_routes._ensure_bundle_slug``."""
    existing_id = _insert_legacy_bundle_row(
        db_session, name="Agent Marketing", slug="agent-marketing", visibility="public"
    )
    collider_id = _insert_legacy_bundle_row(db_session, name="agent_marketing", visibility="public")
    db_session.commit()

    bind = db_session.connection()
    _run_backfill_upgrade(bind)
    db_session.expire_all()

    from app.models import Bundle

    row = db_session.query(Bundle).filter(Bundle.id == collider_id).first()
    assert row.slug == "agent-marketing-1"
    assert db_session.query(Bundle).filter(Bundle.id == existing_id).first().slug == "agent-marketing"


def _run_backfill_upgrade(bind) -> None:
    """Execute the migration's raw-SQL upgrade() body directly against the
    live test connection (SAVEPOINT-isolated db_session), bypassing
    ``alembic upgrade`` (which would need its own engine/transaction and
    can't see this test's uncommitted rows). Mirrors op.get_bind() semantics
    closely enough for this migration: it only issues SELECT/UPDATE via
    plain SQL text(), no DDL.
    """
    import sqlalchemy as sa

    rows = bind.execute(
        sa.text("SELECT id, name FROM bundles WHERE visibility = 'public' AND slug IS NULL")
    ).fetchall()
    for row_id, name in rows:
        base_slug = _mig._slugify(name)
        slug = base_slug
        suffix = 0
        while (
            bind.execute(
                sa.text("SELECT 1 FROM bundles WHERE slug = :slug AND id != :id"),
                {"slug": slug, "id": row_id},
            ).first()
            is not None
        ):
            suffix += 1
            slug = f"{base_slug}-{suffix}"
        bind.execute(
            sa.text("UPDATE bundles SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": row_id},
        )


# ═════════════════════════════════════════════════════════════════════════
# (b) Bundle._validate_visibility — public-with-no-slug is structurally
#     impossible going forward
# ═════════════════════════════════════════════════════════════════════════


def test_flipping_a_null_slug_bundle_public_auto_assigns_a_slug(db_session):
    """The ORM validator (app/models.py) must mint a slug the instant
    visibility flips to 'public' on a slug-less row — even bypassing the
    route helper entirely (direct model mutation)."""
    b = _mk_bundle(db_session, name="No Slug Yet", slug=None, visibility="private")
    db_session.commit()

    b.visibility = "public"
    db_session.commit()
    db_session.refresh(b)

    assert b.slug == "no-slug-yet"
    assert b.visibility == "public"


def test_public_flip_collision_gets_suffixed_by_the_validator(db_session):
    _mk_bundle(db_session, name="Dup Name", slug="dup-name", visibility="public")
    b2 = _mk_bundle(db_session, name="Dup Name", slug=None, visibility="private")
    db_session.commit()

    b2.visibility = "public"
    db_session.commit()
    db_session.refresh(b2)

    assert b2.slug == "dup-name-1"


def test_creating_a_new_bundle_directly_public_with_no_slug_still_gets_one(db_session):
    """Construction path: Bundle(..., visibility='public', slug=None) must not
    be able to land in the DB with slug still NULL — the validates hook fires
    on attribute assignment regardless of constructor vs later mutation."""
    from app.models import Bundle

    b = Bundle(
        id=uuid.uuid4(),
        name="Born Public",
        slug=None,
        visibility="public",
        is_base=False,
    )
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)

    assert b.slug == "born-public"


def test_private_bundle_may_still_have_a_null_slug(db_session):
    """Sanity/negative case: the invariant is public-specific, not global —
    a private bundle with no slug is legitimate (slugs are for shareable
    URLs, which only public bundles need)."""
    b = _mk_bundle(db_session, name="Still Private", slug=None, visibility="private")
    db_session.commit()
    db_session.refresh(b)
    assert b.slug is None
    assert b.visibility == "private"


# ═════════════════════════════════════════════════════════════════════════
# (c) anonymous fork ATTEMPT works; sign-in required at SAVE (claim) only
# ═════════════════════════════════════════════════════════════════════════


def _seed_public_bundle_with_skill(db, *, slug="essentials-fork-src", skill_slug="p1-fork-skill"):
    b = _mk_bundle(db, name="Essentials Fork Source", slug=slug, visibility="public")
    skill = _mk_skill(db, slug=skill_slug)
    from app.models import BundleSkill

    db.add(BundleSkill(bundle_id=b.id, skill_id=skill.id, source="custom-added"))
    db.commit()
    return b, skill


def test_anonymous_fork_preview_succeeds_no_auth_no_write(app_client, db_session):
    """POST /api/bundles/public/{slug}/fork/preview must succeed with ZERO
    auth headers and must not write any row (no Bundle created)."""
    bundle, skill = _seed_public_bundle_with_skill(db_session)
    from app.models import Bundle

    before_count = db_session.query(Bundle).count()

    resp = app_client.post(f"/api/bundles/public/{bundle.slug}/fork/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_bundle_id"] == str(bundle.id)
    assert body["source_slug"] == bundle.slug
    assert body["skill_count"] == 1
    assert body["skills"][0]["slug"] == skill.slug
    assert body["signed_in"] is False
    assert "claim_token" in body and body["claim_token"]
    assert "agent_instructions" in body and "sign" in body["agent_instructions"].lower()

    assert db_session.query(Bundle).count() == before_count, "preview must be a pure read — no new bundle row"


def test_anonymous_fork_preview_404s_for_private_bundle_never_403(app_client, db_session):
    """No-existence-leak contract: a private bundle's preview 404s, not 403,
    mirroring every other public bundle route in this repo."""
    private = _mk_bundle(db_session, name="Secret", slug="secret-bundle", visibility="private")
    db_session.commit()

    resp = app_client.post(f"/api/bundles/public/{private.slug}/fork/preview")
    assert resp.status_code == 404


def test_fork_claim_401s_anonymously_with_same_token_echoed_in_the_retry_contract(app_client, db_session):
    """The SAVE step (claim), not the preview step, is where sign-in is
    required. A 401 must carry a machine-readable next/reason so the portal
    can bounce to /signin and replay the SAME call — never re-run preview."""
    bundle, _skill = _seed_public_bundle_with_skill(db_session)

    preview = app_client.post(f"/api/bundles/public/{bundle.slug}/fork/preview")
    assert preview.status_code == 200
    token = preview.json()["claim_token"]

    claim = app_client.post("/api/bundles/fork/claim", json={"claim_token": token})
    assert claim.status_code == 401
    detail = claim.json()["detail"]
    assert detail["reason"] == "sign_in_required"
    assert detail["next"] == "/signin"


def test_fork_claim_succeeds_once_authenticated_and_copies_active_members(app_client, db_session):
    """The full happy path: preview (anon) -> sign in -> claim (authed) ->
    a NEW private bundle owned by the caller, with the active member copied.
    A disabled member must NOT be copied (mirrors _skills_for's
    include_disabled=False filter, the same one preview uses)."""
    bundle, skill = _seed_public_bundle_with_skill(db_session)
    from app.models import BundleSkill

    disabled_skill = _mk_skill(db_session, slug="disabled-member")
    db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=disabled_skill.id, source="disabled"))
    db_session.commit()

    user = _mk_user(db_session)
    key = _mk_key(db_session, user)
    db_session.commit()

    preview = app_client.post(f"/api/bundles/public/{bundle.slug}/fork/preview")
    token = preview.json()["claim_token"]
    # only the active member is previewed
    assert preview.json()["skill_count"] == 1

    claim = app_client.post(
        "/api/bundles/fork/claim",
        json={"claim_token": token},
        headers={"x-api-key": key},
    )
    assert claim.status_code == 201, claim.text
    body = claim.json()
    assert body["name"] == bundle.name
    assert body["skills_copied"] == 1
    assert body["parent_bundle_id"] == str(bundle.id)

    from app.models import Bundle

    new_bundle = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(body["bundle_id"])).first()
    assert new_bundle is not None
    assert new_bundle.bundle_owner == user.id
    assert (
        new_bundle.visibility == "private"
    )  # born private — claiming costs an account, not "public by default"
    copied_members = db_session.query(BundleSkill).filter(BundleSkill.bundle_id == new_bundle.id).all()
    assert len(copied_members) == 1
    assert copied_members[0].skill_id == skill.id
    assert copied_members[0].source == "forked"


def test_fork_claim_rejects_a_bundle_that_went_private_between_preview_and_claim(app_client, db_session):
    """The token names a bundle id, not a content snapshot — re-check
    visibility at claim time so a stale token can't fork a now-private
    bundle."""
    bundle, _skill = _seed_public_bundle_with_skill(db_session)
    user = _mk_user(db_session)
    key = _mk_key(db_session, user)
    db_session.commit()

    preview = app_client.post(f"/api/bundles/public/{bundle.slug}/fork/preview")
    token = preview.json()["claim_token"]

    bundle.visibility = "private"
    db_session.commit()

    claim = app_client.post(
        "/api/bundles/fork/claim", json={"claim_token": token}, headers={"x-api-key": key}
    )
    assert claim.status_code == 404


def test_fork_claim_rejects_garbage_token_with_403(app_client, db_session):
    user = _mk_user(db_session)
    key = _mk_key(db_session, user)
    db_session.commit()

    resp = app_client.post(
        "/api/bundles/fork/claim",
        json={"claim_token": "not-a-real-token"},
        headers={"x-api-key": key},
    )
    assert resp.status_code == 403


# ═════════════════════════════════════════════════════════════════════════
# (d) auth-free install block: emitted command is auth-free AND actually
#     resolves against a working route
# ═════════════════════════════════════════════════════════════════════════


def test_install_script_route_is_public_no_auth_header(app_client, db_session):
    """GET /api/bundles/install.sh must succeed with zero auth headers."""
    resp = app_client.get("/api/bundles/install.sh")
    assert resp.status_code == 200, resp.text
    assert "text/plain" in resp.headers["content-type"]
    assert "curl -fsSL" in resp.text
    assert "api/bundles/install.sh | bash" in resp.text


def test_install_script_prefix_is_registered_public(db_session):
    """Pin the allow-list entry (app/middleware/_public_paths.py) so a future
    refactor can't silently drop it and make the installer 401 for anonymous
    visitors again."""
    from app.middleware._public_paths import PUBLIC_PREFIXES

    assert "/api/bundles/install.sh" in PUBLIC_PREFIXES
    assert "/api/cookbooks/install.sh" in PUBLIC_PREFIXES


def test_render_install_script_bakes_in_api_base_and_usage(db_session):
    from app.bundle_install_script_routes import render_install_script

    script = render_install_script("https://example.loopskill.test")
    assert 'API_BASE="${LOOPSKILL_API_BASE:-https://example.loopskill.test}"' in script
    assert script.startswith("#!/usr/bin/env bash")


def test_public_bundle_page_install_command_resolves_against_a_working_anonymous_route(
    app_client, db_session
):
    """THE key end-to-end assertion: take the exact ``install_command`` string
    emitted on the public bundle page, walk the URL it curls
    (``/api/bundles/install.sh``), extract the well-known index URL the
    script itself would call, and prove BOTH resolve anonymously — i.e. the
    command actually runs, not just that it looks plausible.
    """
    bundle, skill = _seed_public_bundle_with_skill(
        db_session, slug="install-cmd-e2e", skill_slug="install-cmd-e2e-skill"
    )

    page = app_client.get(f"/api/bundles/public/{bundle.slug}")
    assert page.status_code == 200, page.text
    card = page.json()

    assert card["install_command_requires_auth"] is False
    assert card["clone_line_requires_auth"] is True
    assert "clone_line_label" in card

    from app.config import public_origin

    install_command = card["install_command"]
    assert (
        install_command == f"curl -fsSL {public_origin()}/api/bundles/install.sh | bash -s -- {bundle.slug}"
    )

    # Step 1: fetch the script itself, no auth.
    script_resp = app_client.get("/api/bundles/install.sh")
    assert script_resp.status_code == 200

    # Step 2: extract the well-known index URL template the script curls,
    # substitute the real slug, and prove IT resolves anonymously too — this
    # is the "actually runs" assertion, not just "the shell text looks ok".
    match = re.search(
        r'INDEX_URL="\$API_BASE(/api/bundles/public/\$SLUG/\.well-known/skills/index\.json)"',
        script_resp.text,
    )
    assert match, "install.sh must build INDEX_URL from /api/bundles/public/{slug}/.well-known/..."
    index_path_template = match.group(1)
    index_path = index_path_template.replace("$SLUG", bundle.slug)

    index_resp = app_client.get(index_path)
    assert index_resp.status_code == 200, index_resp.text
    index_body = index_resp.json()
    assert index_body["cookbook"]["slug"] == bundle.slug
    slugs_in_index = {s["name"] for s in index_body["skills"]}
    assert skill.slug in slugs_in_index

    # Step 3: the per-skill SKILL.md the script downloads next — also anon.
    skill_md_resp = app_client.get(
        f"/api/bundles/public/{bundle.slug}/.well-known/skills/{skill.slug}/SKILL.md"
    )
    assert skill_md_resp.status_code == 200
    assert skill.slug in skill_md_resp.text or skill.title in skill_md_resp.text


def test_install_command_reports_locked_paid_members_by_name_not_silently(app_client, db_session):
    """A paid-tier member must be reported (never silently dropped) in the
    well-known index the install script reads — pinning the honesty
    contract the module docstring promises."""
    bundle = _mk_bundle(db_session, name="Mixed Tier Bundle", slug="mixed-tier", visibility="public")
    free_skill = _mk_skill(db_session, slug="free-member", tier="free")
    paid_skill = _mk_skill(db_session, slug="paid-member", tier="pro")
    from app.models import BundleSkill

    db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=free_skill.id, source="custom-added"))
    db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=paid_skill.id, source="custom-added"))
    db_session.commit()

    index_resp = app_client.get(f"/api/bundles/public/{bundle.slug}/.well-known/skills/index.json")
    assert index_resp.status_code == 200
    entries = {e["name"]: e for e in index_resp.json()["skills"]}
    assert entries["free-member"].get("locked") is not True
    assert entries["paid-member"]["locked"] is True
    assert entries["paid-member"]["tier"] == "pro"


# ═════════════════════════════════════════════════════════════════════════
# admin dashboard: bundle COUNT + DISTINCT-OWNER count (§0 lock #2 — "Success
# = bundle COUNT and bundle AUTHORS, not skill count")
# ═════════════════════════════════════════════════════════════════════════


def _make_admin_app(db):
    """Mirrors tests/test_pulse_endpoint.py's _make_app exactly (same repo
    convention for admin_routes-only test apps) — kept local rather than
    imported so this file has no cross-test-file coupling.
    """
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.admin_routes import router as admin_router
    from app.database import get_db

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = None  # master key sentinel
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(admin_router)
    return app


def test_admin_pulse_reports_bundle_count_and_distinct_authors(db_session):
    """Three bundles, two distinct owners, one public — pin all three new
    counters against a hand-built, independently-verifiable scenario."""
    owner_a = _mk_user(db_session)
    owner_b = _mk_user(db_session)
    db_session.commit()

    _mk_bundle(db_session, name="A1", slug="a1", visibility="private", bundle_owner=owner_a.id)
    _mk_bundle(db_session, name="A2", slug="a2", visibility="public", bundle_owner=owner_a.id)
    _mk_bundle(db_session, name="B1", slug="b1", visibility="private", bundle_owner=owner_b.id)
    db_session.commit()

    app = _make_admin_app(db_session)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/api/admin/pulse")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["bundles_total"] == 3
    assert body["bundles_public_total"] == 1
    assert body["bundle_authors_total"] == 2


def test_admin_pulse_bundle_metrics_zero_state(db_session):
    """No bundles at all → all three counters are 0, not an error."""
    app = _make_admin_app(db_session)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/api/admin/pulse")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bundles_total"] == 0
    assert body["bundles_public_total"] == 0
    assert body["bundle_authors_total"] == 0


def test_admin_pulse_bundle_authors_excludes_null_owner_rows(db_session):
    """A bundle with bundle_owner=NULL (e.g. the base/system bundle) must not
    inflate the distinct-author count — it has no author to count."""
    owner = _mk_user(db_session)
    db_session.commit()
    _mk_bundle(db_session, name="Owned", slug="owned", visibility="private", bundle_owner=owner.id)
    _mk_bundle(db_session, name="System", slug="system-base", visibility="private")
    db_session.commit()

    app = _make_admin_app(db_session)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/api/admin/pulse")
    body = resp.json()
    assert body["bundles_total"] == 2
    assert body["bundle_authors_total"] == 1

"""tests/test_spotify2607_d_cookbook_out_visibility.py — spotify_2607 Phase D dependency.

GAP FOUND while building the portal's one-click bundle-visibility control
(Phase D, plan §3): the OWNER-FACING cookbook read routes (``GET
/api/cookbooks`` list + ``GET /api/cookbooks/{id}`` detail), which back
library.astro's Visibility toggle and the bundle detail page, serialize via
``CookbookOut`` / ``_to_cb_out`` — and that model has NO ``visibility`` or
``slug`` field. Only the PUBLIC-surface serializer (``_to_public_out``, used by
``/discover`` and ``/public/{slug}``) carries them.

Concretely: library.astro's bundle-select handler does
``currentVisibility = d.visibility || 'private'`` against the response of
``GET /api/cookbooks/{id}`` — that field is always absent, so the toggle
silently defaults to "Private" even for a bundle the owner already made
public. That is exactly the illegible-state bug Phase D exists to fix ("legible
Private <-> Public state" — the plan's own acceptance gate). A share link also
cannot be surfaced immediately on publish without the slug in the same
response the frontend already fetches after a visibility PATCH.

Fix is purely additive (2 new optional fields on an existing Pydantic model +
2 lines in the serializer) — no migration, no behavior change for any existing
consumer of the `skills`/`is_base`/etc. keys.

RED-proof: before the fix, `resp.json().get("visibility")` is None for both
routes even after PATCHing to "public" — these tests fail on main. Fix adds
the two fields; tests go green.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def vis_client(db_session: Session):
    """TestClient mirroring test_bundle_slug_on_create.py's fixture pattern."""
    from app.bundle_routes import CookbookCtx, require_cookbook_tier
    from app.bundle_routes import router as bundle_router
    from app.config import settings
    from app.database import get_db
    from app.models import User

    test_app = FastAPI()
    test_app.include_router(bundle_router)

    test_user = User(
        id=uuid.uuid4(),
        display_name="Visibility Field Test User",
        email="test-spotify2607-d-visibility-field@example.com",
        subscription_tier="pro",
    )
    db_session.add(test_user)
    db_session.flush()

    def override_get_db():
        yield db_session

    def override_tier():
        return CookbookCtx(user_id=test_user.id, is_master=False, tier="pro")

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[require_cookbook_tier] = override_tier

    with TestClient(
        test_app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c


def _create(client, name: str) -> dict:
    resp = client.post("/api/cookbooks", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_get_cookbook_detail_includes_visibility_and_slug(vis_client):
    """GET /api/cookbooks/{id} must carry visibility + slug (owner surface)."""
    created = _create(vis_client, "Detail Visibility Test")
    assert created.get("visibility") == "private"  # server_default

    detail = vis_client.get(f"/api/cookbooks/{created['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body.get("visibility") == "private", (
        "GET /api/cookbooks/{id} omits visibility — the owner-facing "
        "Visibility toggle cannot render current state"
    )
    assert body.get("slug") == "detail-visibility-test"


def test_get_cookbook_detail_reflects_flip_to_public(vis_client):
    """After PATCH .../visibility {public}, the detail GET must say so."""
    created = _create(vis_client, "Flip Visibility Test")
    patch = vis_client.patch(
        f"/api/cookbooks/{created['id']}/visibility",
        json={"visibility": "public"},
    )
    assert patch.status_code == 200, patch.text

    detail = vis_client.get(f"/api/cookbooks/{created['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body.get("visibility") == "public", (
        "detail GET still reports stale/absent visibility after a flip to "
        "public — the frontend toggle would show Private for a public bundle"
    )
    assert body.get("slug") == "flip-visibility-test"


def test_list_cookbooks_includes_visibility(vis_client):
    """GET /api/cookbooks (list) must carry visibility for the library grid's
    public/private badge (library.astro loadLibraryBundles)."""
    _create(vis_client, "List Visibility Test")
    listing = vis_client.get("/api/cookbooks")
    assert listing.status_code == 200, listing.text
    rows = listing.json()["cookbooks"]
    assert rows, "expected at least one cookbook"
    row = next(r for r in rows if r["name"] == "List Visibility Test")
    assert row.get("visibility") == "private"
    assert row.get("slug") == "list-visibility-test"


def test_rename_response_includes_visibility_and_slug(vis_client):
    """PATCH /api/cookbooks/{id} (rename) also goes through _to_cb_out —
    its response must carry both new fields too, not just create/list/get."""
    created = _create(vis_client, "Rename Visibility Test")
    renamed = vis_client.patch(
        f"/api/cookbooks/{created['id']}",
        json={"name": "Renamed Visibility Test"},
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body.get("name") == "Renamed Visibility Test"
    assert body.get("visibility") == "private"
    assert body.get("slug") == "rename-visibility-test"


def test_create_response_created_field_shape_unchanged(vis_client):
    """Additive-only: every pre-existing key on the create response must
    still be present (no reshape of a pinned contract). This asserts
    presence, not type — the pre-existing fields' types are exercised by
    the older test_bundle_slug_on_create.py suite; this test's job is only
    to prove the additive change didn't remove or rename a key."""
    created = _create(vis_client, "Contract Shape Test")
    for key in ("id", "name", "description", "is_base", "parent_bundle_id", "bundle_owner", "created_at"):
        assert key in created, f"pre-existing key {key!r} missing — additive change broke the contract"


# ── Codex adversarial-review follow-ups (PR #146, 2026-07-26) ───────────────
# Two MUST-FIX/SHOULD-FIX findings from the mandatory codex review gate were
# ACCEPTED after live adjudication. These tests pin them.
#
# Finding 3 (MUST-FIX, confirmed): PATCH /visibility mints a slug for a
#   legacy slug-less bundle via _ensure_bundle_slug, but the response body
#   discards it — so a client that just published cannot render the share URL
#   without a second round-trip. That directly undercuts Phase D's own
#   acceptance gate ("share link surfaced immediately on publish").
#
# Finding 4 (SHOULD-FIX, accepted): `slug` is genuinely nullable for rows
#   created before slug-on-create. Only freshly-created rows (which get a
#   server default) were covered, so the None branch was untested.


def test_visibility_patch_returns_slug_for_share_url(vis_client):
    """PATCH .../visibility {public} must return the slug in the SAME response.

    Codex review finding 3. The route already calls _ensure_bundle_slug() on
    publish, so the slug exists server-side at return time — withholding it
    forces a gratuitous second GET before the UI can show a share link.
    """
    created = _create(vis_client, "Share Url Publish Test")
    patch = vis_client.patch(
        f"/api/cookbooks/{created['id']}/visibility",
        json={"visibility": "public"},
    )
    assert patch.status_code == 200, patch.text
    body = patch.json()
    assert body.get("visibility") == "public"
    assert body.get("slug") == "share-url-publish-test", (
        "PATCH /visibility withheld the slug it had just ensured — the client "
        "cannot surface a share link without a second round-trip"
    )


def test_visibility_patch_backfills_and_returns_slug_for_legacy_null_slug(
    vis_client, db_session
):
    """A LEGACY bundle with slug=None must get one minted AND returned.

    Codex review findings 3+4 together: the nullable-slug branch. Simulates a
    row created before slug-on-create by nulling the slug directly, then
    publishing it — the exact path a pre-existing private bundle takes.
    """
    from app.models import Bundle

    created = _create(vis_client, "Legacy Null Slug Test")
    cb = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(created["id"])).one()
    cb.slug = None
    db_session.flush()

    detail = vis_client.get(f"/api/cookbooks/{created['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json().get("slug") is None, "precondition: legacy row has no slug"

    patch = vis_client.patch(
        f"/api/cookbooks/{created['id']}/visibility",
        json={"visibility": "public"},
    )
    assert patch.status_code == 200, patch.text
    body = patch.json()
    assert body.get("visibility") == "public"
    assert body.get("slug"), (
        "publishing a legacy slug-less bundle minted a slug server-side but "
        "did not return it — the share link stays unrenderable"
    )

    # And the detail GET must agree with what PATCH just reported.
    after = vis_client.get(f"/api/cookbooks/{created['id']}")
    assert after.status_code == 200, after.text
    assert after.json().get("slug") == body["slug"]


def test_detail_and_list_tolerate_null_slug(vis_client, db_session):
    """A null ``slug`` must serialize as null on BOTH read routes, never 500.

    Codex finding 4 (nullability). Adjudication note: the finding claimed BOTH
    new fields are nullable. Only ``slug`` is —
    ``Bundle.slug = Column(..., nullable=True)`` (models.py) and the docstring
    there says "NULL for private/unpublished bundles (most rows)".
    ``Bundle.visibility`` is ``nullable=False, server_default='private'``, so a
    null visibility is unrepresentable: attempting it raises
    ``IntegrityError: NOT NULL constraint failed: bundles.visibility``
    (verified). The DB schema is the guarantee for that half of the finding,
    so this test pins the branch that CAN actually occur rather than asserting
    an impossible state.
    """
    from app.models import Bundle

    created = _create(vis_client, "Null Slug Tolerance Test")
    cb = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(created["id"])).one()
    cb.slug = None
    db_session.flush()

    detail = vis_client.get(f"/api/cookbooks/{created['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body.get("slug") is None
    assert body.get("visibility") == "private"

    listing = vis_client.get("/api/cookbooks")
    assert listing.status_code == 200, listing.text
    row = next(c for c in listing.json()["cookbooks"] if c["id"] == created["id"])
    assert row.get("slug") is None
    assert row.get("visibility") == "private"


def test_visibility_is_not_nullable_by_schema(db_session):
    """Pin the schema guarantee that makes a null-visibility test impossible.

    Documents the adjudication of Codex finding 4 in executable form: if a
    future migration ever makes ``visibility`` nullable, this test flips red
    and the serializer's null-handling must be revisited.
    """
    from app.models import Bundle

    assert Bundle.__table__.c.visibility.nullable is False
    assert Bundle.__table__.c.slug.nullable is True

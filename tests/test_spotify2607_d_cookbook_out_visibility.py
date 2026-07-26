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

"""tests/test_bundle_slug_on_create.py — fix/bundle-slug-on-create (2026-07-17).

Regression suite for the invisible-public-bundle bug: every public surface
(GET /api/cookbooks/discover, GET /api/cookbooks/public/{slug}) filters on
``Bundle.slug.isnot(None)``, but POST /api/cookbooks never assigned a slug —
only the Pro deploy path did. A user-created bundle flipped public was
therefore unreachable on every public surface (found live 2026-07-17 while
shipping the "LoopSkill Essentials" bundle; fixed in prod via manual UPDATE).

Uses the same fixture pattern as test_cookbook_rename.py (real User + Bundle
rows, require_cookbook_tier override).

Tests:
  1. POST /api/cookbooks assigns a slug at birth (slugified from name).
  2. Slug collisions get a numeric suffix (create twice with the same name).
  3. Visibility flip to public backfills a slug on a pre-fix (slug-less) row.
  4. Visibility flip never REWRITES an existing slug (shareable URLs stable).
  5. End-to-end: created bundle flipped public appears in the discover feed
     and resolves on /public/{slug} — the actual bug's kill-test.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def slug_client(db_session: Session):
    """TestClient with bundle routes + db override + real user auth ctx."""
    from app.bundle_routes import CookbookCtx, require_cookbook_tier
    from app.bundle_routes import router as bundle_router
    from app.config import settings
    from app.database import get_db
    from app.models import User

    test_app = FastAPI()
    test_app.include_router(bundle_router)

    test_user = User(
        id=uuid.uuid4(),
        display_name="Slug Test User",
        email="test-slug-on-create@example.com",
        subscription_tier="pro",
    )
    db_session.add(test_user)
    db_session.flush()

    def override_get_db():
        yield db_session

    def override_tier():
        return CookbookCtx(
            user_id=test_user.id,
            is_master=False,
            tier="pro",
        )

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


def test_create_assigns_slug(slug_client, db_session: Session):
    """POST /api/cookbooks slugifies the name at birth."""
    from app.models import Bundle

    created = _create(slug_client, "My Great Bundle")
    row = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(created["id"])).first()
    assert row is not None
    assert row.slug == "my-great-bundle"


def test_create_slug_collision_gets_suffix(slug_client, db_session: Session):
    """Two bundles with the same name get distinct slugs (numeric suffix)."""
    from app.models import Bundle

    first = _create(slug_client, "Duplicate Name")
    second = _create(slug_client, "Duplicate Name")
    row1 = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(first["id"])).first()
    row2 = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(second["id"])).first()
    assert row1.slug == "duplicate-name"
    assert row2.slug == "duplicate-name-1"
    assert row1.slug != row2.slug


def test_public_flip_backfills_slug_for_prefix_rows(slug_client, db_session: Session):
    """A pre-fix (slug-less) bundle gets a slug the moment it goes public."""
    from app.models import Bundle

    created = _create(slug_client, "Legacy Bundle")
    cb_id = uuid.UUID(created["id"])
    # Simulate a pre-fix row: strip the slug the create path just assigned.
    row = db_session.query(Bundle).filter(Bundle.id == cb_id).first()
    row.slug = None
    db_session.flush()

    resp = slug_client.patch(
        f"/api/cookbooks/{created['id']}/visibility",
        json={"visibility": "public"},
    )
    assert resp.status_code == 200, resp.text
    db_session.refresh(row)
    assert row.slug == "legacy-bundle"
    assert row.visibility == "public"


def test_public_flip_never_rewrites_existing_slug(slug_client, db_session: Session):
    """Visibility flips must not change an existing slug (URLs are shareable)."""
    from app.models import Bundle

    created = _create(slug_client, "Stable Slug Bundle")
    cb_id = uuid.UUID(created["id"])
    row = db_session.query(Bundle).filter(Bundle.id == cb_id).first()
    original_slug = row.slug
    assert original_slug is not None

    for vis in ("public", "private", "public"):
        resp = slug_client.patch(
            f"/api/cookbooks/{created['id']}/visibility",
            json={"visibility": vis},
        )
        assert resp.status_code == 200, resp.text

    db_session.refresh(row)
    assert row.slug == original_slug


def test_public_bundle_reachable_on_public_surfaces(slug_client, db_session: Session):
    """KILL-TEST for the original bug: create → flip public → visible.

    The bundle must appear in the discover feed AND resolve on
    /public/{slug}. Before the fix both filtered it out (slug IS NULL).
    """
    created = _create(slug_client, "Essentials Kill Test")
    resp = slug_client.patch(
        f"/api/cookbooks/{created['id']}/visibility",
        json={"visibility": "public"},
    )
    assert resp.status_code == 200, resp.text

    discover = slug_client.get("/api/cookbooks/discover?sort=newest&limit=50")
    assert discover.status_code == 200, discover.text
    slugs = [c["slug"] for c in discover.json()["cookbooks"]]
    assert "essentials-kill-test" in slugs

    public_page = slug_client.get("/api/cookbooks/public/essentials-kill-test")
    assert public_page.status_code == 200, public_page.text
    assert public_page.json()["name"] == "Essentials Kill Test"

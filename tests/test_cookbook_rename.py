"""tests/test_cookbook_rename.py — PATCH /api/cookbooks/{id} rename endpoint.

Uses the same db_session fixture pattern as test_feedback_mcp.py.
The bundle router is mounted at /api/cookbooks and require_cookbook_tier
is overridden with a master ctx so all routes are callable.

Tests:
  1. Happy path: rename a cookbook (name update)
  2. Happy path: update description
  3. Empty name rejected (422)
  4. Non-existent cookbook (404)
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def rename_client(db_session: Session):
    """TestClient with bundle routes + db override + master auth ctx."""
    from app.database import get_db
    from app.bundle_routes import router as bundle_router
    from app.bundle_routes import require_cookbook_tier, CookbookCtx
    from app.config import settings

    test_app = FastAPI()
    test_app.include_router(bundle_router)

    def override_get_db():
        yield db_session

    def override_tier():
        # Master ctx — can create + modify any cookbook
        return CookbookCtx(user_id=None, is_master=True, tier="pro_plus")

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[require_cookbook_tier] = override_tier

    with TestClient(
        test_app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c


def test_rename_cookbook_happy_path(rename_client):
    """PATCH /api/cookbooks/{id} with new name returns 200 + updated name."""
    create_resp = rename_client.post(
        "/api/cookbooks",
        json={"name": "Original Name"},
    )
    assert create_resp.status_code == 201, create_resp.text
    cb_id = create_resp.json()["id"]

    patch_resp = rename_client.patch(
        f"/api/cookbooks/{cb_id}",
        json={"name": "Renamed Bundle"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    data = patch_resp.json()
    assert data["name"] == "Renamed Bundle"
    assert data["id"] == cb_id


def test_update_description(rename_client):
    """PATCH /api/cookbooks/{id} with description updates it."""
    create_resp = rename_client.post(
        "/api/cookbooks",
        json={"name": "Test Bundle"},
    )
    assert create_resp.status_code == 201, create_resp.text
    cb_id = create_resp.json()["id"]

    patch_resp = rename_client.patch(
        f"/api/cookbooks/{cb_id}",
        json={"description": "A new description"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["description"] == "A new description"


def test_empty_name_rejected(rename_client):
    """PATCH with whitespace-only name returns 422."""
    create_resp = rename_client.post(
        "/api/cookbooks",
        json={"name": "Test Bundle"},
    )
    assert create_resp.status_code == 201, create_resp.text
    cb_id = create_resp.json()["id"]

    patch_resp = rename_client.patch(
        f"/api/cookbooks/{cb_id}",
        json={"name": "   "},
    )
    assert patch_resp.status_code == 422


def test_nonexistent_cookbook_404(rename_client):
    """PATCH on a non-existent cookbook returns 404."""
    fake_id = str(uuid.uuid4())
    patch_resp = rename_client.patch(
        f"/api/cookbooks/{fake_id}",
        json={"name": "New Name"},
    )
    assert patch_resp.status_code == 404

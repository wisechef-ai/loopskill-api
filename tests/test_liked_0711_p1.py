"""P1 regression tests for the typed Liked library API and MCP verb."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.library_routes import router as library_router
from app.mcp.server import _dispatch
from app.models import (
    Bundle,
    BundleCompositeLoop,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    Personality,
    Skill,
    User,
)


def _library_app(db: Session, owner_id) -> FastAPI:
    app = FastAPI()

    def override_get_db():
        yield db

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth_ctx = AuthContext(scope="user", user_id=owner_id, tier="free")
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(library_router)
    return app


def _artifacts(db: Session) -> tuple[Skill, Personality, CompositeLoop]:
    skill = Skill(slug=f"skill-{uuid4()}", title="Liked skill")
    personality = Personality(
        slug=f"personality-{uuid4()}", title="Liked personality", system_prompt="Be helpful."
    )
    loop = CompositeLoop(
        slug=f"loop-{uuid4()}",
        title="Liked loop",
        schedule="1h",
        skills=[],
        connectors=[],
        subagents_config={},
        verifier_slug="check",
        state_seed={},
        prompt="Do the work.",
    )
    db.add_all([skill, personality, loop])
    db.commit()
    return skill, personality, loop


def test_library_heart_contract_mcp_to_http_and_free_scope(db_session):
    owner = User(id=uuid4(), email=f"{uuid4()}@example.test", display_name="Heart owner")
    ordinary = Bundle(name="Ordinary", bundle_owner=owner.id)
    db_session.add_all([owner, ordinary])
    db_session.commit()
    skill, personality, loop = _artifacts(db_session)
    ctx = AuthContext(scope="user", user_id=owner.id, tier="free")

    for artifact_type, artifact in (("skill", skill), ("personality", personality), ("loop", loop)):
        assert _dispatch(
            "recipes_like",
            db_session,
            {"action": "like", "type": artifact_type, "id": str(artifact.id)},
            {"auth_ctx": ctx},
        ) == {"liked": True, "type": artifact_type, "id": str(artifact.id)}

    with TestClient(_library_app(db_session, owner.id)) as client:
        response = client.get("/api/library")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"liked_bundle_id", "shelves", "followed_bundles"}
    assert body["followed_bundles"] == []
    assert {key: len(value) for key, value in body["shelves"].items()} == {
        "skills": 1,
        "personalities": 1,
        "loops": 1,
    }
    for shelf in body["shelves"].values():
        assert set(shelf[0]) == {"id", "slug", "title", "liked_at"}
        assert shelf[0]["liked_at"] is not None
    assert "count" not in str(body).lower()
    assert "total" not in str(body).lower()

    liked_bundle_id = UUID(body["liked_bundle_id"])
    assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == ordinary.id).count() == 0
    assert db_session.query(BundlePersonality).filter(BundlePersonality.bundle_id == ordinary.id).count() == 0
    assert db_session.query(BundleCompositeLoop).filter(BundleCompositeLoop.bundle_id == ordinary.id).count() == 0
    assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == liked_bundle_id).count() == 1
    assert db_session.query(BundlePersonality).filter(BundlePersonality.bundle_id == liked_bundle_id).count() == 1
    assert db_session.query(BundleCompositeLoop).filter(BundleCompositeLoop.bundle_id == liked_bundle_id).count() == 1


def test_like_unlike_http_are_idempotent_and_validate(db_session):
    owner = User(id=uuid4(), email=f"{uuid4()}@example.test", display_name="HTTP heart owner")
    db_session.add(owner)
    db_session.commit()
    skill, _personality, _loop = _artifacts(db_session)

    with TestClient(_library_app(db_session, owner.id)) as client:
        payload = {"type": "skill", "id": str(skill.id)}
        assert client.post("/api/library/like", json=payload).json() == {
            "liked": True,
            "type": "skill",
            "id": str(skill.id),
        }
        assert client.post("/api/library/like", json=payload).status_code == 200
        assert client.request("DELETE", "/api/library/like", json=payload).json() == {
            "liked": False,
            "type": "skill",
            "id": str(skill.id),
        }
        assert client.request("DELETE", "/api/library/like", json=payload).status_code == 200
        assert client.post("/api/library/like", json={"type": "unknown", "id": str(skill.id)}).status_code == 422
        assert client.post("/api/library/like", json={"type": "skill", "id": str(uuid4())}).status_code == 404

    liked = db_session.query(Bundle).filter(Bundle.bundle_owner == owner.id, Bundle.is_liked.is_(True)).one()
    assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == liked.id).count() == 0

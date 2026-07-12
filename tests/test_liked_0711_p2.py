"""P2 regression tests for public, read-only followed bundles."""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.follow_routes import router as follow_router
from app.library_routes import router as library_router
from app.mcp.tools.fleet_write import loopskill_bundle_deploy
from app.models import Bundle, BundleSkill, FollowedBundle, Skill, SkillVersion, User
from app.reconcile_routes import router as reconcile_router


def _follow_app(db: Session, acting_user: dict[str, object]) -> FastAPI:
    app = FastAPI()

    def override_get_db():
        yield db

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth_ctx = AuthContext(scope="user", user_id=acting_user["id"], tier="free")
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(follow_router)
    app.include_router(library_router)
    app.include_router(reconcile_router)
    return app


def _user(db: Session, name: str) -> User:
    user = User(id=uuid4(), email=f"{uuid4()}@example.test", display_name=name, subscription_tier="free")
    db.add(user)
    db.flush()
    return user


def _public_bundle(db: Session, owner: User) -> tuple[Bundle, Skill]:
    bundle = Bundle(
        id=uuid4(),
        name="Public loop",
        slug=f"public-loop-{uuid4().hex[:8]}",
        bundle_owner=owner.id,
        visibility="public",
    )
    skill = Skill(id=uuid4(), slug=f"follow-skill-{uuid4()}", title="Followable skill", is_public=True)
    db.add_all([bundle, skill])
    db.flush()
    db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
    db.add(
        SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            tarball_path="/tmp/follow-skill.tar.gz",
            tarball_size_bytes=1,
            checksum_sha256="a" * 64,
        )
    )
    db.commit()
    return bundle, skill


def test_followed_public_bundle_is_listed_deployable_and_read_only(db_session: Session):
    owner = _user(db_session, "Bundle owner")
    follower = _user(db_session, "Follower handle")
    bundle, skill = _public_bundle(db_session, owner)
    acting_user: dict[str, object] = {"id": follower.id}

    with TestClient(_follow_app(db_session, acting_user)) as client:
        followed = client.post(f"/api/bundles/{bundle.id}/follow")
        assert followed.status_code == 200
        assert followed.json() == {"following": True, "bundle_id": str(bundle.id)}
        assert client.post(f"/api/bundles/{bundle.id}/follow").json() == followed.json()

        library = client.get("/api/library")
        assert library.status_code == 200
        assert library.json()["followed_bundles"] == [
            {
                "id": str(bundle.id),
                "slug": bundle.slug,
                "name": bundle.name,
                "owner_handle": owner.display_name,
                "followed_at": library.json()["followed_bundles"][0]["followed_at"],
            }
        ]

        deployed = client.post(f"/api/bundles/{bundle.id}/reconcile", json={"local": []})
        assert deployed.status_code == 200, deployed.text
        assert deployed.json()["diff"]["add"][0]["slug"] == skill.slug
        assert (
            client.post(
                f"/api/bundles/{bundle.id}/reconcile",
                json={"local": [], "dry_run": False},
            ).status_code
            == 403
        )

    assert loopskill_bundle_deploy(
        db_session,
        bundle_id=str(bundle.id),
        skills=[{"slug": skill.slug}],
        ctx=AuthContext(scope="user", user_id=follower.id),
    ) == {"error": "forbidden", "code": 403}
    assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == bundle.id).count() == 1


def test_follow_rejects_non_public_and_own_bundles_and_unfollow_is_idempotent(db_session: Session):
    owner = _user(db_session, "Owner")
    follower = _user(db_session, "Follower")
    public, _skill = _public_bundle(db_session, owner)
    private = Bundle(id=uuid4(), name="Private", bundle_owner=owner.id, visibility="private")
    team = Bundle(id=uuid4(), name="Team", bundle_owner=owner.id, visibility="team")
    db_session.add_all([private, team])
    db_session.commit()
    acting_user: dict[str, object] = {"id": follower.id}

    with TestClient(_follow_app(db_session, acting_user)) as client:
        assert client.post(f"/api/bundles/{private.id}/follow").status_code == 403
        assert client.post(f"/api/bundles/{team.id}/follow").status_code == 403
        assert client.post(f"/api/bundles/{public.id}/follow").status_code == 200
        assert client.delete(f"/api/bundles/{public.id}/follow").json() == {
            "following": False,
            "bundle_id": str(public.id),
        }
        assert client.delete(f"/api/bundles/{public.id}/follow").status_code == 200

        acting_user["id"] = owner.id
        assert client.post(f"/api/bundles/{public.id}/follow").status_code == 400

    assert db_session.query(FollowedBundle).filter(FollowedBundle.bundle_id == public.id).count() == 0

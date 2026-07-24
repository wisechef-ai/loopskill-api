"""P0 regression tests for the per-owner Liked bundle primitive."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import auth_routes
from app.bundle_routes import CookbookCtx, require_cookbook_tier, router as bundle_router
from app.database import get_db
from app.liked_service import ensure_liked_bundle
from app.models import Bundle, User


def _bundle_app(db: Session, owner_id) -> FastAPI:
    app = FastAPI()

    def override_get_db():
        yield db

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.api_key_user_id = owner_id
        request.state.api_key_id = None
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_cookbook_tier] = lambda: CookbookCtx(
        user_id=owner_id, tier="pro_plus"
    )
    app.include_router(bundle_router)
    return app


def test_ensure_liked_bundle_is_idempotent(db_session):
    owner_id = uuid4()

    first = ensure_liked_bundle(db_session, owner_id)
    second = ensure_liked_bundle(db_session, owner_id)

    assert second.id == first.id
    assert first.name == "Liked"
    assert first.visibility == "private"
    assert first.is_base is False
    assert first.is_liked is True
    assert (
        db_session.query(Bundle)
        .filter(Bundle.bundle_owner == owner_id, Bundle.is_liked.is_(True))
        .count()
        == 1
    )


def test_concurrent_first_touch_integrity_race_returns_winner(tmp_path, monkeypatch):
    """Two sessions converge when the losing insert hits the unique index."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'liked-race.db'}")
    Bundle.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE UNIQUE INDEX uq_test_liked_owner ON bundles (bundle_owner) "
                "WHERE is_liked = 1"
            )
        )
    sessions = sessionmaker(bind=engine)
    winner_session = sessions()
    loser_session = sessions()
    owner_id = uuid4()
    winner = ensure_liked_bundle(winner_session, owner_id)

    real_query = loser_session.query
    query_calls = 0

    class MissingQuery:
        def filter(self, *args):
            return self

        def first(self):
            return None

    def stale_then_fresh_query(*entities, **kwargs):
        nonlocal query_calls
        query_calls += 1
        if query_calls == 1:
            return MissingQuery()
        return real_query(*entities, **kwargs)

    monkeypatch.setattr(loser_session, "query", stale_then_fresh_query)
    resolved = ensure_liked_bundle(loser_session, owner_id)

    assert resolved.id == winner.id
    with sessions() as verification_session:
        assert (
            verification_session.query(Bundle)
            .filter(Bundle.bundle_owner == owner_id, Bundle.is_liked.is_(True))
            .count()
            == 1
        )
    loser_session.close()
    winner_session.close()
    engine.dispose()


def test_integrity_error_without_visible_winner_is_not_swallowed(db_session, monkeypatch):
    monkeypatch.setattr(
        db_session,
        "flush",
        lambda: (_ for _ in ()).throw(IntegrityError("insert", {}, Exception("unique"))),
    )

    with pytest.raises(IntegrityError):
        ensure_liked_bundle(db_session, uuid4())


def test_bundle_list_lazily_provisions_and_liked_cannot_be_deleted(db_session):
    owner = User(id=uuid4(), email=f"{uuid4()}@example.test", display_name="Liked owner")
    ordinary = Bundle(name="Ordinary", bundle_owner=owner.id)
    db_session.add_all([owner, ordinary])
    db_session.commit()

    with TestClient(_bundle_app(db_session, owner.id)) as client:
        listed = client.get("/api/bundles")
        liked_id = next(row["id"] for row in listed.json()["cookbooks"] if row["name"] == "Liked")
        protected = client.delete(f"/api/bundles/{liked_id}")
        deleted = client.delete(f"/api/bundles/{ordinary.id}")

    assert listed.status_code == 200
    assert protected.status_code == 403
    assert "Liked" in protected.json()["detail"]
    assert deleted.status_code == 204


@pytest.mark.parametrize(
    ("provider", "exchange_name", "find_name"),
    [
        ("github", "exchange_github_code", "find_or_create_user_by_github"),
        ("google", "exchange_google_code", "find_or_create_user_by_google"),
    ],
)
def test_oauth_callbacks_provision_liked_bundle(monkeypatch, provider, exchange_name, find_name):
    owner_id = uuid4()
    user = SimpleNamespace(id=owner_id, display_name="OAuth user")

    async def exchange(_code):
        return {"provider": provider}

    provisioned = []
    monkeypatch.setattr(auth_routes, exchange_name, exchange)
    monkeypatch.setattr(auth_routes, find_name, lambda db, data: user)
    monkeypatch.setattr(auth_routes, "ensure_liked_bundle", lambda db, user_id: provisioned.append(user_id))
    monkeypatch.setattr(auth_routes, "ensure_referral_code", lambda user, db: None)
    monkeypatch.setattr(auth_routes, "create_jwt", lambda user: "jwt")

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "valid")
        response = client.get(
            f"/api/auth/{provider}/callback?code=code&state=valid",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert provisioned == [owner_id]


def test_migration_backfills_idempotently_and_downgrades_without_data_loss():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    bundles = sa.Table(
        "bundles",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_base", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bundle_owner", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="private"),
    )
    metadata.create_all(engine)
    owner_a = uuid4()
    owner_b = uuid4()
    original_ids = {uuid4(), uuid4(), uuid4()}
    with engine.begin() as connection:
        connection.execute(
            bundles.insert(),
            [
                {"id": row_id, "name": f"existing-{index}", "bundle_owner": owner}
                for index, (row_id, owner) in enumerate(
                    zip(original_ids, [owner_a, owner_a, owner_b], strict=True)
                )
            ],
        )
        context = MigrationContext.configure(connection)
        migration_path = (
            Path(__file__).parents[1]
            / "alembic"
            / "versions"
            / "liked0711_p0_add_liked_bundle.py"
        )
        spec = spec_from_file_location("liked0711_p0_migration", migration_path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()

        rows = connection.execute(
            sa.text("SELECT id, bundle_owner, is_liked FROM bundles")
        ).mappings().all()
        assert {str(row_id).replace("-", "") for row_id in original_ids}.issubset(
            {str(row["id"]).replace("-", "") for row in rows}
        )
        assert sum(bool(row["is_liked"]) for row in rows) == 2
        assert len(rows) == 5

        with Operations.context(context):
            migration.downgrade()
        assert "is_liked" not in {column["name"] for column in sa.inspect(connection).get_columns("bundles")}
        assert connection.execute(sa.text("SELECT COUNT(*) FROM bundles")).scalar_one() == 5

    engine.dispose()

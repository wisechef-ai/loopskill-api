"""mesh_0408 T0-D — POST /api/mesh/credentials over HTTP.

Exercises the full route: auth via a FleetMember-dedicated API key,
class validation, 409 mesh_tenant_unassigned mapping, and 401 when the
caller has no member row.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import create_app
from app.mesh.keys import generate_keypair
from app.models import APIKey, Fleet, FleetMember, Org, User


def _client(db_session, monkeypatch=None):
    app = create_app()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    if monkeypatch is not None:
        # APIKeyMiddleware opens its OWN session via app.database.SessionLocal
        # (it runs before the get_db dependency is resolved). Redirect it to
        # the same per-test SAVEPOINT session so newly-created fixtures
        # (member/fleet/key rows) are visible to the middleware's lookup.
        class _FakeSession:
            def __init__(self, real):
                self._real = real

            def query(self, *a, **kw):
                return self._real.query(*a, **kw)

            def close(self):
                pass

        monkeypatch.setattr("app.database.SessionLocal", lambda: _FakeSession(db_session), raising=False)

    return TestClient(app, raise_server_exceptions=True)


def _mk_user(db) -> User:
    u = User(id=uuid4(), display_name="route-user", email=f"{uuid4().hex[:8]}@example.com")
    db.add(u)
    db.flush()
    return u


def _mk_org(db) -> Org:
    org = Org(id=uuid4(), name="route-org", slug=f"route-org-{uuid4().hex[:6]}", api_key_hash="")
    db.add(org)
    db.flush()
    return org


def _mk_fleet(db, owner, org) -> Fleet:
    fleet = Fleet(id=uuid4(), name="route-fleet", owner_user_id=owner.id, fleet_api_key_hash=uuid4().hex, org_id=org.id if org else None)
    db.add(fleet)
    db.flush()
    return fleet


def _mk_member_with_key(db, fleet) -> tuple[FleetMember, str]:
    """Returns (member, plaintext_key). key follows the lsk_ prefix used
    elsewhere in the middleware (LOOPSKILL_KEY_PREFIX)."""
    import hashlib

    plaintext = f"lsk_route_{uuid4().hex}"
    key_row = APIKey(
        id=uuid4(),
        user_id=fleet.owner_user_id,
        key_prefix=plaintext[:12],
        key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        name="member-key",
        is_active=True,
    )
    db.add(key_row)
    db.flush()
    member = FleetMember(
        id=uuid4(),
        fleet_id=fleet.id,
        host="route-host",
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key_row.id,
        is_active=True,
    )
    db.add(member)
    db.flush()
    return member, plaintext


class TestMintRouteHappyPath:
    def test_mints_mesh_directory_credential(self, db_session, tmp_path, monkeypatch):
        priv_pem, pub_pem = generate_keypair()
        priv_path = tmp_path / "signing.pem"
        priv_path.write_bytes(priv_pem)
        import os

        os.chmod(priv_path, 0o600)
        jwks_dir = tmp_path / "jwks"
        jwks_dir.mkdir()
        (jwks_dir / "route-kid.pub.pem").write_bytes(pub_pem)

        monkeypatch.setattr(settings, "MESH_SIGNING_KEY_PATH", str(priv_path))
        monkeypatch.setattr(settings, "MESH_SIGNING_KID", "route-kid")
        monkeypatch.setattr(settings, "MESH_JWKS_DIR", str(jwks_dir))

        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member, plaintext_key = _mk_member_with_key(db_session, fleet)
        db_session.commit()

        client = _client(db_session, monkeypatch)
        resp = client.post(
            "/api/mesh/credentials",
            json={"credential_class": "mesh-directory"},
            headers={"x-api-key": plaintext_key},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["class"] == "mesh-directory"
        assert body["aud"] == "loopskill-api"
        assert "token" in body


class TestMintRouteFailClosedOnNullOrg:
    def test_returns_409_mesh_tenant_unassigned(self, db_session, tmp_path, monkeypatch):
        priv_pem, pub_pem = generate_keypair()
        priv_path = tmp_path / "signing.pem"
        priv_path.write_bytes(priv_pem)
        import os

        os.chmod(priv_path, 0o600)
        jwks_dir = tmp_path / "jwks"
        jwks_dir.mkdir()
        (jwks_dir / "route-kid2.pub.pem").write_bytes(pub_pem)

        monkeypatch.setattr(settings, "MESH_SIGNING_KEY_PATH", str(priv_path))
        monkeypatch.setattr(settings, "MESH_SIGNING_KID", "route-kid2")
        monkeypatch.setattr(settings, "MESH_JWKS_DIR", str(jwks_dir))

        owner = _mk_user(db_session)
        fleet = _mk_fleet(db_session, owner, org=None)  # NULL org_id
        member, plaintext_key = _mk_member_with_key(db_session, fleet)
        db_session.commit()

        client = _client(db_session, monkeypatch)
        resp = client.post(
            "/api/mesh/credentials",
            json={"credential_class": "mesh-directory"},
            headers={"x-api-key": plaintext_key},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "mesh_tenant_unassigned"


class TestMintRouteAuthRequired:
    def test_master_key_without_member_row_rejected(self, db_session):
        client = _client(db_session)
        resp = client.post(
            "/api/mesh/credentials",
            json={"credential_class": "mesh-directory"},
            headers={"x-api-key": settings.API_KEY},
        )
        assert resp.status_code == 401

    def test_no_key_rejected(self, db_session):
        client = _client(db_session)
        resp = client.post("/api/mesh/credentials", json={"credential_class": "mesh-directory"})
        assert resp.status_code == 401

    def test_invalid_class_rejected(self, db_session, tmp_path, monkeypatch):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member, plaintext_key = _mk_member_with_key(db_session, fleet)
        db_session.commit()

        client = _client(db_session, monkeypatch)
        resp = client.post(
            "/api/mesh/credentials",
            json={"credential_class": "mesh-superadmin"},
            headers={"x-api-key": plaintext_key},
        )
        assert resp.status_code == 422

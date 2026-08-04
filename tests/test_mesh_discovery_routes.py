"""mesh_0408 T3-A — HTTP tests for the A2A discovery surfaces.

Covers:
  * GET /api/orgs/{org_id}/a2a-directory — happy path, missing/invalid
    bearer credential (401), wrong class/audience (401), CROSS-TENANT
    rejection org A -> org B (403) — the gate acceptance requires.
  * GET /api/fleets/{id}/members?a2a_only=true — narrows to members with a
    registered a2a endpoint, reusing FleetMemberLiveness.provides (no
    parallel directory table).
  * Endpoint substitution: mutating provides.a2a on one member does not let
    a credential minted for a DIFFERENT member harvest anything — the
    directory only ever returns rows within the caller's OWN verified org,
    and per-member scoping never changes based on what `provides` contains.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import create_app
from app.mesh.constants import CLASS_MESH_DIRECTORY
from app.mesh.keys import generate_keypair
from app.mesh.mint import mint_credential
from app.models import APIKey, Fleet, FleetMember, FleetMemberLiveness, Org, User


def _client(db_session, monkeypatch):
    app = create_app()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    class _FakeSession:
        def __init__(self, real):
            self._real = real

        def query(self, *a, **kw):
            return self._real.query(*a, **kw)

        def close(self):
            pass

    monkeypatch.setattr("app.database.SessionLocal", lambda: _FakeSession(db_session), raising=False)
    return TestClient(app, raise_server_exceptions=True)


def _setup_keys(tmp_path, monkeypatch, kid="disc-kid"):
    from app.mesh.keys import SigningKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv_pem, pub_pem = generate_keypair()
    priv_path = tmp_path / "signing.pem"
    priv_path.write_bytes(priv_pem)
    import os

    os.chmod(priv_path, 0o600)
    jwks_dir = tmp_path / "jwks"
    jwks_dir.mkdir()
    (jwks_dir / f"{kid}.pub.pem").write_bytes(pub_pem)

    monkeypatch.setattr(settings, "MESH_SIGNING_KEY_PATH", str(priv_path))
    monkeypatch.setattr(settings, "MESH_SIGNING_KID", kid)
    monkeypatch.setattr(settings, "MESH_JWKS_DIR", str(jwks_dir))

    private_key = load_pem_private_key(priv_pem, password=None)
    return SigningKey(kid=kid, private_key=private_key)


def _mk_user(db) -> User:
    u = User(id=uuid4(), display_name="disc-user", email=f"{uuid4().hex[:8]}@example.com")
    db.add(u)
    db.flush()
    return u


def _mk_org(db) -> Org:
    org = Org(id=uuid4(), name="disc-org", slug=f"disc-org-{uuid4().hex[:6]}", api_key_hash="")
    db.add(org)
    db.flush()
    return org


def _mk_fleet(db, owner, org) -> Fleet:
    fleet = Fleet(id=uuid4(), name="disc-fleet", owner_user_id=owner.id, fleet_api_key_hash=uuid4().hex, org_id=org.id)
    db.add(fleet)
    db.flush()
    return fleet


def _mk_member_with_key(db, fleet, host="disc-host") -> tuple[FleetMember, str]:
    plaintext = f"lsk_disc_{uuid4().hex}"
    key_row = APIKey(
        id=uuid4(), user_id=fleet.owner_user_id, key_prefix=plaintext[:12],
        key_hash=hashlib.sha256(plaintext.encode()).hexdigest(), name="member-key", is_active=True,
    )
    db.add(key_row)
    db.flush()
    member = FleetMember(
        id=uuid4(), fleet_id=fleet.id, host=host, profile="default", skills_dir="~/x",
        api_key_id=key_row.id, is_active=True,
    )
    db.add(member)
    db.flush()
    return member, plaintext


def _mk_liveness(db, member, provides):
    row = FleetMemberLiveness(member_id=member.id, provides=provides)
    db.add(row)
    db.flush()
    return row


class TestA2ADirectoryHappyPath:
    def test_directory_lists_only_members_with_a2a_endpoint(self, db_session, tmp_path, monkeypatch):
        key = _setup_keys(tmp_path, monkeypatch)
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        caller_member, _ = _mk_member_with_key(db_session, fleet, host="caller")
        with_endpoint, _ = _mk_member_with_key(db_session, fleet, host="with-endpoint")
        without_endpoint, _ = _mk_member_with_key(db_session, fleet, host="without-endpoint")
        _mk_liveness(db_session, with_endpoint, {"a2a": "https://with-endpoint.example/a2a"})
        _mk_liveness(db_session, without_endpoint, {"os": "linux"})  # no "a2a" key
        db_session.commit()

        minted = mint_credential(db_session, member_id=caller_member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        db_session.commit()

        client = _client(db_session, monkeypatch)
        resp = client.get(
            f"/api/orgs/{org.id}/a2a-directory",
            headers={"Authorization": f"Bearer {minted.token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        member_ids = {m["member_id"] for m in body["members"]}
        assert str(with_endpoint.id) in member_ids
        assert str(without_endpoint.id) not in member_ids
        entry = next(m for m in body["members"] if m["member_id"] == str(with_endpoint.id))
        assert entry["a2a_endpoint"] == "https://with-endpoint.example/a2a"


class TestA2ADirectoryAuthRequired:
    def test_missing_bearer_rejected(self, db_session, tmp_path, monkeypatch):
        _setup_keys(tmp_path, monkeypatch)
        _mk_user(db_session)
        org = _mk_org(db_session)
        db_session.commit()
        client = _client(db_session, monkeypatch)
        resp = client.get(f"/api/orgs/{org.id}/a2a-directory")
        assert resp.status_code == 401

    def test_malformed_bearer_rejected(self, db_session, tmp_path, monkeypatch):
        _setup_keys(tmp_path, monkeypatch)
        _mk_user(db_session)
        org = _mk_org(db_session)
        db_session.commit()
        client = _client(db_session, monkeypatch)
        resp = client.get(f"/api/orgs/{org.id}/a2a-directory", headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401

    def test_wrong_class_rejected(self, db_session, tmp_path, monkeypatch):
        """A mesh-exec credential (aud = peer member id) must not satisfy
        the loopskill-api directory audience — the blast-radius separation."""
        from app.mesh.constants import CLASS_MESH_EXEC

        key = _setup_keys(tmp_path, monkeypatch)
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member, _ = _mk_member_with_key(db_session, fleet)
        target, _ = _mk_member_with_key(db_session, fleet, host="target")
        db_session.commit()

        minted = mint_credential(
            db_session, member_id=member.id, cls=CLASS_MESH_EXEC, target_member_id=str(target.id), signing_key=key
        )
        db_session.commit()

        client = _client(db_session, monkeypatch)
        resp = client.get(
            f"/api/orgs/{org.id}/a2a-directory",
            headers={"Authorization": f"Bearer {minted.token}"},
        )
        assert resp.status_code == 401


class TestCrossTenantRejection:
    """THE acceptance gate: org A's credential must not read org B's directory."""

    def test_org_a_credential_rejected_at_org_b_directory(self, db_session, tmp_path, monkeypatch):
        key = _setup_keys(tmp_path, monkeypatch)
        owner_a = _mk_user(db_session)
        owner_b = _mk_user(db_session)
        org_a = _mk_org(db_session)
        org_b = _mk_org(db_session)
        fleet_a = _mk_fleet(db_session, owner_a, org_a)
        fleet_b = _mk_fleet(db_session, owner_b, org_b)
        member_a, _ = _mk_member_with_key(db_session, fleet_a, host="member-a")
        member_b, _ = _mk_member_with_key(db_session, fleet_b, host="member-b")
        _mk_liveness(db_session, member_b, {"a2a": "https://org-b-secret.example/a2a"})
        db_session.commit()

        minted_a = mint_credential(db_session, member_id=member_a.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        db_session.commit()

        client = _client(db_session, monkeypatch)
        # org A's own directory works.
        ok = client.get(f"/api/orgs/{org_a.id}/a2a-directory", headers={"Authorization": f"Bearer {minted_a.token}"})
        assert ok.status_code == 200

        # org A's credential against org B's directory — REJECTED.
        resp = client.get(f"/api/orgs/{org_b.id}/a2a-directory", headers={"Authorization": f"Bearer {minted_a.token}"})
        assert resp.status_code == 403, resp.text
        # The forbidden response must not leak org B's directory contents.
        assert "org-b-secret" not in resp.text


class TestEndpointSubstitutionResistance:
    """Mutating provides.a2a on a member OTHER than the caller's own must
    not let the caller harvest anything beyond its own org's directory —
    endpoint data is scoped by the (re-derived) org, never by request input."""

    def test_endpoint_mutation_does_not_widen_directory_access(self, db_session, tmp_path, monkeypatch):
        key = _setup_keys(tmp_path, monkeypatch)
        owner_a = _mk_user(db_session)
        owner_b = _mk_user(db_session)
        org_a = _mk_org(db_session)
        org_b = _mk_org(db_session)
        fleet_a = _mk_fleet(db_session, owner_a, org_a)
        fleet_b = _mk_fleet(db_session, owner_b, org_b)
        member_a, _ = _mk_member_with_key(db_session, fleet_a, host="member-a")
        member_b, _ = _mk_member_with_key(db_session, fleet_b, host="member-b")
        db_session.commit()

        minted_a = mint_credential(db_session, member_id=member_a.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        db_session.commit()

        # Attacker (or a misconfigured member_b) sets its OWN provides.a2a to
        # point at an attacker-controlled URL — this alone must not surface
        # member_b in org_a's directory, because org_a's credential's
        # verified_tenant is org_a, and member_b lives in org_b.
        _mk_liveness(db_session, member_b, {"a2a": "https://attacker.example/harvest"})
        db_session.commit()

        client = _client(db_session, monkeypatch)
        resp = client.get(f"/api/orgs/{org_a.id}/a2a-directory", headers={"Authorization": f"Bearer {minted_a.token}"})
        assert resp.status_code == 200
        member_ids = {m["member_id"] for m in resp.json()["members"]}
        assert str(member_b.id) not in member_ids


class TestFleetMembersA2AOnlyFilter:
    def test_a2a_only_filter_narrows_to_registered_endpoints(self, db_session, tmp_path, monkeypatch):
        _setup_keys(tmp_path, monkeypatch)
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        owner_key_plain = f"rec_live_{uuid4().hex}"
        db_session.add(
            APIKey(
                id=uuid4(), user_id=owner.id, key_prefix=owner_key_plain[:12],
                key_hash=hashlib.sha256(owner_key_plain.encode()).hexdigest(), name="owner", is_active=True,
            )
        )
        with_a2a, _ = _mk_member_with_key(db_session, fleet, host="with-a2a")
        without_a2a, _ = _mk_member_with_key(db_session, fleet, host="without-a2a")
        _mk_liveness(db_session, with_a2a, {"a2a": "https://with-a2a.example/a2a"})
        db_session.commit()

        client = _client(db_session, monkeypatch)
        resp = client.get(
            f"/api/fleets/{fleet.id}/members",
            params={"a2a_only": "true"},
            headers={"x-api-key": owner_key_plain},
        )
        assert resp.status_code == 200, resp.text
        member_ids = {m["member_id"] for m in resp.json()["members"]}
        assert str(with_a2a.id) in member_ids
        assert str(without_a2a.id) not in member_ids

        # Default (no filter) still returns both.
        resp_all = client.get(f"/api/fleets/{fleet.id}/members", headers={"x-api-key": owner_key_plain})
        all_ids = {m["member_id"] for m in resp_all.json()["members"]}
        assert str(with_a2a.id) in all_ids and str(without_a2a.id) in all_ids

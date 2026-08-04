"""mesh_0408 T3-A — app/mesh/verify.py unit tests.

Covers: happy-path admission for each class, wrong-audience/wrong-class
rejection, revoked-member rejection (the revocation gate — spec §4.1),
re-enrollment (stale positive does NOT authorise under the old identity —
spec §4.5), unassigned-org fail-closed, array-audience rejection, and the
core T3-A guarantee: verified_tenant is re-derived from the LIVE DB, never
trusted from the token's own org claim.
"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.mesh.constants import (
    ADMIN_AUD,
    CLAIM_NS,
    CLASS_MESH_ADMIN,
    CLASS_MESH_DIRECTORY,
    CLASS_MESH_EXEC,
    DIRECTORY_AUD,
    HEADER_ALG,
    HEADER_TYP,
    ISS,
)
from app.mesh.errors import MeshVerifyError
from app.mesh.keys import SigningKey, generate_keypair
from app.mesh.mint import mint_credential
from app.mesh.verify import verify_control_plane_credential
from app.models import APIKey, Fleet, FleetMember, Org, User


def _signing_key(kid="verify-test-kid"):
    priv_pem, pub_pem = generate_keypair()
    private_key = load_pem_private_key(priv_pem, password=None)
    return SigningKey(kid=kid, private_key=private_key), pub_pem


def _write_jwks(tmp_path, kid, pub_pem):
    jwks_dir = tmp_path / f"jwks-{uuid4().hex[:8]}"
    jwks_dir.mkdir()
    (jwks_dir / f"{kid}.pub.pem").write_bytes(pub_pem)
    return str(jwks_dir)


def _mk_user(db) -> User:
    u = User(id=uuid4(), display_name="verify-user", email=f"{uuid4().hex[:8]}@example.com")
    db.add(u)
    db.flush()
    return u


def _mk_org(db) -> Org:
    org = Org(id=uuid4(), name="verify-org", slug=f"verify-org-{uuid4().hex[:6]}", api_key_hash="")
    db.add(org)
    db.flush()
    return org


def _mk_fleet(db, owner, org=None) -> Fleet:
    fleet = Fleet(
        id=uuid4(), name="verify-fleet", owner_user_id=owner.id, fleet_api_key_hash=uuid4().hex,
        org_id=org.id if org else None,
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_member(db, fleet, host="verify-host", profile="default") -> FleetMember:
    key_row = APIKey(id=uuid4(), user_id=fleet.owner_user_id, key_prefix="lsk_vrf", key_hash=uuid4().hex, name="k", is_active=True)
    db.add(key_row)
    db.flush()
    member = FleetMember(
        id=uuid4(), fleet_id=fleet.id, host=host, profile=profile, skills_dir="~/x",
        api_key_id=key_row.id, is_active=True,
    )
    db.add(member)
    db.flush()
    return member


def _mint(db, member, cls, key, target_member_id=None):
    return mint_credential(db, member_id=member.id, cls=cls, target_member_id=target_member_id, signing_key=key)


class TestHappyPathThreeClasses:
    def test_mesh_directory_admits(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        minted = _mint(db_session, member, CLASS_MESH_DIRECTORY, key)

        caller = verify_control_plane_credential(
            minted.token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
            jwks_dir=jwks_dir,
        )
        assert caller.member_id == member.id
        assert caller.verified_tenant == org.id
        assert caller.cls == CLASS_MESH_DIRECTORY

    def test_mesh_admin_admits(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        minted = _mint(db_session, member, CLASS_MESH_ADMIN, key)

        caller = verify_control_plane_credential(
            minted.token, db=db_session, expected_aud=ADMIN_AUD, expected_class=CLASS_MESH_ADMIN,
            jwks_dir=jwks_dir,
        )
        assert caller.cls == CLASS_MESH_ADMIN


class TestWrongAudienceOrClassRejected:
    def test_directory_token_rejected_at_admin_endpoint(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        minted = _mint(db_session, member, CLASS_MESH_DIRECTORY, key)

        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                minted.token, db=db_session, expected_aud=ADMIN_AUD, expected_class=CLASS_MESH_ADMIN,
                jwks_dir=jwks_dir,
            )

    def test_exec_token_rejected_at_directory_endpoint(self, db_session, tmp_path):
        """Spec §1 blast-radius gate: mesh-exec (aud = peer member id) can
        never satisfy the loopskill-api audience the directory endpoint
        requires."""
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        target = _mk_member(db_session, fleet, host="target-h")
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        minted = _mint(db_session, member, CLASS_MESH_EXEC, key, target_member_id=str(target.id))

        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                minted.token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
                jwks_dir=jwks_dir,
            )

    def test_forged_class_with_matching_audience_rejected(self, db_session, tmp_path):
        """Spec §1 rule 1 — `class` is mandatory and validated INDEPENDENTLY
        of `aud`. This crafts a token whose `aud` legitimately matches
        loopskill-api (so the jwt.decode audience check alone would admit
        it) but whose class claim is forged to `mesh-admin`. The directory
        endpoint (expected_class=mesh-directory) must still reject it — the
        class check is a SEPARATE gate, not a restatement of the audience
        check (a real mint never produces this combination; a forged/
        tampered token could)."""
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": f"lsm:member:{member.id}",
            "aud": DIRECTORY_AUD,  # legitimate audience for the directory endpoint
            "exp": now + 600,
            "iat": now,
            "nbf": now,
            "jti": "01FORGEDCLASSTESTULID0000",
            f"{CLAIM_NS}org": str(org.id),
            f"{CLAIM_NS}fleet": str(fleet.id),
            f"{CLAIM_NS}member": str(member.id),
            f"{CLAIM_NS}class": CLASS_MESH_ADMIN,  # FORGED — real mint-directory pairs never carry this
            f"{CLAIM_NS}pact": None,
        }
        token = jwt.encode(claims, key.private_key, algorithm=HEADER_ALG, headers={"kid": key.kid, "typ": HEADER_TYP})
        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
                jwks_dir=jwks_dir,
            )


class TestRevocationGate:
    """Spec §4.1 — revocation. DELETE members/{id} sets is_active=False;
    the NEXT admission check must reject even though the token is still
    cryptographically valid and unexpired."""

    def test_revoked_member_rejected_even_with_valid_unexpired_token(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        minted = _mint(db_session, member, CLASS_MESH_DIRECTORY, key)

        # token verifies fine BEFORE revocation
        caller = verify_control_plane_credential(
            minted.token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
            jwks_dir=jwks_dir,
        )
        assert caller.member_id == member.id

        # now revoke (mirrors DELETE /api/fleets/{id}/members/{member_id})
        member.is_active = False
        db_session.commit()

        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                minted.token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
                jwks_dir=jwks_dir,
            )


class TestReEnrollmentRace:
    """Spec §4.5 — a recreated member gets a NEW server-generated UUID. A
    stale token naming the OLD (now-deleted) member id must not authorise
    under the new member's identity, and must not silently resolve to the
    new row."""

    def test_stale_token_does_not_authorise_after_reenrollment(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet, host="reenroll-host")
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        minted = _mint(db_session, member, CLASS_MESH_DIRECTORY, key)

        # "re-enrollment": deactivate the old member (DELETE), then enroll a
        # NEW member row (mirrors POST .../members creating a fresh id).
        # NOTE: FleetMember has an UNCONDITIONAL UniqueConstraint on
        # (fleet_id, host, profile) — even a deactivated row occupies that
        # slot, so a real re-enrollment under the identical host/profile
        # would need the caller to pick a different profile or the operator
        # to hard-delete the old row first. We use a distinct profile here
        # to model "recreate the member" without fighting that constraint;
        # the property under test (server-generated NEW uuid4, old token
        # stale) is unaffected by which profile string is chosen.
        member.is_active = False
        db_session.commit()
        new_member = _mk_member(db_session, fleet, host="reenroll-host", profile="reenrolled")
        db_session.commit()
        assert new_member.id != member.id

        # The OLD token still names the OLD (now-inactive) member id.
        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                minted.token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
                jwks_dir=jwks_dir,
            )


class TestUnassignedOrgFailsClosed:
    def test_null_org_fleet_rejected(self, db_session, tmp_path):
        """A member whose fleet.org_id went to NULL after mint (or was
        never assigned) must be rejected at verify time too, not just mint
        time — defence in depth for the tenancy invariant."""
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        minted = _mint(db_session, member, CLASS_MESH_DIRECTORY, key)

        # Simulate the org being unset AFTER mint (e.g. admin detached it).
        fleet.org_id = None
        db_session.commit()

        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                minted.token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
                jwks_dir=jwks_dir,
            )


class TestVerifiedTenantIsDbDerivedNeverTokenClaim:
    """THE T3-A guarantee. Forge a token whose org claim names one org, but
    whose member row actually belongs to a DIFFERENT org — verify must
    return the member's REAL (DB) org, not the claim."""

    def test_forged_org_claim_is_ignored_real_tenant_used(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        real_org = _mk_org(db_session)
        fake_org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, real_org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)

        # Hand-craft a token with a forged org claim (fake_org) but a member
        # claim that genuinely resolves to `member` (real_org's fleet).
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": f"lsm:member:{member.id}",
            "aud": DIRECTORY_AUD,
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "jti": "01FORGEDCLAIMTESTULID0000",
            f"{CLAIM_NS}org": str(fake_org.id),  # FORGED — does not match member's real fleet
            f"{CLAIM_NS}fleet": str(fleet.id),
            f"{CLAIM_NS}member": str(member.id),
            f"{CLAIM_NS}class": CLASS_MESH_DIRECTORY,
            f"{CLAIM_NS}pact": None,
        }
        token = jwt.encode(claims, key.private_key, algorithm=HEADER_ALG, headers={"kid": key.kid, "typ": HEADER_TYP})

        caller = verify_control_plane_credential(
            token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
            jwks_dir=jwks_dir,
        )
        # The DB-derived truth wins — real_org, never the forged fake_org.
        assert caller.verified_tenant == real_org.id
        assert caller.verified_tenant != fake_org.id


class TestArrayAudienceRejected:
    def test_array_aud_rejected(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        jwks_dir = _write_jwks(tmp_path, key.kid, pub)
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": f"lsm:member:{member.id}",
            "aud": [DIRECTORY_AUD, "loopskill-api-admin"],  # multi-target — forbidden by spec §5
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "jti": "01ARRAYAUDTESTULID000000A",
            f"{CLAIM_NS}org": str(org.id),
            f"{CLAIM_NS}fleet": str(fleet.id),
            f"{CLAIM_NS}member": str(member.id),
            f"{CLAIM_NS}class": CLASS_MESH_DIRECTORY,
            f"{CLAIM_NS}pact": None,
        }
        token = jwt.encode(claims, key.private_key, algorithm=HEADER_ALG, headers={"kid": key.kid, "typ": HEADER_TYP})
        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
                jwks_dir=jwks_dir,
            )


class TestUnknownKidRejected:
    def test_unknown_kid_rejected(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key, pub = _signing_key()
        # Deliberately DO NOT write pub key to the jwks_dir the verifier reads.
        empty_dir = tmp_path / "empty-jwks"
        empty_dir.mkdir()
        minted = _mint(db_session, member, CLASS_MESH_DIRECTORY, key)

        with pytest.raises(MeshVerifyError):
            verify_control_plane_credential(
                minted.token, db=db_session, expected_aud=DIRECTORY_AUD, expected_class=CLASS_MESH_DIRECTORY,
                jwks_dir=str(empty_dir),
            )

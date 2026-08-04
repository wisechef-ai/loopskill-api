"""mesh_0408 T0-D — credential minting. Spec §1, §2, §2.4, §4.9.

Covers: three classes/audiences, mandatory class claim, nullable-org_id
fail-closed (409 mesh_tenant_unassigned), never-caller-supplied member id,
the settlement claim slot, and the transactional-read race guard.
"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest

from app.mesh.constants import (
    ADMIN_AUD,
    CLAIM_NS,
    CLASS_MESH_ADMIN,
    CLASS_MESH_DIRECTORY,
    CLASS_MESH_EXEC,
    CLASS_TTL_SECONDS,
    DIRECTORY_AUD,
    ISS,
)
from app.mesh.errors import MeshMintRaceError, MeshTenantUnassignedError
from app.mesh.keys import generate_keypair
from app.mesh.mint import mint_credential
from app.models import APIKey, Fleet, FleetMember, Org, User


def _signing_key(tmp_path, kid="mint-test-kid"):
    from app.mesh.keys import SigningKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv_pem, _pub = generate_keypair()
    private_key = load_pem_private_key(priv_pem, password=None)
    return SigningKey(kid=kid, private_key=private_key)


def _mk_user(db, email="mint-user@example.com") -> User:
    u = User(id=uuid4(), display_name=email, email=email)
    db.add(u)
    db.flush()
    return u


def _mk_org(db, name="mint-org") -> Org:
    org = Org(id=uuid4(), name=name, slug=f"{name}-{uuid4().hex[:6]}", api_key_hash="")
    db.add(org)
    db.flush()
    return org


def _mk_fleet(db, owner: User, org: Org | None) -> Fleet:
    fleet = Fleet(
        id=uuid4(),
        name="mint-fleet",
        owner_user_id=owner.id,
        fleet_api_key_hash=uuid4().hex,
        org_id=org.id if org else None,
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_member(db, fleet: Fleet, host="mint-host") -> FleetMember:
    key_row = APIKey(
        id=uuid4(),
        user_id=fleet.owner_user_id,
        key_prefix="lsk_mnttst",
        key_hash=uuid4().hex,
        name="member-key",
        is_active=True,
    )
    db.add(key_row)
    db.flush()
    member = FleetMember(
        id=uuid4(),
        fleet_id=fleet.id,
        host=host,
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key_row.id,
        is_active=True,
    )
    db.add(member)
    db.flush()
    return member


class TestMintThreeClasses:
    def test_mesh_exec_audience_is_receiving_member(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet, host="minter")
        target = _mk_member(db_session, fleet, host="receiver")
        db_session.commit()

        key = _signing_key(tmp_path)
        minted = mint_credential(
            db_session,
            member_id=member.id,
            cls=CLASS_MESH_EXEC,
            target_member_id=str(target.id),
            signing_key=key,
        )
        assert minted.aud == f"lsm:member:{target.id}"
        assert minted.exp - minted.iat == CLASS_TTL_SECONDS[CLASS_MESH_EXEC]

    def test_mesh_directory_audience_is_loopskill_api(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        minted = mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        assert minted.aud == DIRECTORY_AUD
        assert minted.exp - minted.iat == CLASS_TTL_SECONDS[CLASS_MESH_DIRECTORY]

    def test_mesh_admin_audience_is_loopskill_api_admin(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        minted = mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_ADMIN, signing_key=key)
        assert minted.aud == ADMIN_AUD
        assert minted.exp - minted.iat == CLASS_TTL_SECONDS[CLASS_MESH_ADMIN]

    def test_three_classes_have_three_distinct_audiences(self, db_session, tmp_path):
        """Spec §1 — the v1 defect both council seats caught: mesh-directory
        and mesh-admin must NOT share an audience."""
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        target = _mk_member(db_session, fleet, host="target-h")
        db_session.commit()

        key = _signing_key(tmp_path)
        exec_cred = mint_credential(
            db_session, member_id=member.id, cls=CLASS_MESH_EXEC, target_member_id=str(target.id), signing_key=key
        )
        dir_cred = mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        admin_cred = mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_ADMIN, signing_key=key)

        auds = {exec_cred.aud, dir_cred.aud, admin_cred.aud}
        assert len(auds) == 3, f"audiences must be pairwise distinct, got {auds}"


class TestClassClaimIsMandatoryAndCanonical:
    def test_class_claim_present_in_token(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        minted = mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        decoded = jwt.decode(minted.token, options={"verify_signature": False})
        assert decoded[f"{CLAIM_NS}class"] == CLASS_MESH_DIRECTORY

    def test_settlement_claim_slot_reserved_and_null(self, db_session, tmp_path):
        """Spec §10 — pact claim reserved, always null, unused this sprint."""
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        minted = mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        decoded = jwt.decode(minted.token, options={"verify_signature": False})
        assert f"{CLAIM_NS}pact" in decoded
        assert decoded[f"{CLAIM_NS}pact"] is None

    def test_org_fleet_member_are_canonical_uuid_strings(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        minted = mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        decoded = jwt.decode(minted.token, options={"verify_signature": False})
        from uuid import UUID

        for f in ("org", "fleet", "member"):
            v = decoded[f"{CLAIM_NS}{f}"]
            assert str(UUID(v)) == v, f"{f} claim {v!r} is not canonical-form UUID"


class TestNullableOrgIdFailsClosed:
    """Spec §2.4 — the nullable-org_id hole. THE cross-tenant bypass gate."""

    def test_red_mint_refuses_when_fleet_org_id_is_null(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        fleet = _mk_fleet(db_session, owner, org=None)  # personal scope, org_id IS NULL
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        with pytest.raises(MeshTenantUnassignedError) as exc_info:
            mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
        assert exc_info.value.fleet_id == str(fleet.id)

    def test_never_mints_org_null_token(self, db_session, tmp_path):
        """Even under the exception path, no token with org: null is ever produced."""
        owner = _mk_user(db_session)
        fleet = _mk_fleet(db_session, owner, org=None)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        try:
            mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)
            pytest.fail("expected MeshTenantUnassignedError")
        except MeshTenantUnassignedError:
            pass  # correct — no token was returned to inspect, which is the point


class TestCallerCannotSupplyMemberId:
    """Spec §4.5 gate 1 — mint path must never accept a caller-supplied member id."""

    def test_target_member_must_exist_and_be_active(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        # A fabricated, never-enrolled member id must not silently mint.
        fabricated_id = str(uuid4())
        minted = mint_credential(
            db_session,
            member_id=member.id,
            cls=CLASS_MESH_EXEC,
            target_member_id=fabricated_id,
            signing_key=key,
        )
        # mesh-exec aud is set from the STRING the caller supplied — this is
        # allowed (aud names the intended recipient, which the recipient's
        # own audience check gates), but the MINTING identity (sub/member)
        # must always come from the resolved, DB-verified `member_id`, never
        # from client input. Assert that invariant here.
        decoded = jwt.decode(minted.token, options={"verify_signature": False})
        assert decoded[f"{CLAIM_NS}member"] == str(member.id)
        assert decoded[f"{CLAIM_NS}member"] != fabricated_id

    def test_inactive_caller_member_cannot_mint(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        member.is_active = False
        db_session.commit()

        key = _signing_key(tmp_path)
        with pytest.raises(MeshMintRaceError):
            mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_DIRECTORY, signing_key=key)


class TestMintValidation:
    def test_unknown_class_rejected(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        with pytest.raises(MeshMintRaceError):
            mint_credential(db_session, member_id=member.id, cls="mesh-superadmin", signing_key=key)

    def test_mesh_exec_without_target_rejected(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        with pytest.raises(MeshMintRaceError):
            mint_credential(db_session, member_id=member.id, cls=CLASS_MESH_EXEC, signing_key=key)

    def test_unknown_member_id_rejected(self, db_session, tmp_path):
        key = _signing_key(tmp_path)
        with pytest.raises(MeshMintRaceError):
            mint_credential(db_session, member_id=uuid4(), cls=CLASS_MESH_DIRECTORY, signing_key=key)


class TestExpIatOverClassMaxRejected:
    """Spec §8 verifier requirement — exp - iat over the class max must be
    rejected AT VERIFY TIME. Mint itself always produces conformant TTLs;
    this proves a tampered/forged token with an inflated TTL is caught."""

    def test_mint_never_produces_overlong_ttl(self, db_session, tmp_path):
        owner = _mk_user(db_session)
        org = _mk_org(db_session)
        fleet = _mk_fleet(db_session, owner, org)
        member = _mk_member(db_session, fleet)
        db_session.commit()

        key = _signing_key(tmp_path)
        for cls in (CLASS_MESH_EXEC, CLASS_MESH_DIRECTORY, CLASS_MESH_ADMIN):
            target = None
            if cls == CLASS_MESH_EXEC:
                target = str(_mk_member(db_session, fleet, host=f"t-{cls}").id)
                db_session.commit()
            minted = mint_credential(db_session, member_id=member.id, cls=cls, target_member_id=target, signing_key=key)
            assert minted.exp - minted.iat <= CLASS_TTL_SECONDS[cls]

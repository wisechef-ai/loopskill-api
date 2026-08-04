"""The REAL lock #17 proof — plan §3 T0-D.5(b), the corrected independence
test from T0-B.

Not "does a bare script verify a signature" (that's test_verify_mesh_
credential.py — authentication only). This proves a RECEIVER wired with
ONLY the reference verifier + a JWKS snapshot REJECTS a cross-tenant call
— even when the caller has a perfectly valid, correctly-signed,
correct-audience credential for its OWN org and merely requests a
DIFFERENT org's profile. No Hermes import, no LoopSkill package import.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parent))

from jwks_snapshot import JWKSStateMachine  # noqa: E402
from receiver_demo import MeshReceiver, TenantRejected  # noqa: E402

NS = "https://loopskill.io/claims/"
ISS = "https://app.loopskill.io"


def _mint(private_key, kid, *, aud, org, cls="mesh-exec", ttl=900):
    now = int(time.time())
    claims = {
        "iss": ISS,
        "sub": f"lsm:member:{uuid.uuid4()}",
        "aud": aud,
        "exp": now + ttl,
        "iat": now,
        "nbf": now,
        "jti": str(uuid.uuid4()),
        f"{NS}org": org,
        f"{NS}fleet": str(uuid.uuid4()),
        f"{NS}member": str(uuid.uuid4()),
        f"{NS}class": cls,
        f"{NS}pact": None,
    }
    return jwt.encode(claims, private_key, algorithm="EdDSA", headers={"kid": kid, "typ": "at+jwt"})


@pytest.fixture()
def mesh_world():
    """One issuer keypair, one receiver identity, two entitled orgs."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    kid = "world-kid"
    snapshot = JWKSStateMachine(fetch_jwks_fn=lambda: {kid: pub})
    snapshot.bootstrap()

    receiver_id = f"lsm:member:{uuid.uuid4()}"
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())

    receiver = MeshReceiver(
        my_audience=receiver_id,
        org_to_profiles={org_a: ["profile-a"], org_b: ["profile-b"]},
        snapshot=snapshot,
    )
    return {"priv": priv, "kid": kid, "receiver": receiver, "receiver_id": receiver_id, "org_a": org_a, "org_b": org_b}


class TestReceiverAcceptsLegitimateCall:
    def test_org_reaches_its_own_profile(self, mesh_world):
        token = _mint(mesh_world["priv"], mesh_world["kid"], aud=mesh_world["receiver_id"], org=mesh_world["org_a"])
        result = mesh_world["receiver"].handle_call(token, requested_tenant="profile-a")
        assert result["profile"] == "profile-a"
        assert result["org"] == mesh_world["org_a"]


class TestReceiverRejectsCrossTenantCall:
    """THE gate. A valid signature + correct audience is NOT sufficient —
    the org claim must also be entitled to the REQUESTED profile."""

    def test_red_org_a_cannot_reach_org_b_profile(self, mesh_world):
        # org_a has a perfectly valid credential — signed by the real
        # issuer key, correct audience (this receiver), unexpired, correct
        # class. It is simply not entitled to org_b's profile.
        token = _mint(mesh_world["priv"], mesh_world["kid"], aud=mesh_world["receiver_id"], org=mesh_world["org_a"])

        with pytest.raises(TenantRejected) as exc_info:
            mesh_world["receiver"].handle_call(token, requested_tenant="profile-b")

        assert "requested_tenant_not_in_allowed_set" in exc_info.value.reason

    def test_red_org_b_cannot_reach_org_a_profile(self, mesh_world):
        token = _mint(mesh_world["priv"], mesh_world["kid"], aud=mesh_world["receiver_id"], org=mesh_world["org_b"])

        with pytest.raises(TenantRejected) as exc_info:
            mesh_world["receiver"].handle_call(token, requested_tenant="profile-a")

        assert "requested_tenant_not_in_allowed_set" in exc_info.value.reason

    def test_unmapped_org_rejected_even_without_a_specific_target(self, mesh_world):
        unmapped_org = str(uuid.uuid4())
        token = _mint(mesh_world["priv"], mesh_world["kid"], aud=mesh_world["receiver_id"], org=unmapped_org)

        with pytest.raises(TenantRejected) as exc_info:
            mesh_world["receiver"].handle_call(token, requested_tenant="profile-a")

        assert "tenant_not_entitled" in exc_info.value.reason

    def test_wrong_audience_rejected_before_org_is_even_read(self, mesh_world):
        """A token minted for a DIFFERENT receiver must fail at
        authentication — the org/tenant logic never even runs."""
        token = _mint(mesh_world["priv"], mesh_world["kid"], aud="lsm:member:some-other-receiver", org=mesh_world["org_a"])

        with pytest.raises(TenantRejected) as exc_info:
            mesh_world["receiver"].handle_call(token, requested_tenant="profile-a")

        assert "authentication_failed" in exc_info.value.reason

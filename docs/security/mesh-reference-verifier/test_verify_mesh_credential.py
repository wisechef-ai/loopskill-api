"""Runnable tests for the reference verifier — spec §8, plan §3 T0-D.4.

**These tests import NOTHING from `app` or any LoopSkill package.** That is
itself the T0-D gate ("Verifier has no LoopSkill-specific dependency") —
proven by construction, not by inspection. Run standalone:

    cd docs/security/mesh-reference-verifier
    /path/to/venv/bin/python -m pytest test_verify_mesh_credential.py -v

(also collected by the main repo's pytest run, since it lives under a path
pytest can discover — but its independence does not depend on that).
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
from replay_store import InMemoryReplayStore  # noqa: E402
from verify_mesh_credential import ISS, verify  # noqa: E402

NS = "https://loopskill.io/claims/"


def _snapshot_with_key(kid: str, public_key) -> JWKSStateMachine:
    snap = JWKSStateMachine(fetch_jwks_fn=lambda: {kid: public_key})
    snap.bootstrap()
    return snap


def _mint(private_key, kid, *, aud, cls, org=None, fleet=None, member=None, ttl=900, iat_offset=0):
    now = int(time.time()) + iat_offset
    org = org or str(uuid.uuid4())
    fleet = fleet or str(uuid.uuid4())
    member = member or str(uuid.uuid4())
    claims = {
        "iss": ISS,
        "sub": f"lsm:member:{member}",
        "aud": aud,
        "exp": now + ttl,
        "iat": now,
        "nbf": now,
        "jti": str(uuid.uuid4()),
        f"{NS}org": org,
        f"{NS}fleet": fleet,
        f"{NS}member": member,
        f"{NS}class": cls,
        f"{NS}pact": None,
    }
    return jwt.encode(claims, private_key, algorithm="EdDSA", headers={"kid": kid, "typ": "at+jwt"})


@pytest.fixture()
def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


class TestHappyPath:
    def test_verifies_a_real_credential(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        token = _mint(priv, "k1", aud="loopskill-api", cls="mesh-directory")
        claims = verify(token, "loopskill-api", snap, InMemoryReplayStore())
        assert claims["aud"] == "loopskill-api"
        assert claims[f"{NS}class"] == "mesh-directory"


class TestRejectsForgedCredential:
    def test_signature_from_wrong_key_rejected(self, keypair):
        _priv, pub = keypair
        forger_priv = Ed25519PrivateKey.generate()
        snap = _snapshot_with_key("k1", pub)
        forged = _mint(forger_priv, "k1", aud="loopskill-api", cls="mesh-directory")
        with pytest.raises(Exception):
            verify(forged, "loopskill-api", snap, InMemoryReplayStore())

    def test_unknown_kid_rejected(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        token = _mint(priv, "k-not-in-ring", aud="loopskill-api", cls="mesh-directory")
        with pytest.raises(Exception):
            verify(token, "loopskill-api", snap, InMemoryReplayStore())


class TestWrongAudienceRejected:
    def test_rejected(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        token = _mint(priv, "k1", aud="loopskill-api", cls="mesh-directory")
        with pytest.raises(Exception):
            verify(token, "lsm:member:someone-else", snap, InMemoryReplayStore())


class TestExpiredRejected:
    def test_rejected(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        token = _mint(priv, "k1", aud="loopskill-api", cls="mesh-directory", ttl=10, iat_offset=-1000)
        with pytest.raises(Exception):
            verify(token, "loopskill-api", snap, InMemoryReplayStore())


class TestTtlWindowMatchesStatedClassTtl:
    def test_mesh_exec_within_900s_accepted(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        token = _mint(priv, "k1", aud="lsm:member:x", cls="mesh-exec", ttl=900)
        claims = verify(token, "lsm:member:x", snap, InMemoryReplayStore())
        assert claims[f"{NS}class"] == "mesh-exec"

    def test_ttl_over_class_maximum_rejected(self, keypair):
        """Spec §8 — exp - iat over the class max must be rejected. A
        forged/tampered token claiming mesh-exec (900s max) with an
        8000s window must not verify even with a VALID signature — an
        attacker with mint access to one class must not extend TTL."""
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        token = _mint(priv, "k1", aud="lsm:member:x", cls="mesh-exec", ttl=8000)
        with pytest.raises(ValueError, match="ttl exceeds class maximum"):
            verify(token, "lsm:member:x", snap, InMemoryReplayStore())


class TestArrayAudienceRejected:
    def test_rejected(self, keypair):
        """Spec §5 — PyJWT accepts an array aud if ANY element matches.
        The verifier must explicitly reject non-string aud."""
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": "lsm:member:x",
            "aud": ["loopskill-api", "loopskill-api-admin"],
            "exp": now + 900,
            "iat": now,
            "nbf": now,
            "jti": str(uuid.uuid4()),
            f"{NS}org": str(uuid.uuid4()),
            f"{NS}fleet": str(uuid.uuid4()),
            f"{NS}member": str(uuid.uuid4()),
            f"{NS}class": "mesh-directory",
            f"{NS}pact": None,
        }
        token = jwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1", "typ": "at+jwt"})
        with pytest.raises(ValueError, match="array audience"):
            verify(token, "loopskill-api", snap, InMemoryReplayStore())


class TestOrgNullOrMissingRejected:
    def test_null_org_rejected(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": "lsm:member:x",
            "aud": "loopskill-api",
            "exp": now + 900,
            "iat": now,
            "nbf": now,
            "jti": str(uuid.uuid4()),
            f"{NS}org": None,
            f"{NS}fleet": str(uuid.uuid4()),
            f"{NS}member": str(uuid.uuid4()),
            f"{NS}class": "mesh-directory",
            f"{NS}pact": None,
        }
        token = jwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1", "typ": "at+jwt"})
        with pytest.raises(ValueError, match="bad org"):
            verify(token, "loopskill-api", snap, InMemoryReplayStore())

    def test_missing_org_rejected(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": "lsm:member:x",
            "aud": "loopskill-api",
            "exp": now + 900,
            "iat": now,
            "nbf": now,
            "jti": str(uuid.uuid4()),
            f"{NS}fleet": str(uuid.uuid4()),
            f"{NS}member": str(uuid.uuid4()),
            f"{NS}class": "mesh-directory",
            f"{NS}pact": None,
        }
        token = jwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1", "typ": "at+jwt"})
        with pytest.raises(ValueError, match="bad org"):
            verify(token, "loopskill-api", snap, InMemoryReplayStore())

    def test_empty_string_org_rejected(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": "lsm:member:x",
            "aud": "loopskill-api",
            "exp": now + 900,
            "iat": now,
            "nbf": now,
            "jti": str(uuid.uuid4()),
            f"{NS}org": "",
            f"{NS}fleet": str(uuid.uuid4()),
            f"{NS}member": str(uuid.uuid4()),
            f"{NS}class": "mesh-directory",
            f"{NS}pact": None,
        }
        token = jwt.encode(claims, priv, algorithm="EdDSA", headers={"kid": "k1", "typ": "at+jwt"})
        with pytest.raises(ValueError, match="bad org"):
            verify(token, "loopskill-api", snap, InMemoryReplayStore())


class TestClassConfusionRejected:
    """Spec §1 gate — a mesh-directory token CANNOT reach a mesh-admin
    operation. The verifier alone cannot enforce this (it only authenticates
    — see the docstring at the bottom of verify_mesh_credential.py); this
    test proves the (aud, class) PAIR the verifier returns is sufficient for
    a receiver to enforce the matrix itself, and that swapping audiences
    doesn't let a directory-class token pass as admin."""

    def test_directory_class_token_cannot_present_as_admin_audience(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        # A directory-class token minted (correctly) for loopskill-api aud
        # must fail verification against the loopskill-api-admin audience —
        # audience is the coarse separator (spec §1).
        token = _mint(priv, "k1", aud="loopskill-api", cls="mesh-directory")
        with pytest.raises(Exception):
            verify(token, "loopskill-api-admin", snap, InMemoryReplayStore())

    def test_receiver_enforces_class_even_when_audience_would_match(self, keypair):
        """Defence in depth: even if a caller ONLY checked audience and not
        class, class is present and mandatory in the return value so a
        conformant receiver has no excuse to skip it (spec §1 rule 1)."""
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        token = _mint(priv, "k1", aud="loopskill-api", cls="mesh-directory")
        claims = verify(token, "loopskill-api", snap, InMemoryReplayStore())
        assert claims[f"{NS}class"] == "mesh-directory"
        # A receiver implementing the (aud, class, operation) matrix (spec
        # §1) would reject this claim set for any mesh-admin-only operation
        # BECAUSE class != "mesh-admin" — proving the information needed to
        # do so is present and correctly labelled.
        assert claims[f"{NS}class"] != "mesh-admin"


class TestReplayRejected:
    def test_same_jti_twice_is_rejected_second_time(self, keypair):
        priv, pub = keypair
        snap = _snapshot_with_key("k1", pub)
        store = InMemoryReplayStore()
        token = _mint(priv, "k1", aud="loopskill-api", cls="mesh-directory")
        verify(token, "loopskill-api", snap, store)  # first use — accepted
        with pytest.raises(ValueError, match="replay"):
            verify(token, "loopskill-api", snap, store)  # second use — replay


class TestNoNetworkCallDuringVerify:
    def test_verify_never_calls_fetch_jwks_fn(self, keypair):
        """Spec §3.1 — verification never performs a network fetch."""
        priv, pub = keypair
        calls = {"n": 0}

        def _fetch():
            calls["n"] += 1
            return {"k1": pub}

        snap = JWKSStateMachine(fetch_jwks_fn=_fetch)
        snap.bootstrap()
        assert calls["n"] == 1  # only the explicit bootstrap call

        token = _mint(priv, "k1", aud="loopskill-api", cls="mesh-directory")
        verify(token, "loopskill-api", snap, InMemoryReplayStore())
        assert calls["n"] == 1, "verify() must not trigger any additional fetch"

"""LoopSkill mesh credential RECEIVER — reference implementation + demo.

Spec: plan §3 T0-D.5(b) — "that same script wired as a RECEIVER rejects a
cross-tenant call — the corrected independence test from T0-B."

This is the SECOND, real proof of lock #17: not "does a bare script verify
a signature" (that's authentication — verify_mesh_credential.py already
proves it) but "does a receiver that ONLY has the reference verifier + the
JWKS actually enforce tenant isolation the way spec §2.2 requires."

**No Hermes import. No LoopSkill package import.** This is a standalone
receiver any A2A-speaking runtime could ship. It implements spec §2.2's
NORMATIVE org->profile mapping exactly:

    1. verified = verify(token, my_audience)        # signature, aud, class, TTL
    2. org      = verified["…/claims/org"]           # UUID, canonical (§7)
    3. allowed  = LOCAL_ORG_TO_PROFILES[org]         # local config; absence -> REJECT
    4. profile  = select(allowed, request.tenant)    # body value is a SELECTOR ONLY
    5. if profile not in allowed: REJECT

Step 4 is the ONLY place a request-body value is ever read, and it can only
NARROW a set the signature already authorised — this is what closes the
T0-B defect (adapter.py reading `tenant` from the request body and routing
on it with the signature checked and discarded). A receiver that reaches
step 4 without completing step 3 is non-conformant, per spec §2.2.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from replay_store import InMemoryReplayStore  # noqa: E402
from verify_mesh_credential import verify  # noqa: E402


class TenantRejected(Exception):
    """Raised whenever the receiver refuses a call. Carries a machine-
    readable reason so the demo/tests can assert WHY, not just THAT."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class MeshReceiver:
    """A minimal, conformant mesh-exec receiver.

    `my_audience` is THIS receiver's own `lsm:member:<uuid>` identity — the
    value it checks incoming tokens' `aud` against. `org_to_profiles` is
    LOCAL config: which locally-hosted agent profile(s) each org is
    entitled to reach. This is deliberately NOT learned from the token or
    from any Agent Card — it is operator-configured, exactly as spec §2.2
    requires ("endpoint URLs are never a trust input", §4.6).
    """

    def __init__(self, my_audience: str, org_to_profiles: dict[str, list[str]], snapshot, replay_store=None):
        self.my_audience = my_audience
        self.org_to_profiles = org_to_profiles
        self.snapshot = snapshot
        self.replay_store = replay_store if replay_store is not None else InMemoryReplayStore()

    def handle_call(self, token: str, requested_tenant: str | None = None) -> dict:
        """Handle one inbound mesh-exec call. Returns the routed profile +
        the verified claims on success. Raises TenantRejected on any
        rejection — including "verification succeeded but tenant is
        unentitled", which is the whole point of this class.
        """
        # Step 1 — authenticate. Any verification failure is an
        # unconditional reject; there is no degraded/fallback mode (§3.3).
        try:
            verified = verify(token, self.my_audience, self.snapshot, self.replay_store)
        except Exception as exc:  # noqa: BLE001 — deliberately broad: ANY verify failure rejects
            raise TenantRejected(f"authentication_failed: {exc}") from exc

        # Step 2 — the TENANCY decision reads org and nothing else (§2.1).
        org = verified.get("https://loopskill.io/claims/org")

        # Step 3 — absence is REJECTION, never default-allow (§2.2 step 3).
        allowed = self.org_to_profiles.get(org)
        if not allowed:
            raise TenantRejected(f"tenant_not_entitled: org {org!r} has no configured profile mapping")

        # Step 4 — the request body's `tenant` is a SELECTOR ONLY, and can
        # only NARROW the set the signature already authorised. This is the
        # ONLY place body-supplied data is consulted, and it happens AFTER
        # step 3, never instead of it — the T0-B defect (adapter.py:588)
        # read `tenant` from the body and routed on it directly, with
        # `identity` computed but never passed into the routing call.
        if requested_tenant is not None:
            if requested_tenant not in allowed:
                # Step 5 — narrowing outside the authorised set REJECTS,
                # it does not silently fall back to "pick the first one".
                raise TenantRejected(
                    f"requested_tenant_not_in_allowed_set: requested={requested_tenant!r} allowed={allowed!r}"
                )
            profile = requested_tenant
        else:
            if len(allowed) != 1:
                raise TenantRejected(f"ambiguous_profile_selection: allowed={allowed!r}, no tenant selector given")
            profile = allowed[0]

        return {"profile": profile, "org": org, "claims": verified}


if __name__ == "__main__":
    # Runnable demo — see docs/security/mesh-reference-verifier/README.md
    # for the full narrative. This block is exercised by
    # test_receiver_cross_tenant_rejection.py; running it directly prints a
    # human-readable trace of the same three calls that test makes.
    import time
    import uuid

    import jwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from jwks_snapshot import JWKSStateMachine

    NS = "https://loopskill.io/claims/"
    ISS = "https://app.loopskill.io"

    issuer_priv = Ed25519PrivateKey.generate()
    issuer_pub = issuer_priv.public_key()
    kid = "demo-kid"

    def mint(*, aud, org, cls="mesh-exec"):
        now = int(time.time())
        claims = {
            "iss": ISS,
            "sub": f"lsm:member:{uuid.uuid4()}",
            "aud": aud,
            "exp": now + 900,
            "iat": now,
            "nbf": now,
            "jti": str(uuid.uuid4()),
            f"{NS}org": org,
            f"{NS}fleet": str(uuid.uuid4()),
            f"{NS}member": str(uuid.uuid4()),
            f"{NS}class": cls,
            f"{NS}pact": None,
        }
        return jwt.encode(claims, issuer_priv, algorithm="EdDSA", headers={"kid": kid, "typ": "at+jwt"})

    snapshot = JWKSStateMachine(fetch_jwks_fn=lambda: {kid: issuer_pub})
    snapshot.bootstrap()

    receiver_id = f"lsm:member:{uuid.uuid4()}"
    org_astrovita = str(uuid.uuid4())
    org_praga = str(uuid.uuid4())

    receiver = MeshReceiver(
        my_audience=receiver_id,
        org_to_profiles={
            org_astrovita: ["astrovita-assistant"],
            org_praga: ["praga-assistant"],
        },
        snapshot=snapshot,
    )

    print("=== Legitimate call: astrovita org, correct aud, its own profile ===")
    good_token = mint(aud=receiver_id, org=org_astrovita)
    result = receiver.handle_call(good_token, requested_tenant="astrovita-assistant")
    print("ACCEPTED:", result["profile"])

    print("\n=== Cross-tenant attack: praga org (itself entitled to its OWN profile)")
    print("    presents a valid, correctly-signed, correct-audience token but")
    print("    requests ASTROVITA's profile ===")
    cross_tenant_token = mint(aud=receiver_id, org=org_praga)
    try:
        receiver.handle_call(cross_tenant_token, requested_tenant="astrovita-assistant")
        print("VULNERABLE: cross-tenant call was ACCEPTED — this must never print")
    except TenantRejected as exc:
        print("REJECTED (correct):", exc.reason)

    print("\n=== Unmapped org (no entitlement configured at all) ===")
    no_org_token = mint(aud=receiver_id, org=str(uuid.uuid4()))  # some org with NO mapping at all
    try:
        receiver.handle_call(no_org_token, requested_tenant="astrovita-assistant")
        print("VULNERABLE: unmapped org call was ACCEPTED — this must never print")
    except TenantRejected as exc:
        print("REJECTED (correct):", exc.reason)

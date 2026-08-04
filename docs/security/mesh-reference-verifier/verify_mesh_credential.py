"""LoopSkill mesh credential verifier — reference implementation.

Spec: projects/loopskill/plans/2026-08-04-mesh0408-T0C-credential-trust-spec.md §8

Deps: pyjwt[crypto] only. NO LoopSkill package. NO Hermes import.
NO network call in this function — `snapshot` (a JWKSStateMachine from
jwks_snapshot.py) is consulted in-memory only; refresh happens off the
request path, on the caller's own schedule.

This is the literal spec §8 verifier, unmodified in logic. It exists here,
in docs/, specifically so it can be imported and run WITHOUT the LoopSkill
package on the Python path — that portability is itself the T0-D gate
("Verifier has no LoopSkill-specific dependency").
"""

from __future__ import annotations

import uuid

import jwt

ISS = "https://app.loopskill.io"
NS = "https://loopskill.io/claims/"
MAX_TTL = {"mesh-exec": 900, "mesh-directory": 3600, "mesh-admin": 600}
LEEWAY = 60


def verify(token: str, my_aud: str, snapshot, seen_jti) -> dict:
    """Validate a mesh credential FOR ME. `snapshot` is the locally-managed
    JWKS state machine (jwks_snapshot.py); `seen_jti` is the shared atomic
    replay store (replay_store.py, or a production Redis-backed equivalent
    with the same insert_if_absent contract).

    Raises on any failure. There is no partial success.
    """
    hdr = jwt.get_unverified_header(token)
    if hdr.get("alg") != "EdDSA" or hdr.get("typ") != "at+jwt":
        raise ValueError("bad header")
    key = snapshot.key_for(hdr.get("kid"))  # unknown/retired/stale -> raises
    c = jwt.decode(
        token,
        key,
        algorithms=["EdDSA"],
        audience=my_aud,
        issuer=ISS,
        leeway=LEEWAY,
        options={
            "require": ["exp", "iat", "nbf", "aud", "iss", "sub", "jti"],
            "verify_aud": True,
            "verify_exp": True,
            "verify_iss": True,
        },
    )
    if not isinstance(c["aud"], str):  # §5 — arrays are multi-target, reject
        raise ValueError("array audience")
    cls = c.get(NS + "class")
    if cls not in MAX_TTL:
        raise ValueError("bad class")
    if c["exp"] - c["iat"] > MAX_TTL[cls] + LEEWAY:  # §4 bounds are worthless
        raise ValueError("ttl exceeds class maximum")  # if never checked here
    for f in ("org", "fleet", "member"):
        v = c.get(NS + f)
        if not v or str(uuid.UUID(str(v))) != v:  # §7 canonical form, rejects
            raise ValueError(f"bad {f}")  # null/empty/non-canonical
    if not seen_jti.insert_if_absent(c["jti"], ttl=MAX_TTL[cls] + 2 * LEEWAY):
        raise ValueError("replay")
    return c


# ── What this STILL does not do — the docs MUST say so (spec §8) ──────────
#
# - It does not make the authorization decision. It returns an `org`.
#   Spec §2.2's `org -> allowed profiles` mapping is the tenancy boundary
#   and it is NOT optional and NOT application-specific — it is normative
#   and it is the caller's to implement. See receiver_demo.py for a worked
#   example of a conformant receiver implementing that mapping.
# - It does not manage the JWKS snapshot. `snapshot` is jwks_snapshot.py's
#   JWKSStateMachine (last_success, next_refresh at 2880s, hard_expiry at
#   86400s, atomic swap, off-request-path refresh, rate-limited unknown-kid
#   handling). A hand-rolled substitute is where spec §3 gets silently
#   violated — ship this component, don't re-derive it.
# - It does not provide the replay store. `seen_jti` must be shared across
#   processes and hosts in production (spec §5) — replay_store.py's
#   InMemoryReplayStore is single-process only and says so.
# - It does not check the clock's integrity (spec §4.8). The verifying host
#   must run NTP/chrony with a monitored sync state and must itself refuse
#   to verify anything if the clock is known-unsynchronised — that check
#   lives outside this function, at the process/host level.
#
# Say this plainly: this function tells you WHO is calling and that the
# credential is well-formed and unexpired. It does not tell you WHAT they
# may reach. If you deploy it and believe you have tenant isolation, you
# have authentication and no authorization, and that gap is where the
# incident happens.

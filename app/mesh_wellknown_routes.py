"""mesh_0408 T0-D — public discovery endpoints. Spec §9, §9.1.

GET /.well-known/jwks.json                    — the mesh signing public keys
GET /.well-known/oauth-authorization-server    — RFC 8414 metadata (NOT OIDC — see spec §9)

Both are:
  - Unauthenticated BY DESIGN — verification must need no credential. Added
    to APIKeyMiddleware.EXEMPT_PATHS (app/middleware/api_key.py) as exact
    path matches.
  - Rate-limited — via the EXISTING RateLimitMiddleware (app/middleware/
    rate_limit.py), which applies its per-IP sliding-window limit to every
    path except its own small EXEMPT_PATHS set; `.well-known/*` is not in
    that set, so these routes inherit the standard 60/min-per-IP bound for
    free. No bespoke limiter is added here — one rate-limiting mechanism,
    not two that can drift out of sync.
  - Size-bounded — the JWKS body is proportional to the number of keys in
    the ring (bounded by the small number of kids ever active/retired at
    once; there is no user-controlled input that can inflate it), and the
    RFC 8414 document is a fixed, static shape.
  - Served with `Cache-Control: max-age=3600` + `ETag` so §3's client-side
    cache TTL is enforceable server-side, not merely requested.

**Deployment note — spec §9.1's "verify as T0-D's first step" (see the PR
body for the full write-up):** confirmed LIVE against production
(`curl -D- https://app.loopskill.io/.well-known/<anything>`) that
`/.well-known/*` root paths already reach this FastAPI application — the
401 body returned is APIKeyMiddleware's own JSON
(`{"detail":"Invalid or missing x-api-key header"}`), not a Caddy/portal
404. Un-mapped root paths outside `/api/*` and `/.well-known/*` DO 404 at
the edge (verified: `/randomnonexistent123` -> 404, `/.well-known/x` ->
401), so Caddy is already proxying this specific prefix through. **No edge
change is required** for these two routes to become live once merged and
deployed — adding them to EXEMPT_PATHS is sufficient.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Response

from app.mesh.constants import ISS
from app.mesh.keys import build_jwks
from app.version import __version__  # noqa: F401 — kept for future service_documentation versioning

router = APIRouter(tags=["mesh", "well-known"])

_CACHE_CONTROL = "public, max-age=3600"

# Spec §9 — RFC 8414 metadata, NOT OIDC discovery. Empty grant/response
# types is the honest statement: credentials are issued through an
# authenticated LoopSkill API call, not an OAuth flow. No endpoint is
# advertised that does not exist (v1's authorization_endpoint defect).
_METADATA_DOC = {
    "issuer": ISS,
    "jwks_uri": f"{ISS}/.well-known/jwks.json",
    "grant_types_supported": [],
    "response_types_supported": [],
    "token_endpoint_auth_methods_supported": [],
    "service_documentation": f"{ISS}/docs/mesh-credentials",
    "claims_supported": [
        "iss",
        "sub",
        "aud",
        "exp",
        "iat",
        "nbf",
        "jti",
        "https://loopskill.io/claims/org",
        "https://loopskill.io/claims/fleet",
        "https://loopskill.io/claims/member",
        "https://loopskill.io/claims/class",
        "https://loopskill.io/claims/pact",
    ],
}


def _etagged_json(body: dict) -> Response:
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    etag = hashlib.sha256(payload).hexdigest()[:16]
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Cache-Control": _CACHE_CONTROL, "ETag": f'"{etag}"'},
    )


@router.get("/.well-known/jwks.json")
def mesh_jwks() -> Response:
    """The mesh Ed25519 public key ring. Unauthenticated — spec §9.1."""
    return _etagged_json(build_jwks())


@router.get("/.well-known/oauth-authorization-server")
def mesh_oauth_metadata() -> Response:
    """RFC 8414 authorization server metadata for mesh credentials. Spec §9."""
    return _etagged_json(_METADATA_DOC)

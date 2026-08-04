"""mesh_0408 T0-D — wire-format constants. Spec §1, §2, §7, §9.

These are the ONLY place class/TTL/audience mappings are defined. Mint and
verify both import from here so the two sides cannot drift.
"""

from __future__ import annotations

ISS = "https://app.loopskill.io"
CLAIM_NS = "https://loopskill.io/claims/"

ADMIN_AUD = "loopskill-api-admin"
DIRECTORY_AUD = "loopskill-api"

# Spec §1 — three classes, three DISTINCT audiences, TTL in seconds.
# mesh-exec's audience is computed per-mint (the receiving member's id), so
# it is not a fixed string here.
CLASS_MESH_EXEC = "mesh-exec"
CLASS_MESH_DIRECTORY = "mesh-directory"
CLASS_MESH_ADMIN = "mesh-admin"

VALID_CLASSES = (CLASS_MESH_EXEC, CLASS_MESH_DIRECTORY, CLASS_MESH_ADMIN)

# Spec §1 table + §4.1 (revocation exposure windows reuse these TTLs).
CLASS_TTL_SECONDS: dict[str, int] = {
    CLASS_MESH_EXEC: 900,
    CLASS_MESH_DIRECTORY: 3600,
    CLASS_MESH_ADMIN: 600,
}

# Spec §4.8 — clock leeway, both directions, applied at verify time.
LEEWAY_SECONDS = 60

# Spec §4.1 — per-class revocation exposure window = TTL + leeway.
CLASS_REVOCATION_EXPOSURE_SECONDS: dict[str, int] = {
    cls: ttl + LEEWAY_SECONDS for cls, ttl in CLASS_TTL_SECONDS.items()
}

# Spec §5 — jti retention = TTL + 2 * leeway (replay store TTL).
CLASS_JTI_RETENTION_SECONDS: dict[str, int] = {
    cls: ttl + 2 * LEEWAY_SECONDS for cls, ttl in CLASS_TTL_SECONDS.items()
}

# Spec §3 — JWKS snapshot state machine bounds.
JWKS_CACHE_TTL_SECONDS = 3600  # fresh snapshot lifetime
JWKS_REFRESH_AT_SECONDS = 2880  # 80% of cache TTL — schedule an out-of-band refresh
JWKS_HARD_EXPIRY_SECONDS = 86400  # stale snapshot hard stop — reject everything past this
JWKS_UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 60  # §3.1 — at most one refresh/process/minute
JWKS_REFRESH_TIMEOUT_SECONDS = 2  # §3.1 — never 30s

# Spec §4.3 — key rotation overlap: 2 * cache_TTL + max(class TTL) + leeway.
KEY_ROTATION_OVERLAP_SECONDS = 2 * JWKS_CACHE_TTL_SECONDS + max(CLASS_TTL_SECONDS.values()) + LEEWAY_SECONDS
assert KEY_ROTATION_OVERLAP_SECONDS == 10860  # 3h 01m — spec §4.3, round UP to 4h in ops docs

HEADER_ALG = "EdDSA"
HEADER_TYP = "at+jwt"

# Spec §2 required claim names (bare, i.e. before namespacing).
REQUIRED_STANDARD_CLAIMS = ("exp", "iat", "nbf", "aud", "iss", "sub", "jti")
REQUIRED_PRIVATE_CLAIM_SUFFIXES = ("org", "fleet", "member", "class")

"""mesh_0408 T0-D — the SEPARATE Ed25519 mesh signing key ring. Spec §0, §0.2, §0.3.

**Why this is a separate module from app/auth.py, permanently, per spec §0.2:**

    app/auth.py:verify_jwt() builds its candidate secret list as:

        if kid and kid in keys:
            secrets_to_try.append(keys[kid])
        # Always append the legacy secret as a fallback so tokens signed
        # before JWT_KEYS was populated continue to verify successfully.
        secrets_to_try.append(settings.JWT_SECRET)

    That fallback is UNCONDITIONAL. On the session-JWT path it is a
    deliberate migration affordance. On a mesh path it would be
    catastrophic: retiring a `kid` would revoke nothing, because the legacy
    secret still verifies. This module and its verifier NEVER fall back to
    anything. Unknown kid -> reject. Missing kid -> reject. Retired kid ->
    reject. There is no secondary secret, because there is no secret at all
    on the verification side — only public keys.

**Key custody (spec §0.3), normative:**
  1. The Ed25519 PRIVATE key is generated on the host that signs, via
     ``scripts/mesh_keygen.py`` (uses `Ed25519PrivateKey.generate()`).
     Never on a laptop, never pasted into chat, never committed.
  2. Storage: a PEM file at `settings.MESH_SIGNING_KEY_PATH`, mode 0600,
     owned by the service user, OUTSIDE the repo tree and outside any
     web-servable directory. NOT an env var (env vars leak into /proc,
     crash dumps, subprocess environments, `docker inspect`).
  3. Access: read once at process start (`load_signing_key()`), held in
     memory. No endpoint, log line, admin tool, or debug route may return
     the private key material. `load_signing_key()` is the ONLY function
     in this codebase permitted to open MESH_SIGNING_KEY_PATH.
  4. Backup: one encrypted copy in Bitwarden, tagged
     `loopskill-mesh-signing-key`. Documented in docs/security/mesh-key-custody.md.
  5. Compromise playbook: retire the kid immediately (§4.3's rotation
     overlap does NOT apply to a compromise), generate a new key, publish,
     re-issue. Named owner: Adam. See docs/security/mesh-key-custody.md.
  6. Rotation trigger: every 180 days routine, or immediately on suspicion.

**The JWKS side is public-key-only, by construction:** `build_jwks()` reads
PEM files from `settings.MESH_JWKS_DIR` (a directory of PUBLIC keys only)
and never touches `MESH_SIGNING_KEY_PATH`. A misconfiguration that pointed
JWKS-serving code at the private key path would be a code review finding,
not a silent leak — the loader in this module explicitly types the two
functions differently (`load_signing_key` returns a private key object;
`load_public_keys` returns public key objects) so a mix-up is a TypeError
at the crypto layer, not a runtime leak.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)
from jwt.algorithms import get_default_algorithms

from app.mesh.errors import MeshKeyRingError

_OKP_ALG = get_default_algorithms()["EdDSA"]


@dataclass(frozen=True)
class SigningKey:
    """An active signing key: the kid it signs with + its private key object.

    Deliberately does NOT carry any serialized form of the private key — the
    caller holds this in memory for the process lifetime and it is never
    logged, returned from an endpoint, or written back to disk.
    """

    kid: str
    private_key: Ed25519PrivateKey


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair. Returns (private_pem, public_pem).

    Used only by scripts/mesh_keygen.py (and tests). Never call this to
    generate a production key on a non-signing host — spec §0.3 rule 1.
    """
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    public_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return private_pem, public_pem


def _check_private_key_file_mode(path: Path) -> None:
    """Refuse to load a private key file with permissions looser than 0600.

    Spec §0.3 rule 2: mode 0600, owned by the service user. This is a
    defence-in-depth check, not the only control (filesystem ACLs / host
    hardening are the real boundary) — but a key file the group or world can
    read is a custody violation we can catch cheaply at load time.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise MeshKeyRingError(
            f"refusing to load mesh signing key at {path}: mode {oct(mode)} is "
            f"more permissive than 0600 (group/other bits set). Spec §0.3 rule 2."
        )


def load_signing_key(key_path: str | None = None, kid: str | None = None) -> SigningKey:
    """Load the active mesh signing private key from disk. Spec §0.3 rule 3.

    Reads `settings.MESH_SIGNING_KEY_PATH` / `settings.MESH_SIGNING_KID` by
    default (params exist for testability). Raises MeshKeyRingError if
    unconfigured, missing, wrongly-permissioned, or not a valid Ed25519 key
    — mesh minting fails closed rather than silently falling back to
    anything.
    """
    from app.config import settings

    key_path = key_path if key_path is not None else settings.MESH_SIGNING_KEY_PATH
    kid = kid if kid is not None else settings.MESH_SIGNING_KID

    if not key_path or not kid:
        raise MeshKeyRingError(
            "mesh signing key not configured: set WR_MESH_SIGNING_KEY_PATH and "
            "WR_MESH_SIGNING_KID. Run scripts/mesh_keygen.py to provision a key."
        )

    path = Path(key_path)
    if not path.is_file():
        raise MeshKeyRingError(f"mesh signing key file not found: {path}")

    _check_private_key_file_mode(path)

    pem_bytes = path.read_bytes()
    try:
        private_key = load_pem_private_key(pem_bytes, password=None)
    except ValueError as exc:
        raise MeshKeyRingError(f"mesh signing key at {path} is not a valid PEM private key: {exc}") from exc
    finally:
        # Best-effort scrub of the PEM bytes from the local variable's backing
        # buffer is not possible in pure Python (bytes are immutable); the
        # mitigation is scope — pem_bytes goes out of scope at function
        # return and is not retained anywhere else in this module.
        pass

    if not isinstance(private_key, Ed25519PrivateKey):
        raise MeshKeyRingError(
            f"mesh signing key at {path} is a {type(private_key).__name__}, not Ed25519. "
            f"Spec §0 — EdDSA is the sole signing algorithm, no fallback algorithm."
        )

    return SigningKey(kid=kid, private_key=private_key)


def load_public_keys(jwks_dir: str | None = None) -> dict[str, Ed25519PublicKey]:
    """Load every public key in the ring, keyed by kid. Spec §0.3 — JWKS side.

    Reads `<kid>.pub.pem` files from `settings.MESH_JWKS_DIR`. Includes
    retired-but-not-yet-expired keys so recently-issued tokens still verify
    during the rotation overlap window (spec §4.3). Never reads the private
    key path — this function's whole point is that it CANNOT leak private
    material because it never opens a private-key-shaped file.
    """
    from app.config import settings

    jwks_dir = jwks_dir if jwks_dir is not None else settings.MESH_JWKS_DIR
    if not jwks_dir:
        return {}

    dir_path = Path(jwks_dir)
    if not dir_path.is_dir():
        return {}

    keys: dict[str, Ed25519PublicKey] = {}
    for entry in sorted(dir_path.glob("*.pub.pem")):
        kid = entry.name[: -len(".pub.pem")]
        if not kid:
            continue
        try:
            public_key = load_pem_public_key(entry.read_bytes())
        except ValueError:
            continue
        if isinstance(public_key, Ed25519PublicKey):
            keys[kid] = public_key
    return keys


def public_key_to_jwk(kid: str, public_key: Ed25519PublicKey) -> dict:
    """Render one Ed25519 public key as a JWK dict (RFC 8037 OKP), with kid/use/alg."""
    jwk = _OKP_ALG.to_jwk(public_key, as_dict=True)
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "EdDSA"
    return jwk


def build_jwks(jwks_dir: str | None = None) -> dict:
    """Build the full JWKS document (`{"keys": [...]}`) from the public ring."""
    keys = load_public_keys(jwks_dir)
    return {"keys": [public_key_to_jwk(kid, pk) for kid, pk in sorted(keys.items())]}

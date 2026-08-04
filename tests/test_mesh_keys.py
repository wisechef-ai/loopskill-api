"""mesh_0408 T0-D — Ed25519 mesh key ring. Spec §0, §0.2, §0.3.

Covers:
  - generate_keypair / load_signing_key / load_public_keys / build_jwks
  - custody: file-mode enforcement (0600), fail-closed when unconfigured
  - the NEGATIVE test that proves there is NO legacy-secret fallback on the
    mesh path — the single most important gate in this phase (spec §0.2).
"""

from __future__ import annotations

import os
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.mesh.errors import MeshKeyRingError
from app.mesh.keys import (
    build_jwks,
    generate_keypair,
    load_public_keys,
    load_signing_key,
    public_key_to_jwk,
)


def _write_key_files(tmp_path, kid: str = "test-kid-1"):
    priv_pem, pub_pem = generate_keypair()
    priv_path = tmp_path / "mesh_signing.pem"
    priv_path.write_bytes(priv_pem)
    os.chmod(priv_path, 0o600)

    jwks_dir = tmp_path / "jwks"
    jwks_dir.mkdir()
    (jwks_dir / f"{kid}.pub.pem").write_bytes(pub_pem)

    return priv_path, jwks_dir, kid


class TestGenerateKeypair:
    def test_generates_ed25519_keys(self):
        priv_pem, pub_pem = generate_keypair()
        assert b"BEGIN PRIVATE KEY" in priv_pem
        assert b"BEGIN PUBLIC KEY" in pub_pem


class TestLoadSigningKey:
    def test_loads_valid_key(self, tmp_path):
        priv_path, _jwks_dir, kid = _write_key_files(tmp_path)
        key = load_signing_key(key_path=str(priv_path), kid=kid)
        assert key.kid == kid
        assert isinstance(key.private_key, Ed25519PrivateKey)

    def test_raises_when_unconfigured(self):
        with pytest.raises(MeshKeyRingError):
            load_signing_key(key_path="", kid="")

    def test_raises_when_file_missing(self, tmp_path):
        with pytest.raises(MeshKeyRingError):
            load_signing_key(key_path=str(tmp_path / "does-not-exist.pem"), kid="k1")

    def test_raises_on_loose_permissions(self, tmp_path):
        """Spec §0.3 rule 2 — mode 0600. A group/world-readable key file must
        be refused at load time, not silently accepted."""
        priv_pem, _pub_pem = generate_keypair()
        priv_path = tmp_path / "loose.pem"
        priv_path.write_bytes(priv_pem)
        os.chmod(priv_path, 0o644)  # world-readable — violation

        with pytest.raises(MeshKeyRingError, match="0600"):
            load_signing_key(key_path=str(priv_path), kid="k1")

    def test_raises_on_non_ed25519_key(self, tmp_path):
        """Spec §0 — EdDSA is the sole signing algorithm. A well-formed but
        wrong-algorithm key (e.g. RSA) must be rejected, never silently used."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = rsa_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        path = tmp_path / "rsa.pem"
        path.write_bytes(pem)
        os.chmod(path, 0o600)

        with pytest.raises(MeshKeyRingError, match="Ed25519"):
            load_signing_key(key_path=str(path), kid="k1")


class TestLoadPublicKeysAndJwks:
    def test_loads_ring_by_kid(self, tmp_path):
        _priv_path, jwks_dir, kid = _write_key_files(tmp_path)
        keys = load_public_keys(jwks_dir=str(jwks_dir))
        assert kid in keys

    def test_empty_when_unconfigured(self):
        assert load_public_keys(jwks_dir="") == {}

    def test_build_jwks_shape(self, tmp_path):
        _priv_path, jwks_dir, kid = _write_key_files(tmp_path)
        jwks = build_jwks(jwks_dir=str(jwks_dir))
        assert "keys" in jwks
        assert len(jwks["keys"]) == 1
        jwk = jwks["keys"][0]
        assert jwk["kid"] == kid
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert jwk["alg"] == "EdDSA"
        assert jwk["use"] == "sig"
        # No private material of any kind in the published JWK.
        assert "d" not in jwk

    def test_multiple_kids_all_published(self, tmp_path):
        """Rotation overlap (spec §4.3) requires BOTH old and new kid
        published simultaneously during the overlap window."""
        _priv_path1, jwks_dir, kid1 = _write_key_files(tmp_path, kid="kid-old")
        priv_pem2, pub_pem2 = generate_keypair()
        (jwks_dir / "kid-new.pub.pem").write_bytes(pub_pem2)

        jwks = build_jwks(jwks_dir=str(jwks_dir))
        kids = {k["kid"] for k in jwks["keys"]}
        assert kids == {"kid-old", "kid-new"}


class TestNoLegacyFallbackOnMeshPath:
    """Spec §0.2 — THE core negative gate of this phase.

    app/auth.py::verify_jwt() unconditionally appends settings.JWT_SECRET as
    a fallback secret. The mesh key ring module (app.mesh.keys) must have NO
    equivalent — no function that accepts a legacy/fallback secret, and
    load_signing_key/load_public_keys must never consult
    settings.JWT_SECRET, settings.JWT_KEYS, or settings.JWT_ACTIVE_KID.
    """

    def test_mesh_keys_module_has_no_reference_to_legacy_jwt_settings(self):
        """Checks the CODE (function bodies), not the module docstring —
        the docstring quotes app/auth.py's fallback verbatim to explain why
        this module must never do the same thing, which would otherwise
        false-positive this exact check.
        """
        import ast
        import inspect

        import app.mesh.keys as keys_module

        tree = ast.parse(inspect.getsource(keys_module))
        # Re-render every function/class body (excluding module + function
        # docstrings) back to source and scan THAT — not the raw text, which
        # includes the explanatory module docstring.
        code_only_chunks: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                # Skip a leading docstring Expr node, if present.
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), (ast.Constant,)
                ) and isinstance(body[0].value.value, str):
                    body = body[1:]
                for stmt in body:
                    code_only_chunks.append(ast.unparse(stmt))

        code_only = "\n".join(code_only_chunks)
        for forbidden in ("JWT_SECRET", "JWT_KEYS", "JWT_ACTIVE_KID", "secrets_to_try"):
            assert forbidden not in code_only, (
                f"app.mesh.keys CODE (not docstring) references {forbidden!r} — the "
                f"mesh signing ring must be completely separate from the session-JWT "
                f"HMAC ring (spec §0.2). This is the fallback-trap regression gate."
            )

    def test_retired_kid_is_unresolvable_once_removed_from_ring(self, tmp_path):
        """Retiring a kid = deleting its .pub.pem from MESH_JWKS_DIR. Once
        gone, load_public_keys must not resolve it via ANY fallback path."""
        _priv_path, jwks_dir, kid = _write_key_files(tmp_path)
        assert kid in load_public_keys(jwks_dir=str(jwks_dir))

        (jwks_dir / f"{kid}.pub.pem").unlink()

        keys_after_retirement = load_public_keys(jwks_dir=str(jwks_dir))
        assert kid not in keys_after_retirement, (
            "retired kid still resolves — a fallback path exists where the spec "
            "requires none (§0.2: retired kid -> reject, always)."
        )

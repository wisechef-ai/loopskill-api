"""mesh_0408 T0-D — /.well-known/jwks.json + /.well-known/oauth-authorization-server.

Spec §9, §9.1. Both must serve UNAUTHENTICATED (no x-api-key needed), with
Cache-Control + ETag, and the RFC 8414 doc must NOT be an OIDC document.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import create_app


def _unauthenticated_client(db_session):
    """A raw TestClient with NO x-api-key header — proves the endpoints
    really are exempt from APIKeyMiddleware, using the REAL app (not the
    stripped-down tests/conftest.py client fixture, which pre-seeds an
    x-api-key header on every request and would mask a middleware
    regression)."""
    app = create_app()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, raise_server_exceptions=True)


class TestJwksEndpoint:
    def test_serves_unauthenticated(self, db_session):
        client = _unauthenticated_client(db_session)
        resp = client.get("/.well-known/jwks.json")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"keys": []} or "keys" in resp.json()

    def test_has_cache_control_and_etag(self, db_session):
        client = _unauthenticated_client(db_session)
        resp = client.get("/.well-known/jwks.json")
        assert "max-age=3600" in resp.headers.get("cache-control", "")
        assert resp.headers.get("etag")

    def test_no_private_key_material_ever_in_response(self, db_session, tmp_path, monkeypatch):
        from app.config import settings
        from app.mesh.keys import generate_keypair

        priv_pem, pub_pem = generate_keypair()
        jwks_dir = tmp_path / "jwks"
        jwks_dir.mkdir()
        (jwks_dir / "live-kid.pub.pem").write_bytes(pub_pem)
        monkeypatch.setattr(settings, "MESH_JWKS_DIR", str(jwks_dir))

        client = _unauthenticated_client(db_session)
        resp = client.get("/.well-known/jwks.json")
        body_text = resp.text
        assert "PRIVATE KEY" not in body_text
        assert '"d"' not in body_text  # OKP JWK private-key field name


class TestOAuthAuthorizationServerEndpoint:
    def test_serves_unauthenticated(self, db_session):
        client = _unauthenticated_client(db_session)
        resp = client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200, resp.text

    def test_is_rfc8414_not_oidc(self, db_session):
        """Spec §9 — must NOT advertise an authorization_endpoint (that would
        be OIDC discovery, rejected by both council seats)."""
        client = _unauthenticated_client(db_session)
        body = client.get("/.well-known/oauth-authorization-server").json()
        assert "authorization_endpoint" not in body
        assert "id_token_signing_alg_values_supported" not in body
        assert "scopes_supported" not in body
        assert body["grant_types_supported"] == []
        assert body["response_types_supported"] == []
        assert body["issuer"] == "https://app.loopskill.io"
        assert body["jwks_uri"] == "https://app.loopskill.io/.well-known/jwks.json"

    def test_has_cache_control_and_etag(self, db_session):
        client = _unauthenticated_client(db_session)
        resp = client.get("/.well-known/oauth-authorization-server")
        assert "max-age=3600" in resp.headers.get("cache-control", "")
        assert resp.headers.get("etag")

    def test_claims_supported_are_namespaced_private_claims(self, db_session):
        client = _unauthenticated_client(db_session)
        body = client.get("/.well-known/oauth-authorization-server").json()
        claims = set(body["claims_supported"])
        for c in (
            "https://loopskill.io/claims/org",
            "https://loopskill.io/claims/fleet",
            "https://loopskill.io/claims/member",
            "https://loopskill.io/claims/class",
            "https://loopskill.io/claims/pact",
        ):
            assert c in claims

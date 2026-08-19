"""agentreg_0819 — agent self-registration via Ed25519 proof-of-key.

THE WALL THIS REMOVES
---------------------
Every API-key mint went through ``app/api_key_routes.py:_require_user``, which
401s ``login_required`` without a human OAuth session. An autonomous agent that
discovers LoopSkill through llms.txt or an MCP directory has no browser and no
account, so it could browse the catalog and nothing else — no enrol, no publish,
no feedback. ``POST /api/agents/register`` authenticates the caller by
possession of an Ed25519 private key instead of by session.

WHAT THESE TESTS PIN
--------------------
Not "the happy path works" — that is the easy half. The load-bearing assertions
are the refusals, because this is a PUBLIC endpoint that mints a real API key:

* a forged signature, a stale timestamp and a replayed nonce are each refused;
* the per-IP and platform-wide 24h enrolment caps trip;
* a known pubkey gets 409 and NO fresh secret (the key-stuffing wall);
* a revoked identity's key stops working — on REST *and* over MCP;
* an agent key cannot reach checkout, admin or the sandbox;
* the ``.well-known`` documents answer 200 anonymously;
* the canonical string published in ``.well-known`` is byte-identical to the
  one the verifier actually checks — three copies of that string exist and
  drift between them would silently break every client.

RED-PROOFING
------------
Each negative test was confirmed to fail before its guard existed:
``test_bad_signature_is_401`` passes 201 with the verification call removed;
``test_replayed_nonce_is_401`` passes 201 without ``consume_nonce``;
``test_revoked_identity_key_is_rejected`` passes 200 without the
``_agent_identity`` gate; the ``.well-known`` tests return 401 without the
``EXEMPT_PATHS`` entries (the original defect).
"""

from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.services.agent_registration import canonical_registration_string
from tests._app_factory import build_test_app

REGISTER_PATH = "/api/agents/register"


# ── helpers ─────────────────────────────────────────────────────────────────


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    """Fresh Ed25519 keypair. Returns (private key, base64 raw public key)."""
    private = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private, base64.b64encode(raw).decode()


def _payload(
    private: Ed25519PrivateKey,
    pubkey_b64: str,
    *,
    agent_name: str = "tori-scout",
    timestamp: str | None = None,
    nonce: str | None = None,
    contact: str | None = None,
    sign_as: dict | None = None,
) -> dict:
    """Build a correctly signed registration body.

    ``sign_as`` overrides the values fed to the SIGNER only — used to forge a
    signature that is valid over a *different* claim than the one sent.
    """
    timestamp = timestamp or datetime.now(UTC).isoformat()
    nonce = nonce or secrets.token_hex(16)
    signed = {
        "pubkey": pubkey_b64,
        "timestamp": timestamp,
        "nonce": nonce,
        "agent_name": agent_name,
    }
    signed.update(sign_as or {})
    message = canonical_registration_string(**signed).encode("utf-8")
    signature = base64.b64encode(private.sign(message)).decode()
    body = {
        "pubkey": pubkey_b64,
        "timestamp": timestamp,
        "nonce": nonce,
        "agent_name": agent_name,
        "signature": signature,
    }
    if contact is not None:
        body["contact"] = contact
    return body


@pytest.fixture()
def app(db_session, monkeypatch):
    return build_test_app(db_session=db_session, monkeypatch=monkeypatch)


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _register(client: TestClient, **kwargs) -> tuple[dict, int]:
    private, pubkey = _keypair()
    r = client.post(REGISTER_PATH, json=_payload(private, pubkey, **kwargs))
    return (r.json() if r.content else {}), r.status_code


# ── happy path ──────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_registration_mints_a_scoped_free_tier_key(self, client, db_session):
        private, pubkey = _keypair()
        r = client.post(
            REGISTER_PATH,
            json=_payload(private, pubkey, agent_name="tori-scout", contact="tori@example.test"),
        )
        assert r.status_code == 201, r.text
        body = r.json()

        assert body["api_key"].startswith("rec_agent_")
        assert body["tier"] == "free"
        assert body["scope"] == "user"
        assert body["capabilities"]["run_sandbox"] is False
        assert body["capabilities"]["admin"] is False
        assert body["capabilities"]["checkout_billing"] is False
        assert body["capabilities"]["install_free_skills"] is True

        from app.models import AgentIdentity, APIKey

        identity = db_session.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).one()
        assert identity.agent_name == "tori-scout"
        assert identity.contact == "tori@example.test"
        assert identity.revoked is False

        keys = db_session.query(APIKey).filter(APIKey.user_id == identity.user_id).all()
        assert len(keys) == 1, "exactly one active key per identity"
        assert keys[0].is_active is True
        assert keys[0].is_sandbox_operator is False, "an agent key may never run the sandbox"

    def test_the_shadow_user_is_unreachable_from_oauth(self, client, db_session):
        """No github_id / google_id / email → no OAuth flow can land on it."""
        private, pubkey = _keypair()
        assert client.post(REGISTER_PATH, json=_payload(private, pubkey)).status_code == 201

        from app.models import AgentIdentity, User

        identity = db_session.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).one()
        user = db_session.query(User).filter(User.id == identity.user_id).one()
        assert user.github_id is None
        assert user.google_id is None
        assert user.email is None
        # Free tier is the ABSENCE of a live subscription — no pricing code touched.
        assert user.subscription_status is None
        assert user.subscription_tier is None

    def test_the_minted_key_authenticates_and_resolves_to_free_user_scope(
        self, client, db_session, app
    ):
        private, pubkey = _keypair()
        key = client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()["api_key"]

        from app.mcp.auth import validate_key

        resolved = validate_key(key, db_session)
        assert resolved["scope"] == "user", "rec_agent_ must validate on the MCP path too"
        assert resolved["auth_ctx"].tier is None  # free
        assert resolved["auth_ctx"].is_sandbox_operator is False

        from app.authz import can_run_sandbox

        assert can_run_sandbox(resolved["auth_ctx"]) is False


# ── proof-of-key refusals ───────────────────────────────────────────────────


class TestSignatureVerification:
    def test_bad_signature_is_401(self, client):
        private, pubkey = _keypair()
        body = _payload(private, pubkey)
        body["signature"] = base64.b64encode(b"\x00" * 64).decode()
        r = client.post(REGISTER_PATH, json=body)
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "invalid_signature"

    def test_signature_over_a_different_agent_name_is_401(self, client):
        """The name is INSIDE the canonical string — it is not post-hoc malleable."""
        private, pubkey = _keypair()
        body = _payload(private, pubkey, agent_name="honest", sign_as={"agent_name": "honest"})
        body["agent_name"] = "impostor"
        r = client.post(REGISTER_PATH, json=body)
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "invalid_signature"

    def test_signature_from_a_different_key_is_401(self, client):
        """A relayer cannot re-attribute someone else's signed enrolment."""
        attacker, _ = _keypair()
        _, victim_pubkey = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(attacker, victim_pubkey))
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "invalid_signature"

    def test_malformed_pubkey_is_400(self, client):
        private, _ = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(private, "not-base64!!"))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_pubkey"

    def test_wrong_length_pubkey_is_400(self, client):
        private, _ = _keypair()
        short = base64.b64encode(b"\x01" * 16).decode()
        r = client.post(REGISTER_PATH, json=_payload(private, short))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_pubkey"


class TestTimestampWindow:
    @pytest.mark.parametrize("delta_minutes", [-10, 10])
    def test_stale_or_future_timestamp_is_401(self, client, delta_minutes):
        private, pubkey = _keypair()
        stamp = (datetime.now(UTC) + timedelta(minutes=delta_minutes)).isoformat()
        r = client.post(REGISTER_PATH, json=_payload(private, pubkey, timestamp=stamp))
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "timestamp_out_of_range"

    def test_a_timestamp_just_inside_the_window_is_accepted(self, client):
        private, pubkey = _keypair()
        stamp = (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
        r = client.post(REGISTER_PATH, json=_payload(private, pubkey, timestamp=stamp))
        assert r.status_code == 201, r.text

    def test_unparseable_timestamp_is_400(self, client):
        private, pubkey = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(private, pubkey, timestamp="yesterday"))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_timestamp"


class TestNonceReplay:
    def test_replayed_nonce_is_401(self, client):
        """A captured payload must not enrol twice, ever."""
        private, pubkey = _keypair()
        body = _payload(private, pubkey)
        assert client.post(REGISTER_PATH, json=body).status_code == 201

        replay = client.post(REGISTER_PATH, json=body)
        assert replay.status_code == 401
        assert replay.json()["detail"]["error"] == "nonce_replayed"

    def test_a_nonce_is_burned_across_distinct_keypairs(self, client):
        """The nonce is global, not per-pubkey — a shared nonce pool is one pool."""
        nonce = secrets.token_hex(16)
        first, first_pub = _keypair()
        assert (
            client.post(REGISTER_PATH, json=_payload(first, first_pub, nonce=nonce)).status_code
            == 201
        )
        second, second_pub = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(second, second_pub, nonce=nonce))
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "nonce_replayed"

    def test_short_nonce_is_400(self, client):
        private, pubkey = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(private, pubkey, nonce="ab" * 8))
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_nonce"


class TestDuplicatePubkey:
    def test_reregistration_is_409_and_issues_no_secret(self, client):
        """The key-stuffing wall: a known pubkey never yields a fresh secret."""
        private, pubkey = _keypair()
        assert client.post(REGISTER_PATH, json=_payload(private, pubkey)).status_code == 201

        # Fresh nonce + fresh timestamp: a genuine re-registration, not a replay.
        again = client.post(REGISTER_PATH, json=_payload(private, pubkey))
        assert again.status_code == 409
        detail = again.json()["detail"]
        assert detail["error"] == "pubkey_already_registered"
        assert "rotation" in detail, "409 must point at key rotation"
        assert "rec_agent_" not in again.text, "no secret may cross a 409"


# ── the abuse wall ──────────────────────────────────────────────────────────


def _set_caps(monkeypatch, *, per_ip: int | None = None, global_: int | None = None) -> None:
    """Override the enrolment caps on the live settings object for one test."""
    from app.config import settings

    if per_ip is not None:
        monkeypatch.setattr(settings, "AGENT_REGISTRATION_PER_IP_PER_DAY", per_ip)
    if global_ is not None:
        monkeypatch.setattr(settings, "AGENT_REGISTRATION_GLOBAL_PER_DAY", global_)


class TestRateLimits:
    def test_per_ip_cap_trips_with_429(self, client, monkeypatch):
        _set_caps(monkeypatch, per_ip=2)
        for i in range(2):
            _, status = _register(client, agent_name=f"ok-{i}")
            assert status == 201

        body, status = _register(client, agent_name="over")
        assert status == 429
        assert body["detail"]["error"] == "ip_registration_limit"

    def test_global_cap_trips_with_429(self, client, monkeypatch):
        """The backstop against a farm that spreads across many IPs."""
        _set_caps(monkeypatch, per_ip=100, global_=2)
        for i in range(2):
            _, status = _register(client, agent_name=f"ok-{i}")
            assert status == 201

        body, status = _register(client, agent_name="over")
        assert status == 429
        assert body["detail"]["error"] == "global_registration_limit"

    def test_a_refused_registration_does_not_consume_quota(self, client, monkeypatch):
        """Signature failure must not let an attacker exhaust someone's cap."""
        _set_caps(monkeypatch, per_ip=1)
        private, pubkey = _keypair()
        bad = _payload(private, pubkey)
        bad["signature"] = base64.b64encode(b"\x00" * 64).decode()
        assert client.post(REGISTER_PATH, json=bad).status_code == 401

        _, status = _register(client)
        assert status == 201, "the forged attempt must not have burned the cap"


# ── revocation ──────────────────────────────────────────────────────────────


class TestRevocation:
    def _register_and_revoke(self, client, db_session):
        private, pubkey = _keypair()
        body = client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()
        key = body["api_key"]

        from app.models import AgentIdentity
        from app.services.agent_registration import revoke_identity

        identity = db_session.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).one()
        revoke_identity(db_session, identity.id)
        return key, identity

    def test_a_live_agent_key_works_on_the_route_the_revocation_test_uses(
        self, client, db_session
    ):
        """Control for the test below — otherwise a 401 could mean anything."""
        private, pubkey = _keypair()
        key = client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()["api_key"]
        assert client.get("/api/bundles", headers={"x-api-key": key}).status_code == 200

    def test_revoked_identity_key_is_rejected_on_rest(self, client, db_session):
        key, _ = self._register_and_revoke(client, db_session)
        r = client.get("/api/bundles", headers={"x-api-key": key})
        assert r.status_code == 401, r.text

    def test_revoked_identity_key_is_rejected_over_mcp(self, client, db_session):
        """MCP is the universal path — a REST-only revocation is not one."""
        key, _ = self._register_and_revoke(client, db_session)
        from app.mcp.auth import validate_key

        assert validate_key(key, db_session)["scope"] == "unauthorized"

    def _revoke_flag_only(self, client, db_session) -> str:
        """Set ``revoked`` and leave every key row ACTIVE.

        This isolates the ``_agent_identity`` gate: with the keys deactivated
        too (what the real revoke path does), an ordinary ``is_active`` filter
        would refuse the key anyway and the test would pass without the gate.
        """
        private, pubkey = _keypair()
        key = client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()["api_key"]

        from app.models import AgentIdentity, APIKey

        identity = db_session.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).one()
        identity.revoked = True
        db_session.flush()
        active = db_session.query(APIKey).filter(APIKey.user_id == identity.user_id).one()
        assert active.is_active is True, "this test is only meaningful with the key still active"
        return key

    def test_revocation_flag_alone_blocks_rest(self, client, db_session):
        """Fail closed on the FLAG, not only on api_keys.is_active."""
        key = self._revoke_flag_only(client, db_session)
        r = client.get("/api/bundles", headers={"x-api-key": key})
        assert r.status_code == 401, r.text
        assert "revoked" in r.text.lower()

    def test_revocation_flag_alone_blocks_mcp(self, client, db_session):
        key = self._revoke_flag_only(client, db_session)
        from app.mcp.auth import validate_key

        assert validate_key(key, db_session)["scope"] == "unauthorized"

    def test_revoke_endpoint_requires_the_master_key(self, client, db_session):
        private, pubkey = _keypair()
        agent_key = client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()["api_key"]

        from app.models import AgentIdentity

        identity = db_session.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).one()
        r = client.post(
            f"/api/admin/agent-identities/{identity.id}/revoke",
            headers={"x-api-key": agent_key},
        )
        assert r.status_code == 403, "an agent may not revoke identities"

    def test_master_key_can_revoke(self, client, db_session):
        from app.config import settings

        private, pubkey = _keypair()
        client.post(REGISTER_PATH, json=_payload(private, pubkey))

        from app.models import AgentIdentity

        identity = db_session.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).one()
        r = client.post(
            f"/api/admin/agent-identities/{identity.id}/revoke",
            headers={"x-api-key": settings.API_KEY},
        )
        assert r.status_code == 200, r.text
        assert r.json()["identity"]["revoked"] is True


# ── what an agent key must NOT reach ────────────────────────────────────────


class TestAgentKeyIsFenced:
    @pytest.fixture()
    def agent_key(self, client):
        private, pubkey = _keypair()
        return client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()["api_key"]

    def test_cannot_start_checkout(self, client, agent_key):
        r = client.post("/api/checkout/pro", headers={"x-api-key": agent_key})
        assert r.status_code in (401, 403, 404), r.text
        assert r.status_code != 200

    def test_cannot_read_billing(self, client, agent_key):
        r = client.get("/api/billing/me", headers={"x-api-key": agent_key})
        assert r.status_code in (401, 403, 404), r.text

    def test_cannot_hit_admin(self, client, agent_key):
        r = client.post("/api/admin/reindex-all", headers={"x-api-key": agent_key})
        assert r.status_code == 403, r.text

    def test_cannot_list_agent_identities(self, client, agent_key):
        r = client.get("/api/admin/agent-identities", headers={"x-api-key": agent_key})
        assert r.status_code == 403, r.text

    def test_cannot_mint_more_keys_via_the_human_route(self, client, agent_key):
        """POST /api/api-keys is JWT-only; an agent has no session to present."""
        r = client.post("/api/api-keys", headers={"x-api-key": agent_key}, json={})
        assert r.status_code == 401, r.text

    def test_cannot_run_the_sandbox(self, client, agent_key, db_session):
        from app.authz import can_run_sandbox
        from app.mcp.auth import validate_key

        ctx = validate_key(agent_key, db_session)["auth_ctx"]
        assert can_run_sandbox(ctx) is False


# ── public discovery ────────────────────────────────────────────────────────


class TestWellKnownIsPublic:
    """These 401'd before this change — EXEMPT_PATHS listed only the mesh docs."""

    @pytest.mark.parametrize("path", ["/.well-known/agent.json", "/.well-known/mcp.json"])
    def test_200_anonymously(self, client, path):
        r = client.get(path)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers.get("ETag")
        assert "max-age" in r.headers.get("Cache-Control", "")

    def test_agent_json_publishes_the_registration_contract(self, client):
        doc = client.get("/.well-known/agent.json").json()
        reg = doc["registration"]
        assert reg["endpoint"].endswith("/api/agents/register")
        assert reg["algorithm"] == "Ed25519"
        assert reg["issues"]["key_prefix"] == "rec_agent_"
        assert "sandbox_run" in reg["denies"]
        assert doc["mcp"]["endpoint"].endswith("/api/mcp/http")
        assert doc["catalog"]["stats"].endswith("/api/stats")

    def test_published_canonical_string_matches_the_verifier(self, client):
        """Three copies of this string exist; drift breaks every client silently."""
        published = client.get("/.well-known/agent.json").json()["registration"][
            "canonical_string"
        ]
        assert published == canonical_registration_string(
            pubkey="{pubkey}",
            timestamp="{timestamp}",
            nonce="{nonce}",
            agent_name="{agent_name}",
        )
        assert published == (
            "loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}"
        )

    def test_route_docstring_documents_the_same_canonical_string(self):
        from app.agent_registration_routes import register_agent_route

        doc = register_agent_route.__doc__ or ""
        assert "loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}" in doc

    def test_no_secrets_leak_into_the_discovery_documents(self, client):
        from app.config import settings

        for path in ("/.well-known/agent.json", "/.well-known/mcp.json"):
            text = client.get(path).text
            assert settings.API_KEY not in text
            assert settings.JWT_SECRET not in text
            assert settings.SIGNING_SECRET not in text

    def test_mcp_json_points_at_self_registration(self, client):
        doc = client.get("/.well-known/mcp.json").json()
        assert doc["transport"]["url"].endswith("/api/mcp/http")
        assert doc["authentication"]["self_registration"].endswith("/api/agents/register")


class TestMiddlewareAllowlist:
    def test_register_is_public_for_post_only(self, client):
        """The namespace stays closed to every other verb."""
        r = client.get("/api/agents/register")
        assert r.status_code in (401, 404, 405), r.text
        assert r.status_code != 200

    def test_register_reaches_the_route_without_any_credential(self, client):
        """An empty body must fail VALIDATION (422), not the x-api-key gate."""
        r = client.post(REGISTER_PATH, json={})
        assert r.status_code == 422, r.text
        assert "x-api-key" not in r.text.lower()

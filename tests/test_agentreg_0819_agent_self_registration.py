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
import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Request
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


class TestNonceCanonicality:
    """Review round 2, F7 — ``bytes.fromhex`` is not a validator.

    It accepts UPPERCASE, tolerates ASCII whitespace inside the string, and has
    no upper bound. Each is a second spelling of one nonce, and the replay wall
    hashes the STRING it was handed — so ``"AB…"`` and ``"ab…"`` burn two
    different rows for what a reader would call one value.
    """

    @pytest.mark.parametrize(
        ("nonce", "why"),
        [
            (secrets.token_hex(16).upper(), "uppercase is a second spelling of one nonce"),
            (secrets.token_hex(8) + " " + secrets.token_hex(8), "inner whitespace"),
            (" " + secrets.token_hex(16), "leading whitespace"),
            (secrets.token_hex(16) + "\n", "trailing newline"),
            ("a" * 33, "odd length — 33 hex chars is not a whole number of bytes"),
            # 140 chars: past the 128-char semantic cap but inside the field's
            # outer max_length, so this reaches the SERVICE rule rather than
            # bouncing off Pydantic with a 422.
            ("ab" * 70, "oversize: unauthenticated callers write this table"),
            (secrets.token_hex(16) + "zz", "non-hex characters"),
        ],
    )
    def test_non_canonical_nonce_is_400(self, client, nonce, why):
        private, pubkey = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(private, pubkey, nonce=nonce))
        assert r.status_code == 400, f"{why}: {r.text}"
        assert r.json()["detail"]["error"] == "invalid_nonce", why

    def test_the_canonical_lowercase_form_still_registers(self, client):
        """Control — the rejections above must not be rejecting everything."""
        private, pubkey = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(private, pubkey, nonce=secrets.token_hex(32)))
        assert r.status_code == 201, r.text

    def test_the_uppercase_form_of_a_burned_nonce_is_still_refused(self, client):
        """The point of the rule: no alternate spelling gets a second use."""
        nonce = secrets.token_hex(16)
        first, first_pub = _keypair()
        assert client.post(REGISTER_PATH, json=_payload(first, first_pub, nonce=nonce)).status_code == 201

        second, second_pub = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(second, second_pub, nonce=nonce.upper()))
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "invalid_nonce"


def _alt_pad_bit_spellings(pubkey_b64: str) -> list[str]:
    """Every OTHER base64 string that decodes to the same 32 raw bytes.

    32 bytes is 256 bits; 43 significant base64 characters carry 258. The final
    significant character therefore has 4 real bits and 2 SLACK bits, so four
    strings — differing only in those 2 bits — decode identically, and
    ``b64decode(validate=True)`` accepts all four. Returns the three that are
    not the canonical spelling.
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    assert pubkey_b64.endswith("=") and len(pubkey_b64) == 44, pubkey_b64
    head, last = pubkey_b64[:42], pubkey_b64[42]
    base_index = alphabet.index(last) & ~0b11
    spellings = [f"{head}{alphabet[base_index | bits]}=" for bits in range(4)]
    canonical = base64.b64encode(base64.b64decode(pubkey_b64)).decode()
    return [s for s in spellings if s != canonical]


class TestPubkeyCanonicality:
    """Review round 2, F3 — base64 is not injective onto its own text.

    Round 1 put UNIQUE on the pubkey TEXT and decoded with
    ``b64decode(validate=True)``, which rejects out-of-alphabet characters but
    NOT non-zero trailing pad bits. So four different strings decoded to one
    key and each minted its own identity, shadow user and ``rec_agent_`` key —
    the 409 key-stuffing wall was bypassable by re-spelling the same key.
    """

    def test_the_premise_holds_alternate_spellings_really_do_decode_alike(self):
        """If this ever fails the rest of the class is testing nothing."""
        _, pubkey = _keypair()
        alts = _alt_pad_bit_spellings(pubkey)
        assert len(alts) == 3, "expected exactly 3 non-canonical spellings"
        for alt in alts:
            assert alt != pubkey
            assert base64.b64decode(alt, validate=True) == base64.b64decode(pubkey)

    def test_every_alternate_spelling_is_rejected_outright(self, client):
        private, pubkey = _keypair()
        for alt in _alt_pad_bit_spellings(pubkey):
            r = client.post(REGISTER_PATH, json=_payload(private, alt))
            assert r.status_code == 400, f"{alt} was accepted: {r.text}"
            assert r.json()["detail"]["error"] == "invalid_pubkey"

    def test_a_respelling_cannot_mint_a_second_identity(self, client, db_session):
        """The actual exploit: enrol, then re-enrol the same key under an alias."""
        private, pubkey = _keypair()
        assert client.post(REGISTER_PATH, json=_payload(private, pubkey)).status_code == 201

        for alt in _alt_pad_bit_spellings(pubkey):
            r = client.post(REGISTER_PATH, json=_payload(private, alt))
            assert r.status_code != 201, f"{alt} minted a duplicate identity"
            assert "rec_agent_" not in r.text, "no second secret may be issued for one key"

        from app.models import AgentIdentity

        raw = base64.b64decode(pubkey)
        fingerprint = hashlib.sha256(raw).hexdigest()
        rows = (
            db_session.query(AgentIdentity)
            .filter(AgentIdentity.pubkey_sha256 == fingerprint)
            .all()
        )
        assert len(rows) == 1, "one keypair, one identity"

    def test_uniqueness_is_enforced_by_the_DATABASE_not_only_by_the_check(
        self, client, db_session
    ):
        """Defence in depth: bypass the service and the UNIQUE still refuses.

        The canonicality check and the raw-bytes UNIQUE are two independent
        fixes on purpose. This one proves the second is real by inserting
        straight through the ORM, as a future code path that forgot the check
        would.
        """
        from sqlalchemy.exc import IntegrityError

        from app.models import AgentIdentity, User

        private, pubkey = _keypair()
        assert client.post(REGISTER_PATH, json=_payload(private, pubkey)).status_code == 201
        fingerprint = hashlib.sha256(base64.b64decode(pubkey)).hexdigest()

        shadow = User(display_name="agent:sneaky", is_agent=True)
        db_session.add(shadow)
        db_session.flush()
        with pytest.raises(IntegrityError):
            with db_session.begin_nested():
                db_session.add(
                    AgentIdentity(
                        pubkey=_alt_pad_bit_spellings(pubkey)[0],
                        pubkey_sha256=fingerprint,
                        agent_name="sneaky",
                        user_id=shadow.id,
                        revoked=False,
                    )
                )
                db_session.flush()

    def test_whitespace_and_unpadded_pubkeys_are_rejected(self, client):
        private, pubkey = _keypair()
        for bad in (pubkey.rstrip("="), f" {pubkey}", f"{pubkey}\n", pubkey.replace("=", "")):
            r = client.post(REGISTER_PATH, json=_payload(private, bad))
            assert r.status_code == 400, f"{bad!r} was accepted: {r.text}"
            assert r.json()["detail"]["error"] == "invalid_pubkey"


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


def _MASTER_KEY() -> str:
    """The configured master key — the non-agent control credential."""
    from app.config import settings

    return settings.API_KEY


def _postgres_url() -> str | None:
    """The suite's DB URL when it is pointed at a real Postgres server, else None.

    conftest's ``engine_fixture`` honours TEST_DATABASE_URL / DATABASE_URL /
    WR_DATABASE_URL; the Postgres-only concurrency test keys off the same
    signal so it runs in the CI Postgres job and skips on a local SQLite run.
    """
    import os

    for var in ("TEST_DATABASE_URL", "DATABASE_URL", "WR_DATABASE_URL"):
        url = os.environ.get(var)
        if url and url.startswith(("postgresql", "postgres://")):
            return url
    return None


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

    def test_a_duplicate_pubkey_409_does_not_consume_quota(self, client, monkeypatch):
        """Review round 2, F4. The most likely honest client bug is a retry.

        Round 1 reserved the slot BEFORE the duplicate-pubkey check, so an agent
        that re-sent its own registration spent its daily allowance collecting
        409s. The 409 must be free; only a mint may charge.
        """
        _set_caps(monkeypatch, per_ip=2)
        private, pubkey = _keypair()
        assert client.post(REGISTER_PATH, json=_payload(private, pubkey)).status_code == 201

        for _ in range(4):
            again = client.post(REGISTER_PATH, json=_payload(private, pubkey))
            assert again.status_code == 409, again.text

        _, status = _register(client, agent_name="second-real-agent")
        assert status == 201, "the 409s must not have burned the remaining slot"

    def test_cap_refusals_carry_retry_after(self, client, monkeypatch):
        """429 without Retry-After tells a retry loop nothing. F4."""
        _set_caps(monkeypatch, per_ip=1)
        assert _register(client)[1] == 201

        private, pubkey = _keypair()
        r = client.post(REGISTER_PATH, json=_payload(private, pubkey))
        assert r.status_code == 429, r.text
        assert int(r.headers["Retry-After"]) > 0
        assert r.json()["detail"]["retry_after"] == int(r.headers["Retry-After"])

    def test_the_global_cap_logs_a_stable_alertable_event(self, client, monkeypatch, caplog):
        """F4 — a platform-wide lockout must be an INCIDENT, not a silent outage.

        The global cap closes enrolment for every agent on earth, so the one
        thing that makes it an acceptable trade is that tripping it is loud and
        the ceiling is raiseable without a redeploy. ``agent_registration_global_cap``
        is the stable key an alert rule fires on; renaming it silently is what
        this test exists to prevent.
        """
        from app.services.agent_registration import GLOBAL_CAP_EVENT

        _set_caps(monkeypatch, per_ip=100, global_=1)
        assert _register(client)[1] == 201

        with caplog.at_level("WARNING", logger="app.services.agent_registration"):
            body, status = _register(client, agent_name="over")
        assert status == 429
        assert body["detail"]["error"] == "global_registration_limit"

        hits = [r for r in caplog.records if GLOBAL_CAP_EVENT in r.getMessage()]
        assert hits, f"no {GLOBAL_CAP_EVENT} warning was emitted"
        assert hits[0].levelname == "WARNING"
        assert getattr(hits[0], "event", None) == GLOBAL_CAP_EVENT, "structured field for alerting"
        # The remediation must be discoverable from the log line alone.
        assert "WR_AGENT_REGISTRATION_GLOBAL_PER_DAY" in hits[0].getMessage()

    def test_the_global_cap_is_raiseable_without_a_redeploy(self, client, monkeypatch):
        """The other half of the trade: reopening is an env change, not a release."""
        _set_caps(monkeypatch, per_ip=100, global_=1)
        assert _register(client)[1] == 201
        assert _register(client, agent_name="blocked")[1] == 429

        _set_caps(monkeypatch, global_=5)
        assert _register(client, agent_name="after-raise")[1] == 201

    def test_a_blocked_ip_does_not_drain_the_global_budget(self, client, monkeypatch):
        """Per-IP is charged FIRST, so a capped source cannot lock out the world.

        Reversing the order would let one refused attacker keep spending the
        platform-wide budget it is already barred from using.
        """
        from app.services.agent_registration_quota import GLOBAL_BUCKET, current_usage

        _set_caps(monkeypatch, per_ip=1, global_=10)
        assert _register(client)[1] == 201
        now = datetime.now(UTC)

        from app.database import SessionLocal

        db = SessionLocal()
        try:
            before = current_usage(db, bucket=GLOBAL_BUCKET, now=now)
            for _ in range(5):
                assert _register(client, agent_name="over")[1] == 429
            assert current_usage(db, bucket=GLOBAL_BUCKET, now=now) == before
        finally:
            db.close()


class TestQuotaIsAtomic:
    """Review round 2, F1 — the BLOCKER.

    Round 1 enforced the cap with ``COUNT(*) >= cap`` and a much later INSERT.
    Every request arriving while the count sat at ``cap - 1`` read ``cap - 1``,
    passed, and committed: the cap bounded a sequential attacker and nobody
    else. On a PUBLIC endpoint that mints API keys, that is the whole wall.
    """

    def test_the_reservation_is_ONE_conditional_update(self, db_session):
        """Structural, and deliberately so.

        Concurrency is hard to prove and easy to regress. This asserts the
        SHAPE that makes the reservation atomic on every engine — a single
        ``UPDATE … SET count = count + 1 WHERE … AND count < :cap`` — so a
        refactor back to SELECT-then-UPDATE fails here rather than passing
        review and failing in production under load.
        """
        from sqlalchemy import event

        from app.services.agent_registration_quota import reserve_registration_slot

        statements: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()).lower())

        bind = db_session.get_bind()
        event.listen(bind, "before_cursor_execute", _capture)
        try:
            granted = reserve_registration_slot(
                db_session, bucket="ip:203.0.113.9", cap=3, now=datetime.now(UTC)
            )
        finally:
            event.remove(bind, "before_cursor_execute", _capture)

        assert granted.granted is True

        updates = [s for s in statements if s.startswith("update agent_registration_quota")]
        assert len(updates) == 1, f"expected exactly one UPDATE, got {updates}"
        sql = updates[0]
        # Read and write in the SAME statement: the increment is relative to
        # the column's own committed value, never to one Python read earlier.
        assert re.search(r"set\s+count\s*=\s*\(?\s*(\w+\.)?count\s*\+", sql), sql
        # ...guarded by the cap in its own WHERE clause, not in Python.
        assert re.search(r"where.*(\w+\.)?count\s*<", sql), sql
        # And nothing that reintroduces the gap.
        assert "for update" not in sql, "FOR UPDATE is a no-op on sqlite — the guard belongs in the UPDATE"

    def test_a_full_bucket_grants_nothing_and_never_overshoots(self, db_session):
        from app.services.agent_registration_quota import current_usage, reserve_registration_slot

        now = datetime.now(UTC)
        bucket = "ip:198.51.100.7"
        granted = [
            reserve_registration_slot(db_session, bucket=bucket, cap=3, now=now).granted
            for _ in range(10)
        ]
        assert granted.count(True) == 3, granted
        assert current_usage(db_session, bucket=bucket, now=now) == 3

    def test_a_zero_cap_refuses_without_touching_the_database(self, db_session):
        from app.models import AgentRegistrationQuota
        from app.services.agent_registration_quota import reserve_registration_slot

        before = db_session.query(AgentRegistrationQuota).count()
        r = reserve_registration_slot(
            db_session, bucket="ip:disabled", cap=0, now=datetime.now(UTC)
        )
        assert r.granted is False
        assert db_session.query(AgentRegistrationQuota).count() == before

    def test_concurrent_reservations_never_exceed_the_cap(self, tmp_path):
        """The real proof: N threads, real commits, separate connections.

        Runs against a FILE-BACKED SQLite database rather than the suite's
        in-memory one, because the in-memory engine is shared through a
        StaticPool — every "connection" is the same connection, so nothing
        could actually contend.

        SQLite serialises write transactions against the whole database file,
        so the two conditional UPDATEs cannot interleave at all. That is a
        STRICTER guarantee than production's, not a weaker one, and it is why
        no ``FOR UPDATE`` appears anywhere in the implementation: ``FOR UPDATE``
        parses as a silent no-op here, so a lock expressed that way would be
        proven by this test and absent in fact. On Postgres the identical
        statement is atomic for its own reason — the UPDATE takes the row lock
        and the WHERE clause is re-evaluated against the committed row after
        the lock is released — which
        ``test_concurrent_reservations_never_exceed_the_cap_on_postgres`` below
        exercises when the suite is pointed at a real server.

        RED-PROOFED: re-running this harness against the round-1 shape (read the
        count, commit, sleep, then increment) grants 22 of 24 against a cap of
        3. The current implementation grants exactly 3, repeatably.
        """
        import threading

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.models import Base
        from app.services.agent_registration_quota import current_usage, reserve_registration_slot

        db_path = tmp_path / "quota_race.sqlite"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
        Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables["agent_registration_quota"]])
        SessionFactory = sessionmaker(bind=engine)

        cap = 3
        threads_n = 24
        now = datetime.now(UTC)
        bucket = "ip:203.0.113.42"
        results: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(threads_n)

        def _attempt() -> None:
            start.wait(timeout=30)
            session = SessionFactory()
            try:
                granted = reserve_registration_slot(
                    session, bucket=bucket, cap=cap, now=now
                ).granted
                session.commit()
            except Exception:  # noqa: BLE001 — a lost DB race counts as "not granted"
                session.rollback()
                granted = False
            finally:
                session.close()
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=_attempt) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert len(results) == threads_n, "a thread did not finish"
        # The security property: never MORE than the cap.
        assert results.count(True) <= cap, (
            f"{results.count(True)} of {threads_n} concurrent reservations were granted "
            f"against a cap of {cap} — the quota is not atomic"
        )
        # And the liveness half, so the assertion above cannot pass vacuously by
        # granting nothing at all.
        assert results.count(True) == cap, (
            f"only {results.count(True)} of {cap} slots were granted — the "
            "reservation is refusing callers it should have admitted"
        )
        verify = SessionFactory()
        try:
            assert current_usage(verify, bucket=bucket, now=now) == results.count(True)
        finally:
            verify.close()
            engine.dispose()

    @pytest.mark.skipif(
        not _postgres_url(),
        reason="needs a real Postgres server (CI Postgres job); SQLite proves the "
        "invariant by serialising writes, Postgres proves it by row-locking",
    )
    def test_concurrent_reservations_never_exceed_the_cap_on_postgres(self, engine_fixture):
        """The production engine's version of the test above.

        Postgres at READ COMMITTED does NOT serialise these transactions — the
        loser blocks on the winner's row lock and then re-evaluates its own
        WHERE clause against the newly committed row. That re-check is the
        whole reason the guard lives inside the UPDATE, and it is the property
        SQLite cannot exercise.
        """
        import threading

        from sqlalchemy.orm import sessionmaker

        from app.services.agent_registration_quota import current_usage, reserve_registration_slot

        SessionFactory = sessionmaker(bind=engine_fixture)
        cap = 3
        threads_n = 24
        now = datetime.now(UTC)
        bucket = f"ip:pg-{secrets.token_hex(4)}"
        results: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(threads_n)

        def _attempt() -> None:
            start.wait(timeout=30)
            session = SessionFactory()
            try:
                granted = reserve_registration_slot(
                    session, bucket=bucket, cap=cap, now=now
                ).granted
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
                granted = False
            finally:
                session.close()
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=_attempt) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert results.count(True) <= cap, (
            f"{results.count(True)} concurrent reservations granted against cap {cap}"
        )
        verify = SessionFactory()
        try:
            assert current_usage(verify, bucket=bucket, now=now) == results.count(True)
        finally:
            verify.close()

    def test_a_rolled_back_mint_releases_its_slot(self, db_session):
        """Failure release is what makes the 409 path free — it is not incidental."""
        from app.services.agent_registration_quota import current_usage, reserve_registration_slot

        now = datetime.now(UTC)
        bucket = "ip:192.0.2.55"
        assert reserve_registration_slot(db_session, bucket=bucket, cap=5, now=now).granted
        db_session.flush()
        taken = current_usage(db_session, bucket=bucket, now=now)
        assert taken == 1

        with db_session.begin_nested() as savepoint:
            assert reserve_registration_slot(db_session, bucket=bucket, cap=5, now=now).granted
            db_session.flush()
            assert current_usage(db_session, bucket=bucket, now=now) == 2
            savepoint.rollback()

        db_session.expire_all()
        assert current_usage(db_session, bucket=bucket, now=now) == 1


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


class TestTheAgentPrincipalIsDistinguishable:
    """Review round 2, F5(a) — an agent must not read as a human.

    Round 1 resolved a ``rec_agent_`` key to a ``AuthContext(scope="user")``
    that was byte-identical to a free human's. That was defended as the design
    (and the scope part IS the design — it is what keeps publishing reachable),
    but it left NOTHING for a predicate to key off: the only agent signal was
    the key prefix, a fact about the credential rather than about the actor,
    invisible everywhere past the middleware.
    """

    @pytest.fixture()
    def agent_key(self, client):
        private, pubkey = _keypair()
        return client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()["api_key"]

    def test_the_shadow_user_carries_a_durable_marker(self, client, db_session):
        private, pubkey = _keypair()
        assert client.post(REGISTER_PATH, json=_payload(private, pubkey)).status_code == 201

        from app.models import AgentIdentity, User

        identity = db_session.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).one()
        user = db_session.query(User).filter(User.id == identity.user_id).one()
        assert user.is_agent is True, "the marker must live in the DATABASE, not in a key prefix"

    def test_a_human_user_is_not_marked(self, db_session):
        """Control — the column must actually discriminate."""
        from app.models import User

        human = User(display_name="a real person")
        db_session.add(human)
        db_session.flush()
        assert human.is_agent is False

    def test_the_marker_reaches_auth_context_over_mcp(self, client, agent_key, db_session):
        from app.mcp.auth import validate_key

        ctx = validate_key(agent_key, db_session)["auth_ctx"]
        assert ctx.scope == "user", "scope stays 'user' — that is deliberate, see the module docs"
        assert ctx.is_agent is True, "MCP is the universal path; a REST-only marker is not a marker"

    def test_the_marker_reaches_auth_context_over_rest(self, app, client, agent_key):
        """Read it off a REAL request through the real middleware.

        Constructing an AuthContext by hand would prove only that the dataclass
        has the field. What matters is that ``APIKeyMiddleware`` stamps it, so
        this mounts a probe route on the live app and inspects what the
        middleware actually put on ``request.state``.
        """
        seen: list[object] = []

        @app.get("/_test/agentreg-whoami")
        def _whoami(request: Request) -> dict:
            seen.append(getattr(request.state, "auth_ctx", None))
            return {"ok": True}

        r = client.get("/_test/agentreg-whoami", headers={"x-api-key": agent_key})
        assert r.status_code == 200, r.text
        ctx = seen[-1]
        assert getattr(ctx, "scope", None) == "user"
        assert getattr(ctx, "is_agent", None) is True

        human_probe = client.get("/_test/agentreg-whoami", headers={"x-api-key": _MASTER_KEY()})
        assert human_probe.status_code == 200
        assert getattr(seen[-1], "is_agent", False) is False, "control: master is not an agent"

    def test_a_human_key_is_not_marked_on_either_path(self, db_session):
        from app.mcp.auth import validate_key
        from app.models import APIKey, User

        human = User(display_name="a real person")
        db_session.add(human)
        db_session.flush()
        plaintext = "rec_live_" + secrets.token_urlsafe(24)
        db_session.add(
            APIKey(
                user_id=human.id,
                key_prefix=plaintext[:12],
                key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
                name="human",
                is_active=True,
            )
        )
        db_session.flush()

        ctx = validate_key(plaintext, db_session)["auth_ctx"]
        assert ctx.scope == "user"
        assert ctx.is_agent is False, "a human key must never be marked as an agent"

    def test_authz_denies_the_sandbox_to_an_agent_even_with_the_operator_flag(self):
        """F5(b). The denial is AUTHORIZATION, not configuration.

        Round 1's argument was "the mint sets is_sandbox_operator=False". That
        is a property of one row at one moment; any future path that flips it
        (admin tool, support script, backfill, bug) would have handed arbitrary
        code execution to a principal nobody vouched for. So the predicate
        refuses an agent regardless of the flag.
        """
        from app.auth_ctx import AuthContext
        from app.authz import can_run_sandbox

        from uuid import uuid4

        agent = AuthContext(scope="user", user_id=uuid4(), is_agent=True, is_sandbox_operator=True)
        assert can_run_sandbox(agent) is False

        human = AuthContext(scope="user", user_id=uuid4(), is_sandbox_operator=True)
        assert can_run_sandbox(human) is True, "control: the flag still works for a human"

    def test_an_agent_that_somehow_reached_master_scope_still_cannot_run_the_sandbox(self):
        """The clause is FIRST for a reason — the safe answer wins on any overlap."""
        from app.auth_ctx import AuthContext
        from app.authz import can_run_sandbox

        assert can_run_sandbox(AuthContext(scope="master", is_agent=True)) is False
        assert can_run_sandbox(AuthContext(scope="master")) is True


class TestAgentKeyIsFenced:
    """Review round 2, F5(c) — a fence test that accepts 404 proves NOTHING.

    Round 1 asserted ``status in (401, 403, 404)``. 404 is what an unregistered
    route returns, so those assertions would have passed identically against an
    app where the route simply did not exist — i.e. they demonstrated no
    boundary at all. Every test here now does two things: it establishes a
    CONTROL (a non-agent credential reaches the route, so the path is real and
    mounted), and only then asserts a SPECIFIC denial for the agent.
    """

    @pytest.fixture()
    def agent_key(self, client):
        private, pubkey = _keypair()
        return client.post(REGISTER_PATH, json=_payload(private, pubkey)).json()["api_key"]

    @pytest.fixture()
    def master_key(self):
        from app.config import settings

        return settings.API_KEY

    def test_cannot_start_checkout(self, client, agent_key, master_key):
        """Agents must not create Stripe sessions — 403, explicitly."""
        control = client.post("/api/checkout/pro", headers={"x-api-key": master_key})
        assert control.status_code != 404, "control: the checkout route must exist and be reachable"

        r = client.post("/api/checkout/pro", headers={"x-api-key": agent_key})
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == "agent_principals_cannot_transact"

    def test_cannot_open_a_billing_portal_session(self, client, agent_key, master_key):
        control = client.post("/api/billing/portal-session", headers={"x-api-key": master_key})
        assert control.status_code != 404, "control: the portal route must exist"

        r = client.post("/api/billing/portal-session", headers={"x-api-key": agent_key})
        assert r.status_code == 403, r.text

    def test_cannot_downgrade_a_subscription(self, client, agent_key, master_key):
        control = client.post("/api/subscriptions/downgrade", headers={"x-api-key": master_key})
        assert control.status_code != 404, "control: the downgrade route must exist"

        r = client.post("/api/subscriptions/downgrade", headers={"x-api-key": agent_key})
        assert r.status_code == 403, r.text

    def test_cannot_read_billing(self, client, agent_key, master_key):
        """A READ, so 401 (no session) is the honest answer — but never 404."""
        control = client.get("/api/billing/me", headers={"x-api-key": master_key})
        assert control.status_code != 404, "control: the billing route must exist"

        r = client.get("/api/billing/me", headers={"x-api-key": agent_key})
        assert r.status_code in (401, 402, 403), r.text

    def test_cannot_hit_admin(self, client, agent_key, master_key):
        control = client.post("/api/admin/reindex-all", headers={"x-api-key": master_key})
        assert control.status_code != 404, "control: the admin route must exist"
        assert control.status_code != 403, "control: the master key must NOT be refused"

        r = client.post("/api/admin/reindex-all", headers={"x-api-key": agent_key})
        assert r.status_code == 403, r.text

    def test_cannot_list_agent_identities(self, client, agent_key, master_key):
        control = client.get("/api/admin/agent-identities", headers={"x-api-key": master_key})
        assert control.status_code == 200, control.text

        r = client.get("/api/admin/agent-identities", headers={"x-api-key": agent_key})
        assert r.status_code == 403, r.text

    def test_cannot_mint_more_keys_via_the_human_route(self, client, agent_key):
        """POST /api/api-keys is JWT-only; an agent has no session to present."""
        r = client.post("/api/api-keys", headers={"x-api-key": agent_key}, json={})
        assert r.status_code == 401, r.text

    def test_cannot_run_the_sandbox_on_the_real_route(self, client, agent_key, master_key, db_session):
        """Hit POST /api/skills/{slug}/sandbox/run itself, not just the predicate.

        The predicate test above proves the rule; this proves the rule is WIRED
        to the route an attacker would actually call.
        """
        from app.models import Skill

        slug = f"sandbox-fence-{secrets.token_hex(4)}"
        db_session.add(Skill(slug=slug, title="fence", description="fence", is_public=True))
        db_session.flush()

        body = {"entrypoint": "setup.sh"}
        control = client.post(
            f"/api/skills/{slug}/sandbox/run", headers={"x-api-key": master_key}, json=body
        )
        assert control.status_code != 403, (
            "control: a sandbox-authorized caller must get PAST the authz gate "
            f"(got {control.status_code}: {control.text})"
        )

        r = client.post(
            f"/api/skills/{slug}/sandbox/run", headers={"x-api-key": agent_key}, json=body
        )
        assert r.status_code == 403, r.text
        assert "sandbox" in r.text.lower()


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

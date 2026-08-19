"""agentreg_0819 — Ed25519 proof-of-key agent enrolment.

The whole point: every API key mint used to require a human OAuth login
(``app/api_key_routes.py:_require_user``), so an autonomous agent that
discovered LoopSkill through ``llms.txt`` / an MCP directory could look but
never enrol, publish, or file feedback. This module is the seam that removes
that wall without removing any of the protections around it.

THE CANONICAL STRING — the single normative definition
------------------------------------------------------
The client signs, with its Ed25519 private key, the UTF-8 bytes of::

    loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}

where the five fields are the request's OWN field values, verbatim, in that
order, joined by ``:``. ``pubkey`` is standard base64 of the 32 raw public-key
bytes; ``timestamp`` is ISO-8601 UTC; ``nonce`` is hex, >= 16 bytes;
``agent_name`` is the display name (<= 64 chars).

This exact string is repeated in three places that MUST agree — this module,
``POST /api/agents/register``'s docstring, and ``/.well-known/agent.json`` —
and ``tests/test_agentreg_0819_agent_self_registration.py`` pins all three
against each other so a future edit cannot silently break every client.

WHY THE SIGNATURE COVERS ALL FIVE FIELDS: dropping any one of them makes a
captured signature reusable for a different claim. Without ``pubkey`` a relayer
could re-attribute the enrolment to its own key; without ``nonce`` +
``timestamp`` the payload replays forever; without ``agent_name`` the display
name is attacker-malleable after the fact. ``contact`` is deliberately NOT
signed — it carries no authorization weight, and signing it would force a
client to re-sign to fix a typo in an email address.

THE THREE WALLS
---------------
1. **Replay** — the nonce is hashed and inserted under a UNIQUE constraint, so
   two concurrent replays race at the DATABASE and exactly one wins.
   Deliberately not Redis: ``app.middleware.get_redis`` degrades to ``None``
   whenever Redis is unreachable (see its 30s-backoff fallback), and a replay
   wall that opens when the cache is down is not a wall.
2. **Clock skew** — ``timestamp`` must be within
   ``settings.AGENT_REGISTRATION_MAX_SKEW_SECONDS`` of server time, which also
   bounds how long a consumed nonce must be retained.
3. **Volume** — per-IP and global rolling-24h enrolment caps
   (``settings.AGENT_REGISTRATION_*``).

WHY A SHADOW ``User`` ROW (and not keys hanging off ``agent_identities``)
------------------------------------------------------------------------
``APIKey.user_id`` is NOT NULL and every ownership column in the schema
(``Bundle.bundle_owner``, ``Skill.skill_owner``, feedback, install events) keys
off a user UUID. Making ``api_keys.user_id`` nullable would put a NULL through
``AuthContext.user_id`` — and this codebase's master-key sentinel is literally
``is_master = (api_key_user_id is None)`` (see the cbt_ branch's SECURITY note
in ``app/middleware/api_key.py``). An agent key that reads as master is the
worst possible failure mode, and it would be one missing ``is not None`` away.

With a shadow user, an agent key resolves to the SAME
``AuthContext(scope="user", tier=None)`` a free human key produces, so
``app/authz.py`` and ``app/auth_ctx.py`` are untouched by this feature. The
shadow row carries no ``github_id`` / ``google_id`` / ``email``, so no OAuth
flow can ever land on it, and no password or session exists to steal.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.middleware.key_prefixes import AGENT_KEY_PREFIX
from app.models import AgentIdentity, AgentRegistrationNonce, APIKey, User

logger = logging.getLogger(__name__)

# The canonical-string namespace + version. Bumping the version is how a future
# field addition stays unambiguous — an old client's v1 string simply will not
# verify against a v2 server, instead of silently signing less than it thinks.
CANONICAL_PREFIX = "loopskill-agent-register"
CANONICAL_VERSION = "v1"

ED25519_RAW_PUBKEY_BYTES = 32
MIN_NONCE_BYTES = 16
KEY_BODY_LEN = 32  # urlsafe chars after the rec_agent_ prefix

# One active key per agent identity. Matches the FREE human tier's cap. There is
# structurally no second mint: registration is the only path that issues a
# rec_agent_ key, and a second registration of the same pubkey is a 409.
MAX_ACTIVE_KEYS_PER_IDENTITY = 1


class AgentRegistrationError(Exception):
    """A registration attempt that must be refused, with its HTTP shape.

    Carries the status code and a stable machine-readable ``code`` so the route
    layer stays a thin translator and every refusal is documented in one place.
    """

    def __init__(self, status_code: int, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class RegistrationResult:
    """What a successful enrolment produced. The plaintext key exists ONCE."""

    identity_id: UUID
    user_id: UUID
    api_key_id: UUID
    plaintext_key: str
    key_prefix: str
    agent_name: str


# ── The canonical string ────────────────────────────────────────────────────


def canonical_registration_string(
    *,
    pubkey: str,
    timestamp: str,
    nonce: str,
    agent_name: str,
) -> str:
    """Build the exact string a registering agent must sign.

    ``loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}``

    Field values are used VERBATIM — no normalisation, no re-serialisation of
    the timestamp. The server must verify the bytes the client actually sent,
    otherwise a server-side canonicalisation quirk (say, rewriting ``+00:00``
    to ``Z``) becomes an interop bug no client can debug.
    """
    parts = [
        CANONICAL_PREFIX,
        CANONICAL_VERSION,
        pubkey,
        timestamp,
        nonce,
        agent_name,
    ]
    return ":".join(parts)


# ── Field validation ────────────────────────────────────────────────────────


def _decode_pubkey(pubkey_b64: str) -> Ed25519PublicKey:
    """Decode a base64 raw Ed25519 public key, or raise a 400."""
    try:
        raw = base64.b64decode(pubkey_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AgentRegistrationError(
            400,
            "invalid_pubkey",
            "pubkey must be standard base64",
        ) from exc
    if len(raw) != ED25519_RAW_PUBKEY_BYTES:
        raise AgentRegistrationError(
            400,
            "invalid_pubkey",
            f"pubkey must decode to {ED25519_RAW_PUBKEY_BYTES} raw bytes (got {len(raw)}) — "
            "send the RAW key, not a PEM/DER wrapper",
        )
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise AgentRegistrationError(
            400,
            "invalid_pubkey",
            "pubkey is not a valid Ed25519 public key",
        ) from exc


def _validate_nonce(nonce: str) -> None:
    """Reject a nonce that is not at least 16 bytes of hex.

    Hex-and-length is enforced rather than merely "some string" so a client
    cannot pass a constant like ``"nonce"`` and believe it is protected; the
    replay wall only means something if the value is actually unpredictable.
    """
    try:
        raw = bytes.fromhex(nonce)
    except ValueError as exc:
        raise AgentRegistrationError(
            400,
            "invalid_nonce",
            "nonce must be hex-encoded",
        ) from exc
    if len(raw) < MIN_NONCE_BYTES:
        raise AgentRegistrationError(
            400,
            "invalid_nonce",
            f"nonce must be at least {MIN_NONCE_BYTES} bytes ({MIN_NONCE_BYTES * 2} hex chars)",
        )


def _validate_timestamp(timestamp: str, *, now: datetime) -> None:
    """Parse an ISO-8601 UTC timestamp and enforce the skew window.

    A naive (offset-less) value is read as UTC — the field is documented as
    UTC, and treating it as server-local would make the skew window depend on
    the deployment's timezone.
    """
    try:
        parsed = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentRegistrationError(
            400,
            "invalid_timestamp",
            "timestamp must be ISO-8601 UTC",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    skew = abs((parsed - now).total_seconds())
    max_skew = settings.AGENT_REGISTRATION_MAX_SKEW_SECONDS
    if skew > max_skew:
        raise AgentRegistrationError(
            401,
            "timestamp_out_of_range",
            f"timestamp is {int(skew)}s from server time; the accepted window is +/-{max_skew}s",
        )


def verify_registration_signature(
    *,
    pubkey: str,
    timestamp: str,
    nonce: str,
    agent_name: str,
    signature: str,
) -> None:
    """Verify the Ed25519 signature over the canonical string, or raise 401.

    A signature that does not verify and a signature that is not even valid
    base64 return the SAME ``invalid_signature`` code on purpose: distinguishing
    them tells an attacker which half of their forgery attempt was wrong.
    """
    public_key = _decode_pubkey(pubkey)
    message = canonical_registration_string(
        pubkey=pubkey,
        timestamp=timestamp,
        nonce=nonce,
        agent_name=agent_name,
    ).encode("utf-8")
    try:
        raw_sig = base64.b64decode(signature, validate=True)
        public_key.verify(raw_sig, message)
    except (InvalidSignature, binascii.Error, ValueError) as exc:
        raise AgentRegistrationError(
            401,
            "invalid_signature",
            "signature did not verify against pubkey",
        ) from exc


# ── The three walls ─────────────────────────────────────────────────────────


def sweep_expired_nonces(db: Session, *, now: datetime) -> int:
    """Delete consumed nonces past their retention horizon. Returns the count.

    Opportunistic cleanup on the registration path rather than a cron: the table
    only grows on SUCCESSFUL proof-of-key, which the enrolment caps already
    bound, so there is never enough of it to justify a scheduled job. Dropping
    an expired row is safe because a replay carrying it is already refused by
    the timestamp gate.
    """
    deleted = (
        db.query(AgentRegistrationNonce)
        .filter(AgentRegistrationNonce.expires_at < now)
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def consume_nonce(db: Session, nonce: str, *, now: datetime) -> None:
    """Burn a nonce exactly once, or raise 401 ``nonce_replayed``.

    Two layers, because a check-then-act alone is not a replay wall:

    * the lookup answers the ordinary sequential replay cleanly;
    * the UNIQUE constraint on ``nonce_hash`` is what actually makes it atomic —
      two CONCURRENT replays of one captured payload both pass the lookup and
      race at the database, where exactly one wins. That window is precisely
      what a replay attacker aims for, so the constraint is the real guarantee
      and the lookup is only the fast path.

    The insert runs inside a SAVEPOINT so a lost race rolls back only itself and
    leaves the caller's transaction intact.
    """
    nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    replayed = AgentRegistrationError(
        401,
        "nonce_replayed",
        "this nonce has already been used — sign a fresh registration",
    )
    seen = db.query(AgentRegistrationNonce).filter_by(nonce_hash=nonce_hash).first()
    if seen is not None:
        raise replayed

    horizon = timedelta(seconds=settings.AGENT_REGISTRATION_MAX_SKEW_SECONDS * 2)
    row = AgentRegistrationNonce(
        nonce_hash=nonce_hash,
        expires_at=now + horizon,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise replayed from exc


def enforce_registration_quota(db: Session, *, client_ip: str | None, now: datetime) -> None:
    """Enforce the per-IP and global rolling-24h enrolment caps, or raise 429.

    Counts SUCCESSFUL enrolments (``agent_identities`` rows), which is what the
    caps are about — a forged or malformed attempt must not let an attacker
    exhaust a legitimate agent's allowance. Failed attempts are separately
    bounded by ``RateLimitMiddleware``'s anonymous per-IP minute bucket, which
    this endpoint sits behind precisely because it authenticates nothing.
    """
    window_start = now - timedelta(days=1)

    global_cap = settings.AGENT_REGISTRATION_GLOBAL_PER_DAY
    global_count = db.query(AgentIdentity).filter(AgentIdentity.created_at >= window_start).count()
    if global_count >= global_cap:
        raise AgentRegistrationError(
            429,
            "global_registration_limit",
            f"the platform-wide cap of {global_cap} agent registrations per 24h is reached",
        )

    if not client_ip:
        return
    ip_cap = settings.AGENT_REGISTRATION_PER_IP_PER_DAY
    ip_count = (
        db.query(AgentIdentity)
        .filter(
            AgentIdentity.registration_ip == client_ip,
            AgentIdentity.created_at >= window_start,
        )
        .count()
    )
    if ip_count >= ip_cap:
        raise AgentRegistrationError(
            429,
            "ip_registration_limit",
            f"this source has already registered {ip_count} agents in the last 24h (cap {ip_cap})",
        )


def assert_pubkey_unregistered(db: Session, pubkey: str) -> None:
    """Raise 409 if this pubkey is already enrolled.

    The secret is NEVER re-issued. Re-presenting a known pubkey is either an
    agent that lost its key (a rotation, which is an authenticated operation)
    or someone replaying a captured pubkey to harvest a fresh secret. The
    response points at rotation and leaks nothing beyond "known".
    """
    existing = db.query(AgentIdentity).filter(AgentIdentity.pubkey == pubkey).first()
    if existing is not None:
        raise AgentRegistrationError(
            409,
            "pubkey_already_registered",
            "this pubkey is already registered; registration never re-issues a secret",
            agent_identity_id=str(existing.id),
            rotation=(
                "Rotate instead: revoke the current key with DELETE /api/api-keys/{key_id} "
                "using that key, then register a NEW keypair. A lost private key cannot be "
                "recovered — ask an administrator to revoke the identity."
            ),
        )


# ── Minting ─────────────────────────────────────────────────────────────────


def _generate_agent_key() -> tuple[str, str, str]:
    """Generate a ``rec_agent_`` key. Returns (plaintext, prefix12, sha256).

    Mirrors ``app.api_key_routes._generate_key`` field for field (same entropy,
    same 12-char stored prefix, same sha256-at-rest) so an agent key is
    indistinguishable from a human key to every consumer except the revocation
    gate, which keys off the prefix.
    """
    body = secrets.token_urlsafe(KEY_BODY_LEN)
    plaintext = f"{AGENT_KEY_PREFIX}{body}"
    prefix12 = plaintext[:12]
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, prefix12, key_hash


def register_agent(
    db: Session,
    *,
    pubkey: str,
    timestamp: str,
    nonce: str,
    agent_name: str,
    signature: str,
    contact: str | None = None,
    client_ip: str | None = None,
    now: datetime | None = None,
) -> RegistrationResult:
    """Run the full enrolment and return the once-only plaintext key.

    Order is security-significant and deliberate:

    1. field shape (``nonce``, ``timestamp``) — cheap, and the skew window
       bounds the nonce retention horizon;
    2. signature — proves possession BEFORE any row is written, so an
       unauthenticated caller can never make the server persist anything;
    3. quota — refuse an over-cap enrolment before burning the nonce;
    4. nonce — burn exactly once;
    5. duplicate pubkey — 409 with a pointer to rotation;
    6. mint.

    Because (4) precedes (5), a verbatim replay of a successful registration
    answers ``nonce_replayed`` (an attack) while a genuine re-registration with
    a fresh nonce answers ``pubkey_already_registered`` (an honest client mistake).
    Those are different situations and they get different answers.
    """
    now = now or datetime.now(UTC)

    _validate_nonce(nonce)
    _validate_timestamp(timestamp, now=now)
    verify_registration_signature(
        pubkey=pubkey,
        timestamp=timestamp,
        nonce=nonce,
        agent_name=agent_name,
        signature=signature,
    )

    sweep_expired_nonces(db, now=now)
    enforce_registration_quota(db, client_ip=client_ip, now=now)
    consume_nonce(db, nonce, now=now)
    assert_pubkey_unregistered(db, pubkey)

    # The shadow user. No github_id / google_id / email — unreachable from any
    # OAuth flow, no session to hijack, no password to reset. The subscription
    # columns stay NULL, so revenue_truth.entitled_tier() answers None => free
    # tier, with ZERO pricing or tier code touched.
    shadow_user = User(display_name=f"agent:{agent_name}"[:255])
    db.add(shadow_user)
    db.flush()

    identity = AgentIdentity(
        pubkey=pubkey,
        agent_name=agent_name,
        contact=contact,
        user_id=shadow_user.id,
        revoked=False,
        registration_ip=client_ip,
    )
    db.add(identity)

    plaintext, prefix12, key_hash = _generate_agent_key()
    api_key = APIKey(
        user_id=shadow_user.id,
        key_prefix=prefix12,
        key_hash=key_hash,
        name=f"agent:{agent_name}"[:255],
        label=f"agent:{agent_name}"[:100],
        bundle_id=None,
        is_active=True,
        # authz.can_run_sandbox(ctx) is False unless this flag is set or the
        # caller is master scope. Stated explicitly rather than left to the
        # column default so nobody has to open models.py to learn that a
        # self-registered agent cannot execute code.
        is_sandbox_operator=False,
    )
    db.add(api_key)

    try:
        db.commit()
    except IntegrityError as exc:
        # The pubkey UNIQUE constraint has the last word: two concurrent
        # registrations of one pubkey both pass assert_pubkey_unregistered and
        # exactly one survives the commit. Answer the loser identically to the
        # sequential case.
        db.rollback()
        raise AgentRegistrationError(
            409,
            "pubkey_already_registered",
            "this pubkey is already registered; registration never re-issues a secret",
        ) from exc

    db.refresh(identity)
    db.refresh(api_key)

    logger.info(
        "agent registered: identity=%s user=%s key=%s name=%r ip=%s",
        identity.id,
        shadow_user.id,
        api_key.id,
        agent_name,
        client_ip,
    )

    return RegistrationResult(
        identity_id=identity.id,
        user_id=shadow_user.id,
        api_key_id=api_key.id,
        plaintext_key=plaintext,
        key_prefix=prefix12,
        agent_name=agent_name,
    )


def revoke_identity(db: Session, identity_id: UUID) -> AgentIdentity | None:
    """Revoke an agent identity and deactivate its keys. Idempotent.

    Two mechanisms, on purpose. ``revoked=True`` is the authoritative one — the
    middleware gate reads it, so it also covers a key minted for this identity
    in a race with this call. Deactivating the ``api_keys`` rows additionally
    makes the revocation visible to every ordinary ``is_active`` filter in the
    codebase, so a future code path that forgets the identity gate still fails.
    """
    identity = db.query(AgentIdentity).filter(AgentIdentity.id == identity_id).first()
    if identity is None:
        return None
    identity.revoked = True
    db.query(APIKey).filter(
        APIKey.user_id == identity.user_id,
        APIKey.is_active == True,  # noqa: E712
    ).update({"is_active": False}, synchronize_session=False)
    db.commit()
    db.refresh(identity)
    logger.info("agent identity revoked: identity=%s user=%s", identity.id, identity.user_id)
    return identity

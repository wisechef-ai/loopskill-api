"""agentreg_0819 — Ed25519 proof-of-key agent enrolment.

The whole point: every API key mint used to require a human OAuth login
(``app/api_key_routes.py:_require_user``), so an autonomous agent that
discovered LoopSkill through ``llms.txt`` / an MCP directory could look but
never enrol, publish, or file feedback. This module is the seam that removes
that wall without removing any of the protections around it.

WHERE THE PIECES LIVE
---------------------
This module is the PERSISTENCE half: nonce burning, quota reservation, minting
and revocation. The pure proof-of-key half — the canonical string, field
canonicality, signature verification — lives in
``app.services.agent_registration_proof`` and the atomic enrolment counter in
``app.services.agent_registration_quota``. Both are re-exported here, so
``from app.services.agent_registration import canonical_registration_string``
still resolves; the split exists because the round-2 fixes pushed this file
past the never-waived 600-line cap, and "pure verification" versus "writes
rows" is the seam that was already there.

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
3. **Volume** — per-IP and platform-wide daily enrolment caps
   (``settings.AGENT_REGISTRATION_*``), reserved ATOMICALLY in
   ``app.services.agent_registration_quota`` — the round-1 unlocked ``COUNT(*)``
   was a check-then-act that every concurrent request passed at ``cap - 1``.

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

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.middleware.key_prefixes import AGENT_KEY_PREFIX
from app.models import AgentIdentity, AgentRegistrationNonce, APIKey, User

# Re-exported so this module stays the single import surface for the feature —
# every caller (routes, .well-known, tests) imports from here, and the round-2
# split into a pure proof module is invisible to them.
from app.services.agent_registration_proof import (  # noqa: F401
    CANONICAL_PREFIX,
    CANONICAL_VERSION,
    ED25519_RAW_PUBKEY_BYTES,
    MAX_NONCE_BYTES,
    MIN_NONCE_BYTES,
    NONCE_RE,
    AgentRegistrationError,
    _validate_nonce,
    _validate_timestamp,
    canonical_registration_string,
    decode_pubkey_raw,
    pubkey_fingerprint,
    verify_registration_signature,
)
from app.services.agent_registration_quota import (
    GLOBAL_SCOPE,
    gate_scope_for_ip,
    reserve_registration_slot,
    seconds_until_capacity,
)

logger = logging.getLogger(__name__)

KEY_BODY_LEN = 32  # urlsafe chars after the rec_agent_ prefix

# One active key per agent identity. Matches the FREE human tier's cap. There is
# structurally no second mint: registration is the only path that issues a
# rec_agent_ key, and a second registration of the same pubkey is a 409.
MAX_ACTIVE_KEYS_PER_IDENTITY = 1


@dataclass(frozen=True)
class RegistrationResult:
    """What a successful enrolment produced. The plaintext key exists ONCE."""

    identity_id: UUID
    user_id: UUID
    api_key_id: UUID
    plaintext_key: str
    key_prefix: str
    agent_name: str


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


# Seconds a caller is told to wait after a cap refusal. One hour, not the
# remaining window: the exact reset instant is a free oracle on how much of the
# platform-wide budget an attacker has already consumed.


# The stable log event key for the platform-wide cap. Grep/alert on this string
# — it means enrolment is CLOSED for everyone until the window rolls, and the maintainer
# maintainer has to decide whether that is an attack or genuine demand.
GLOBAL_CAP_EVENT = "agent_registration_global_cap"


def enforce_registration_quota(db: Session, *, client_ip: str | None, now: datetime) -> None:
    """Reserve one enrolment slot atomically, or raise 429. Order: IP, then global.

    Reservations, not counts. See
    ``app.services.agent_registration_quota.reserve_registration_slot`` for why
    the round-1 ``COUNT(*)``-then-compare could not bound a concurrent caller.

    The per-IP bucket is charged FIRST and the platform-wide bucket second, so
    a source that is already over its own cap is refused without spending a
    slot from everyone else's budget — otherwise one blocked attacker could
    still drain the global counter and lock the platform out.

    The two caps are deliberately different KINDS of wall:

    * **per-IP** is an ordinary abuse limit. It refuses one source and nobody
      else notices.
    * **platform-wide** is a CIRCUIT BREAKER. Tripping it stops enrolment for
      every agent on earth, so it is treated as an incident: a ``logger.warning``
      carrying the stable ``agent_registration_global_cap`` event key fires on
      every refusal, and the cap is env-raiseable
      (``WR_AGENT_REGISTRATION_GLOBAL_PER_DAY``) without a redeploy. That
      combination — loud, and liftable in seconds — is what makes a global cap
      an acceptable trade rather than a self-inflicted outage; see the route
      docstring for the full reasoning.

    Both refusals carry ``retry_after`` so the route can set ``Retry-After``.
    """
    ip_cap = settings.AGENT_REGISTRATION_PER_IP_PER_DAY
    if client_ip:
        ip_scope = gate_scope_for_ip(client_ip)
        if not reserve_registration_slot(db, scope=ip_scope, cap=ip_cap, now=now):
            raise AgentRegistrationError(
                429,
                "ip_registration_limit",
                f"this source has reached its cap of {ip_cap} agent registrations per day",
                retry_after=seconds_until_capacity(db, scope=ip_scope, now=now),
            )

    global_cap = settings.AGENT_REGISTRATION_GLOBAL_PER_DAY
    global_scope = GLOBAL_SCOPE
    if not reserve_registration_slot(db, scope=global_scope, cap=global_cap, now=now):
        # Structured and stable: this is the line an alert rule keys off. If it
        # fires, self-registration is DOWN platform-wide until the UTC day rolls
        # or someone raises WR_AGENT_REGISTRATION_GLOBAL_PER_DAY.
        logger.warning(
            "%s: platform-wide agent-registration cap reached — enrolment is CLOSED "
            "for all sources until the UTC day rolls. cap=%d ip=%s. Raise "
            "WR_AGENT_REGISTRATION_GLOBAL_PER_DAY to reopen without a redeploy.",
            GLOBAL_CAP_EVENT,
            global_cap,
            client_ip,
            extra={
                "event": GLOBAL_CAP_EVENT,
                "cap": global_cap,
                "client_ip": client_ip,
            },
        )
        raise AgentRegistrationError(
            429,
            "global_registration_limit",
            f"the platform-wide cap of {global_cap} agent registrations per day is reached",
            retry_after=seconds_until_capacity(db, scope=global_scope, now=now),
        )


def assert_pubkey_unregistered(db: Session, pubkey: str) -> None:
    """Raise 409 if this pubkey is already enrolled.

    The secret is NEVER re-issued. Re-presenting a known pubkey is either an
    agent that lost its key (a rotation, which is an authenticated operation)
    or someone replaying a captured pubkey to harvest a fresh secret. The
    response points at rotation and leaks nothing beyond "known".

    Matched on the RAW-BYTES fingerprint, never on the base64 text: several
    base64 spellings decode to one key (see :func:`decode_pubkey_raw`), and a
    text match would have answered "unregistered" for a re-spelling of a key
    that is already enrolled — turning the key-stuffing wall into a formality.

    Called BEFORE the quota reservation so an honest client's duplicate
    registration (409) never consumes a slot from its own daily allowance.
    """
    existing = (
        db.query(AgentIdentity).filter(AgentIdentity.pubkey_sha256 == pubkey_fingerprint(pubkey)).first()
    )
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
    3. nonce — burn exactly once;
    4. duplicate pubkey — 409 with a pointer to rotation;
    5. quota — reserve a slot atomically, LAST of the refusals;
    6. mint.

    Because (3) precedes (4), a verbatim replay of a successful registration
    answers ``nonce_replayed`` (an attack) while a genuine re-registration with
    a fresh nonce answers ``pubkey_already_registered`` (an honest client mistake).
    Those are different situations and they get different answers.

    Because (4) precedes (5), a 409 costs the caller NO quota. Round 1 reserved
    before the duplicate check, so an agent that retried its own registration —
    the single most likely honest client bug — burned its daily allowance
    answering 409s. And because (5) is the last gate before the mint, a slot is
    only ever reserved for an attempt that is otherwise ready to succeed; if the
    mint then loses the pubkey UNIQUE race, the rollback releases the slot with
    it.
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
    consume_nonce(db, nonce, now=now)
    assert_pubkey_unregistered(db, pubkey)
    enforce_registration_quota(db, client_ip=client_ip, now=now)

    # The shadow user. No github_id / google_id / email — unreachable from any
    # OAuth flow, no session to hijack, no password to reset. The subscription
    # columns stay NULL, so revenue_truth.entitled_tier() answers None => free
    # tier, with ZERO pricing or tier code touched.
    #
    # is_agent=True is the DURABLE marker (review round 2, F5). Without it the
    # only thing distinguishing an agent principal from a human was the key
    # prefix — a fact about the credential, not about the actor, and therefore
    # invisible to app/authz.py. It is stamped here, at the single mint site.
    shadow_user = User(display_name=f"agent:{agent_name}"[:255], is_agent=True)
    db.add(shadow_user)
    db.flush()

    identity = AgentIdentity(
        pubkey=pubkey,
        pubkey_sha256=pubkey_fingerprint(pubkey),
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
        # The pubkey_sha256 UNIQUE constraint has the last word: two concurrent
        # registrations of one pubkey both pass assert_pubkey_unregistered and
        # exactly one survives the commit. Answer the loser identically to the
        # sequential case. The rollback also releases the quota slot this
        # attempt reserved — a 409 must not cost anyone their allowance.
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

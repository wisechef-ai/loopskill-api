"""agentreg_0819 — the PROOF-OF-KEY layer: canonical string, fields, signature.

Split out of ``app.services.agent_registration`` in review round 2. That module
crossed the NEVER-waived 600-line god-object cap
(``tests/test_w0_2_pyfile_size_discipline.py``) once the round-2 fixes landed,
and this is the seam that was already there: everything here is PURE — no
Session, no settings beyond the skew window, no writes. It answers one
question, "is this request a genuine signed claim by the holder of this key",
and it can be tested without a database.

The persistence half — nonce burning, quota reservation, minting, revocation —
stays in ``app.services.agent_registration``, which re-exports these names so
every existing import keeps working.

THE CANONICAL STRING — the single normative definition
------------------------------------------------------
The client signs, with its Ed25519 private key, the UTF-8 bytes of::

    loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}

where the five fields are the request's OWN field values, verbatim, in that
order, joined by ``:``. This exact string is repeated in three places that MUST
agree — this module, ``POST /api/agents/register``'s docstring, and
``/.well-known/agent.json`` — and
``tests/test_agentreg_0819_agent_self_registration.py`` pins all three against
each other so a future edit cannot silently break every client.

WHY THE SIGNATURE COVERS ALL FIVE FIELDS: dropping any one of them makes a
captured signature reusable for a different claim. Without ``pubkey`` a relayer
could re-attribute the enrolment to its own key; without ``nonce`` +
``timestamp`` the payload replays forever; without ``agent_name`` the display
name is attacker-malleable after the fact. ``contact`` is deliberately NOT
signed — it carries no authorization weight, and signing it would force a
client to re-sign to fix a typo in an email address.

CANONICALITY IS PART OF THE PROOF (review round 2, F3/F7). Both encoded fields
are required to be in their ONE canonical spelling — a padded, zero-pad-bit
base64 pubkey and a lowercase even-length hex nonce. Neither
``base64.b64decode(validate=True)`` nor ``bytes.fromhex`` enforces that, and
the alternatives are not cosmetic: they are additional wire forms of one value,
which is precisely what a uniqueness constraint or a replay wall keyed on the
TEXT will fail to recognise.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import settings

# The canonical-string namespace + version. Bumping the version is how a future
# field addition stays unambiguous — an old client's v1 string simply will not
# verify against a v2 server, instead of silently signing less than it thinks.
CANONICAL_PREFIX = "loopskill-agent-register"
CANONICAL_VERSION = "v1"

ED25519_RAW_PUBKEY_BYTES = 32
MIN_NONCE_BYTES = 16
MAX_NONCE_BYTES = 64

# Full-match, lowercase, even length by construction (pairs of hex digits).
# ``bytes.fromhex`` is far more permissive than the documented format: it
# accepts uppercase, ASCII whitespace *inside* the string, and unbounded
# length. None of those are the nonce the contract describes, and each is a
# distinct spelling of one value — i.e. a way to present "the same" nonce in a
# form the replay wall hashes differently. Anchored regex first, decode after.
# Spelled as whole BYTES rather than as a char count so odd lengths are
# rejected by the pattern itself instead of by a decode error further down.
NONCE_RE = re.compile(rf"\A(?:[0-9a-f]{{2}}){{{MIN_NONCE_BYTES},{MAX_NONCE_BYTES}}}\Z")


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


def decode_pubkey_raw(pubkey_b64: str) -> bytes:
    """Decode a CANONICALLY-spelled base64 raw Ed25519 public key, or raise 400.

    Canonicality is the load-bearing part, and it is not what
    ``b64decode(validate=True)`` gives you. That flag rejects characters outside
    the alphabet; it does NOT reject a valid-alphabet string whose trailing
    pad bits are non-zero. 32 bytes encode to 44 characters, of which the last
    significant character carries 4 real bits and 2 slack bits — so
    ``…9E=``, ``…9F=``, ``…9G=`` and ``…9H=`` are four DIFFERENT strings that
    decode to the same 32 bytes, and every one of them decodes cleanly.

    Uniqueness downstream keys off the decoded bytes' hash, so this alone would
    already be closed. It is still rejected here, one layer earlier, because a
    non-canonical spelling is also inside the SIGNED canonical string: allowing
    four spellings of one key means four distinct valid signatures over what a
    reader would call the same claim, and "the same identity has several
    equally valid wire forms" is how a downstream comparison gets written
    against the wrong one.

    The test is exact and cheap: re-encode the decoded bytes and require the
    result to equal what was sent, byte for byte.
    """
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
    if base64.b64encode(raw).decode("ascii") != pubkey_b64:
        raise AgentRegistrationError(
            400,
            "invalid_pubkey",
            "pubkey must be the CANONICAL standard-base64 spelling of the 32 raw bytes "
            "(padded, zero trailing pad bits); several spellings decode to one key and "
            "only the canonical one is accepted",
        )
    return raw


def pubkey_fingerprint(pubkey_b64: str) -> str:
    """sha256 hex of the RAW key bytes — the identity's uniqueness basis.

    Derived from the decoded bytes rather than from the text so it is invariant
    under any spelling that ever slips past :func:`decode_pubkey_raw`.
    """
    return hashlib.sha256(decode_pubkey_raw(pubkey_b64)).hexdigest()


def _decode_pubkey(pubkey_b64: str) -> Ed25519PublicKey:
    """Decode a base64 raw Ed25519 public key into a verifier, or raise a 400."""
    raw = decode_pubkey_raw(pubkey_b64)
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise AgentRegistrationError(
            400,
            "invalid_pubkey",
            "pubkey is not a valid Ed25519 public key",
        ) from exc


def _validate_nonce(nonce: str) -> None:
    """Reject any nonce that is not canonical lowercase hex of 16..64 bytes.

    Hex-and-length is enforced rather than merely "some string" so a client
    cannot pass a constant like ``"nonce"`` and believe it is protected; the
    replay wall only means something if the value is actually unpredictable.

    The shape is checked by an ANCHORED regex BEFORE any decode, because
    ``bytes.fromhex`` is not a validator: it accepts uppercase, tolerates ASCII
    whitespace inside the string, and has no upper bound. Each of those is a
    second spelling of one nonce — the replay wall hashes the string it was
    given, so ``"AB…"`` and ``"ab…"`` burn two different rows for one value.
    The upper bound is separate housekeeping: the nonce table is written by
    unauthenticated callers, so an unbounded field is unbounded storage.
    """
    if not NONCE_RE.fullmatch(nonce):
        raise AgentRegistrationError(
            400,
            "invalid_nonce",
            f"nonce must be lowercase hex, {MIN_NONCE_BYTES}-{MAX_NONCE_BYTES} bytes "
            f"({MIN_NONCE_BYTES * 2}-{MAX_NONCE_BYTES * 2} chars, even length, no whitespace)",
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

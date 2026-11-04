"""ah_1104 — ``GET /api/agents/enroll.sh``: one-command keyless agent enrolment.

#445 made the no-key 401 point a cold agent at ``POST /api/agents/register``
(Ed25519 proof-of-key, no human). That breadcrumb leads to a protocol, not a
tool: the caller still has to generate a key, extract the 32 RAW public-key
bytes (not PEM/DER), spell them in canonical base64, build the versioned
canonical string, sign it and assemble the JSON. Hand-rolled crypto plus a
versioned string is exactly where a cold agent stalls, and no client shipped
for it. Authenticated non-fleet accounts were still 0 when this landed.

This module serves ONE static shell script (bash + curl + OpenSSL 3, nothing
else) that does the whole enrolment::

    curl -fsSL https://app.loopskill.io/api/agents/enroll.sh | bash -s -- <agent-name>

Design, in the order a reviewer will ask:

* **The canonical string is not restated.** The script's prefix and version are
  substituted from ``app.services.agent_registration_proof`` at render time, so
  a v2 bump changes the served script in the same deploy. The tests pin the
  rendered script to that module and register the script's REAL OpenSSL output
  through the REAL ``POST /api/agents/register`` route.
* **Nothing is interpolated that could break the JSON.** ``agent_name`` and
  ``contact`` are validated against a conservative charset before use; the
  other fields are base64/hex/ISO strings the script generated itself.
* **The secret is never printed.** The server shows the plaintext key once. The
  script writes it to ``$LOOPSKILL_CONFIG_DIR/api_key`` (default
  ``~/.config/loopskill``, umask 077) next to the private key, and prints the
  path. Piped-to-bash output often lands in CI logs and agent transcripts.
* **Re-running is safe.** An existing ``api_key`` file refuses (exit 5) instead
  of minting a second identity; an existing private key is reused, so a run
  that died on the network does not burn a fresh identity per retry.
* **Dry run.** ``LOOPSKILL_ENROLL_DRY_RUN=1`` prints the signed request body and
  makes no network call — how the tests exercise the real signing path, and how
  a cautious operator can read what would be sent.

Exit codes: 0 enrolled (or dry run) · 2 usage/invalid name · 3 missing
dependency or an OpenSSL without Ed25519 · 4 the server refused (status and body
on stderr) · 5 already enrolled in this config dir.

Public (no auth), same reasoning as ``/api/bundles/install.sh``: a caller must
be able to fetch the enroller BEFORE it has any key. Read-only, static asset.
``include_in_schema=False`` like its sibling, so the OpenAPI parity hash does
not move.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.services.agent_registration_proof import CANONICAL_PREFIX, CANONICAL_VERSION

router = APIRouter(tags=["agents"])

ENROLL_SCRIPT_PATH = "/api/agents/enroll.sh"

# Placeholders are @@TOKENS@@ rather than str.format fields: the body is bash,
# and doubling every ${VAR} brace would make the script unreadable to the very
# humans and agents who are told to `curl` it and read it before piping.
_ENROLL_SCRIPT_TEMPLATE = r"""#!/usr/bin/env bash
# LoopSkill agent enrolment: one command, no human, no pre-existing key.
#
# Usage:
#   curl -fsSL @@API_BASE@@/api/agents/enroll.sh | bash -s -- <agent-name> [contact]
#
# Generates an Ed25519 key, signs the canonical registration string
#   @@CANONICAL_PREFIX@@:@@CANONICAL_VERSION@@:{pubkey}:{timestamp}:{nonce}:{agent_name}
# and POSTs it to @@API_BASE@@/api/agents/register. The server returns a
# free-tier API key ONCE; it is saved to $LOOPSKILL_CONFIG_DIR/api_key
# (default ~/.config/loopskill, mode 600) and never printed.
#
# Requires: bash, curl, OpenSSL 3 (Ed25519). On macOS the system LibreSSL lacks
# Ed25519 signing: `brew install openssl@3` and set
# OPENSSL=/opt/homebrew/opt/openssl@3/bin/openssl.
#
# Env: LOOPSKILL_API_BASE (default @@API_BASE@@), LOOPSKILL_CONFIG_DIR,
#      OPENSSL (binary), LOOPSKILL_ENROLL_DRY_RUN=1 (print the signed body, no POST).
# Exit: 0 ok | 2 usage | 3 missing dependency | 4 server refused | 5 already enrolled
set -euo pipefail

API_BASE="${LOOPSKILL_API_BASE:-@@API_BASE@@}"
API_BASE="${API_BASE%/}"
CONF="${LOOPSKILL_CONFIG_DIR:-$HOME/.config/loopskill}"
OPENSSL="${OPENSSL:-openssl}"
NAME="${1:-}"
CONTACT="${2:-}"

err() { printf 'enroll: %s\n' "$*" >&2; }

if [ -z "$NAME" ]; then
  err "usage: bash -s -- <agent-name> [contact]"
  exit 2
fi
# Conservative charsets: both values are placed inside JSON without an encoder.
if ! [[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  err "agent-name must be 1-64 chars of A-Z a-z 0-9 . _ - (starting alphanumeric)"
  exit 2
fi
if [ -n "$CONTACT" ] && ! [[ "$CONTACT" =~ ^[A-Za-z0-9@._+:/-]{1,128}$ ]]; then
  err "contact must be 1-128 chars of A-Z a-z 0-9 @ . _ + : / -"
  exit 2
fi

for dep in "$OPENSSL" base64 tail tr date sed chmod mktemp; do
  command -v "$dep" >/dev/null 2>&1 || { err "missing dependency: $dep"; exit 3; }
done
if [ "${LOOPSKILL_ENROLL_DRY_RUN:-0}" != "1" ]; then
  command -v curl >/dev/null 2>&1 || { err "missing dependency: curl"; exit 3; }
fi

KEY="$CONF/agent-ed25519.pem"
KEYFILE="$CONF/api_key"
if [ -s "$KEYFILE" ] && [ "${LOOPSKILL_ENROLL_DRY_RUN:-0}" != "1" ]; then
  err "already enrolled: an API key exists at $KEYFILE (use it, or point LOOPSKILL_CONFIG_DIR elsewhere)"
  exit 5
fi

umask 077
mkdir -p "$CONF"
# -L first and on its own: [ -e ] is false for a DANGLING symlink, which
# openssl would then follow when writing the new private key.
if [ -L "$KEY" ] || { [ -e "$KEY" ] && [ ! -f "$KEY" ]; }; then
  err "$KEY is a symlink or not a regular file; refusing to use it"
  exit 3
fi
if [ -s "$KEY" ]; then
  # Reused key (a retry after a network failure): it may predate this script's
  # umask, so tighten it before trusting it with an identity.
  chmod 600 "$KEY" || { err "cannot restrict $KEY to mode 600"; exit 3; }
else
  if ! "$OPENSSL" genpkey -algorithm ed25519 -out "$KEY" 2>/dev/null; then
    rm -f "$KEY"
    err "$OPENSSL cannot generate Ed25519 keys (need OpenSSL 3; see the header of this script)"
    exit 3
  fi
fi

# The SPKI DER of an Ed25519 key is a fixed 12-byte header + the 32 RAW bytes.
PUBKEY="$("$OPENSSL" pkey -in "$KEY" -pubout -outform DER | tail -c 32 | base64 | tr -d '\n')"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
NONCE="$("$OPENSSL" rand -hex 16)"
MESSAGE="@@CANONICAL_PREFIX@@:@@CANONICAL_VERSION@@:${PUBKEY}:${TIMESTAMP}:${NONCE}:${NAME}"

MSGFILE="$(mktemp "$CONF/.enroll-msg.XXXXXX")"
trap 'rm -f "$MSGFILE"' EXIT
printf '%s' "$MESSAGE" >"$MSGFILE"
if ! SIGNATURE="$("$OPENSSL" pkeyutl -sign -inkey "$KEY" -rawin -in "$MSGFILE" | base64 | tr -d '\n')" \
   || [ -z "$SIGNATURE" ]; then
  err "$OPENSSL could not sign with Ed25519 (need OpenSSL 3 pkeyutl -rawin)"
  exit 3
fi

CONTACT_FIELD=""
[ -n "$CONTACT" ] && CONTACT_FIELD=",\"contact\":\"$CONTACT\""
BODY="{\"pubkey\":\"$PUBKEY\",\"timestamp\":\"$TIMESTAMP\",\"nonce\":\"$NONCE\",\"agent_name\":\"$NAME\",\"signature\":\"$SIGNATURE\"$CONTACT_FIELD}"

if [ "${LOOPSKILL_ENROLL_DRY_RUN:-0}" = "1" ]; then
  printf '%s\n' "$BODY"
  exit 0
fi

RESP="$(curl -sS -m 30 -w '\n%{http_code}' -H 'content-type: application/json' \
  --data "$BODY" "$API_BASE/api/agents/register")" || { err "request to $API_BASE failed"; exit 4; }
CODE="${RESP##*$'\n'}"
RBODY="${RESP%$'\n'*}"

if [ "$CODE" != "200" ] && [ "$CODE" != "201" ]; then
  err "register refused: HTTP $CODE"
  printf '%s\n' "$RBODY" >&2
  if [ "$CODE" = "409" ]; then
    err "this key is already enrolled and the server never re-issues its secret; see 'rotation' in $API_BASE/.well-known/agent.json"
  fi
  exit 4
fi

# Persist the raw response FIRST: it holds the shown-once key, and nothing
# after this line may be allowed to lose it.
printf '%s\n' "$RBODY" >"$CONF/enrollment.json"
API_KEY="$(printf '%s' "$RBODY" | sed -n 's/.*"api_key"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')" || API_KEY=""
if [ -z "$API_KEY" ]; then
  err "HTTP $CODE but no api_key could be parsed; the full response is saved to $CONF/enrollment.json"
  exit 4
fi
printf '%s\n' "$API_KEY" >"$KEYFILE"

echo "enrolled '$NAME' on $API_BASE (free tier)."
echo "API key saved to $KEYFILE (mode 600). The server shows it only once: back it up."
echo "use it:  export LOOPSKILL_API_KEY=\"\$(cat $KEYFILE)\"   # header: x-api-key"
"""


def render_enroll_script(api_base: str) -> str:
    """Render the enroller with the caller's origin and the canonical string baked in.

    Pure, so tests can run the exact bytes a caller would receive.
    """
    return (
        _ENROLL_SCRIPT_TEMPLATE.replace("@@API_BASE@@", api_base.rstrip("/"))
        .replace("@@CANONICAL_PREFIX@@", CANONICAL_PREFIX)
        .replace("@@CANONICAL_VERSION@@", CANONICAL_VERSION)
    )


@router.get(ENROLL_SCRIPT_PATH, include_in_schema=False)
def agent_enroll_script() -> PlainTextResponse:
    """Serve the keyless enrolment script. No auth (see module docstring)."""
    from app import config

    return PlainTextResponse(
        content=render_enroll_script(config.public_origin()),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )

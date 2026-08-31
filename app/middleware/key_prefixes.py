"""User-API-key prefix vocabulary (qa0208-w3 dual-accept).

Canonical prefix is ``lsk_`` (loopskill); ``rec_`` is the legacy prefix,
accepted indefinitely as a fallback. Minting still issues ``rec_live_`` keys
(see app/api_key_routes.py KEY_PREFIX) — switching the mint default is a
separate follow-up (non-trivial: display truncation + prefix regexes in tests
assume ``rec_live_``). The READ/validate path is widened here so any future
``lsk_``-minted key (or a manually reissued one) works identically to a
``rec_`` key today.

Any code path that checks "does this look like a user key" should test against
``USER_KEY_PREFIXES``, not the single ``API_KEY_PREFIX`` constant.
"""

API_KEY_PREFIX = "rec_"  # legacy, accepted as fallback
LOOPSKILL_KEY_PREFIX = "lsk_"  # canonical
USER_KEY_PREFIXES: tuple[str, ...] = (API_KEY_PREFIX, LOOPSKILL_KEY_PREFIX)
API_KEY_LENGTH = 36  # rec_ (4) + 32 hex chars
FLEET_KEY_PREFIX = "rec_fleet_"  # Phase E: fleet API keys (distinct from rec_live_, cbt_)

# agentreg_0819: keys minted by POST /api/agents/register for a self-registered
# autonomous agent. It is a NARROWING of the rec_ user-key namespace, not a new
# one — it already satisfies ``USER_KEY_PREFIXES``, so every existing validate
# path (app/middleware/api_key.py, app/mcp/auth.py) accepts it unchanged and
# resolves it to the same ``AuthContext(scope="user")`` as a rec_live_ key.
#
# The prefix exists so the two paths can cheaply recognise an agent key and add
# the ONE extra gate agent keys need: the identity-revocation check in
# ``app.middleware._agent_identity``. Distinct from rec_live_ and rec_fleet_.
AGENT_KEY_PREFIX = "rec_agent_"

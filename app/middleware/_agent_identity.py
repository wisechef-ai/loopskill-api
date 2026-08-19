"""agentreg_0819 — the revocation gate for self-registered agent keys.

``rec_agent_`` keys are ordinary ``api_keys`` rows: they already validate on
both the REST path (``app.middleware.api_key.APIKeyMiddleware.dispatch``) and
the MCP path (``app.mcp.auth.validate_key``) without a single change, because
``rec_agent_`` starts with ``rec_`` and therefore matches ``USER_KEY_PREFIXES``.

What they need on top is one gate: an agent identity can be REVOKED by an admin,
and a revoked identity's keys must stop working everywhere. This module is that
gate, in ONE place, so REST and MCP cannot drift (the repo's standing rule —
"MCP is the universal path; a REST-only fix is not a fix").

It lives in its own module rather than inline in ``api_key.py`` for the same
reason ``_public_paths`` / ``_org_scope`` / ``_jwt_cookie_auth`` do: that module
sits at 550 of the NEVER-waived 600-line god-object cap
(``tests/test_w0_2_pyfile_size_discipline.py``).

FAIL CLOSED, three ways:
  * identity row missing  → reject (a rec_agent_-prefixed key with no identity
    behind it is not a thing we ever mint)
  * identity revoked      → reject
  * lookup RAISES         → reject (an unavailable DB must not upgrade a
    revoked agent back to working; contrast the opportunistic public-route
    helper ``_auth_ctx_from_api_key``, which degrades to anonymous because
    there the safe answer is "less access", and here it is "no access")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.middleware.key_prefixes import AGENT_KEY_PREFIX

if TYPE_CHECKING:  # pragma: no cover — annotation-only, avoids an import cycle
    from uuid import UUID

    from sqlalchemy.orm import Session

logger = logging.getLogger("loopskill.middleware")


def is_agent_key(key: str | None) -> bool:
    """True when ``key`` is a self-registered agent key (``rec_agent_*``)."""
    return bool(key) and key.startswith(AGENT_KEY_PREFIX)


def agent_key_is_blocked(db: "Session", user_id: "UUID | None") -> bool:
    """True when a ``rec_agent_`` key's owning identity may NOT be used.

    ``user_id`` is the SHADOW user on the ``api_keys`` row; ``agent_identities``
    carries a UNIQUE ``user_id``, so this is a single indexed lookup and it runs
    ONLY for ``rec_agent_``-prefixed keys — no cost is added to any human key.

    Returns True (blocked) for a missing identity, a revoked identity, a NULL
    ``user_id``, or any lookup failure.
    """
    if user_id is None:
        return True
    from app.models import AgentIdentity

    try:
        identity = db.query(AgentIdentity).filter(AgentIdentity.user_id == user_id).first()
    # Rationale: revocation must fail CLOSED. If we cannot prove the identity is
    # live, the key does not work — an unreachable DB may not resurrect a
    # revoked agent.
    except Exception:  # noqa: BLE001
        logger.exception("agent identity revocation lookup failed; failing closed")
        return True
    if identity is None:
        return True
    return bool(identity.revoked)


def user_is_agent(db: "Session", user_id: "UUID | None") -> bool:
    """True when ``user_id`` is a SHADOW agent user (``users.is_agent``).

    agentreg_0819 review round 2, F5(a). This is what stamps
    ``AuthContext.is_agent``, and it reads the durable column rather than the
    ``rec_agent_`` key prefix: the prefix describes a credential, the column
    describes the actor. A principal must not be able to stop being an agent by
    acquiring a differently-shaped key.

    Fails CLOSED, and here "closed" means TRUE. ``is_agent`` only ever REMOVES
    capability (see ``authz.can_run_sandbox`` and the checkout gate), so an
    unresolvable lookup answering "assume agent" costs a human at worst one
    denied privileged action, whereas answering "assume human" would hand an
    agent the privilege the flag exists to withhold. Note the asymmetry with
    ``agent_key_is_blocked`` above, which fails closed to True for the opposite
    reason — both choose the answer that grants less.

    Callers on the hot path already hold the ``User`` row and should read
    ``user_obj.is_agent`` directly; this exists for the paths that do not
    (``app/mcp/auth.py``).
    """
    if user_id is None:
        return False
    from app.models import User

    try:
        row = db.query(User.is_agent).filter(User.id == user_id).first()
    # Rationale: see the fail-closed note above — an unresolvable principal is
    # treated as an agent, i.e. as the LESS privileged of the two.
    except Exception:  # noqa: BLE001
        logger.exception("agent-principal lookup failed; treating caller as an agent")
        return True
    if row is None:
        return False
    return bool(row[0])

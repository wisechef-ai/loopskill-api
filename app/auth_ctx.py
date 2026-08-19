"""Authorization context — frozen dataclass describing the authenticated caller.

Populated by APIKeyMiddleware (and anonymous branch) and attached to
request.state.auth_ctx. Downstream handlers (REST routes, MCP tools, sandbox)
consume this single object rather than re-implementing auth logic.

Phase A: scaffold + middleware wiring.
Phase B: MCP tools adopt ctx parameter.
Phase C: sandbox uses ctx.is_sandbox_operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

Scope = Literal[
    "anonymous", "user", "operator", "master", "cbt_token", "bdl_token", "fleet"
]  # bdl_token added Phase 3+4


@dataclass(frozen=True)
class AuthContext:
    """Immutable authentication context for a single request.

    Attributes:
        scope: Authorization level of the caller.
        user_id: UUID of the authenticated user (None for master/anonymous).
        api_key_id: UUID of the APIKey row (None for master/anonymous).
        bundle_scope: If set, this key is restricted to one specific bundle.
        fleet_id: If scope='fleet', the fleet UUID this key belongs to.
        tier: User subscription tier (e.g. "free", "pro", "pro_plus").
        is_sandbox_operator: True if the key has sandbox execution privileges.
        org_id: Tenant scope UUID (activate_0701 Phase TEN). None = personal scope.
        is_org_owner: True if this user is the org payer (can create client fleets).
        is_agent: True if the principal is a SELF-REGISTERED AGENT, not a human.
    """

    scope: Scope
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    bundle_scope: UUID | None = None  # cookbook-scoped key restriction  # compat-alias
    fleet_id: UUID | None = None  # fleet-scoped key restriction (Phase E)
    tier: str | None = None
    is_sandbox_operator: bool = False
    # repohygiene_2605/H.1 (Issue #290): cbt_token callers with this flag set may
    # call GET /api/skills/install for public-catalog skills they are entitled to
    # (i.e. skill.tier <= bundle-owner's tier).  Default False → opt-in only;
    # set to True by middleware when CookbookShareToken.allow_public_catalog is True.
    allow_public_catalog: bool = False
    # activate_0701/TEN: tenant scope (org boundary). None = personal scope.
    org_id: UUID | None = None
    is_org_owner: bool = False
    # agentreg_0819 (review round 2, F5): the principal is a SELF-REGISTERED
    # AGENT — a shadow User minted by POST /api/agents/register with no human
    # behind it — rather than a person who completed an OAuth login.
    #
    # An agent resolves to scope="user" ON PURPOSE (that is what keeps the
    # publisher/feedback/bundle surfaces reachable, which is the whole point of
    # the feature), so scope cannot carry this distinction. Round 1 had no
    # distinction at all: past the middleware, an agent principal and a free
    # human were the same object, and any predicate that wants to treat them
    # differently had nothing to read.
    #
    # Sourced from the DURABLE ``users.is_agent`` column, not from the
    # ``rec_agent_`` key prefix, so it survives a future second credential type
    # and cannot be shed by re-minting. Defaults False: every existing caller,
    # every human key and every anonymous context keeps its exact behaviour.
    is_agent: bool = False

    @classmethod
    def anonymous(cls) -> AuthContext:
        """Return an anonymous (unauthenticated) context."""
        return cls(scope="anonymous")

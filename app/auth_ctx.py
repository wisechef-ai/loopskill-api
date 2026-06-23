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
        cookbook_scope: If set, this key is restricted to one specific cookbook.
        fleet_id: If scope='fleet', the fleet UUID this key belongs to.
        tier: User subscription tier (e.g. "free", "pro", "pro_plus").
        is_sandbox_operator: True if the key has sandbox execution privileges.
    """

    scope: Scope
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    cookbook_scope: UUID | None = None  # cookbook-scoped key restriction
    fleet_id: UUID | None = None  # fleet-scoped key restriction (Phase E)
    tier: str | None = None
    is_sandbox_operator: bool = False
    # repohygiene_2605/H.1 (Issue #290): cbt_token callers with this flag set may
    # call GET /api/skills/install for public-catalog skills they are entitled to
    # (i.e. skill.tier <= cookbook-owner's tier).  Default False → opt-in only;
    # set to True by middleware when CookbookShareToken.allow_public_catalog is True.
    allow_public_catalog: bool = False

    @classmethod
    def anonymous(cls) -> AuthContext:
        """Return an anonymous (unauthenticated) context."""
        return cls(scope="anonymous")

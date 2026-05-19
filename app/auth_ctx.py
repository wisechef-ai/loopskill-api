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
from typing import Literal, Optional
from uuid import UUID

Scope = Literal["anonymous", "user", "operator", "master", "cbt_token"]


@dataclass(frozen=True)
class AuthContext:
    """Immutable authentication context for a single request.

    Attributes:
        scope: Authorization level of the caller.
        user_id: UUID of the authenticated user (None for master/anonymous).
        api_key_id: UUID of the APIKey row (None for master/anonymous).
        cookbook_scope: If set, this key is restricted to one specific cookbook.
        tier: User subscription tier (e.g. "free", "pro", "pro_plus").
        is_sandbox_operator: True if the key has sandbox execution privileges.
    """

    scope: Scope
    user_id: Optional[UUID] = None
    api_key_id: Optional[UUID] = None
    cookbook_scope: Optional[UUID] = None  # cookbook-scoped key restriction
    tier: Optional[str] = None
    is_sandbox_operator: bool = False

    @classmethod
    def anonymous(cls) -> "AuthContext":
        """Return an anonymous (unauthenticated) context."""
        return cls(scope="anonymous")

"""Authorization predicates — pure functions for centralized access control.

All predicates are pure functions: they take an AuthContext and a model object,
and return True/False without side effects. This makes them trivially testable
and composable across REST routes, MCP tools, and sandbox handlers.

100% line coverage required (see test_secfix_1905_a_authz.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.auth_ctx import AuthContext


def can_read_skill(ctx: AuthContext, skill: Any) -> bool:
    """Return True if ctx may read/view the given skill.

    Access rules:
    - Public skills: always readable
    - Master scope: always readable
    - User scope: readable if the user owns the skill
    - All other cases: False
    """
    if skill.is_public:
        return True
    if ctx.scope == "master":
        return True
    skill_owner = getattr(skill, "skill_owner", None)
    if ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == skill_owner:
        return True
    return False


def can_install(ctx: AuthContext, skill: Any) -> bool:
    """Return True if ctx may install the given skill.

    Same rules as can_read_skill — install requires at least read access.
    """
    return can_read_skill(ctx, skill)


def can_write_cookbook(ctx: AuthContext, cookbook: Any) -> bool:
    """Return True if ctx may write (modify) the given cookbook.

    Access rules:
    - Master scope: always allowed
    - User scope: allowed if ctx.user_id == cookbook.cookbook_owner
    - Cookbook-scoped key: additionally restricted to the specific cookbook
      (ctx.cookbook_scope must match cookbook.id)
    - All other cases: False
    """
    # If this is a cookbook-scoped key restricted to a different cookbook → deny
    if ctx.cookbook_scope is not None and ctx.cookbook_scope != cookbook.id:
        return False

    if ctx.scope == "master":
        return True
    if ctx.scope == "user" and ctx.user_id is not None and ctx.user_id == cookbook.cookbook_owner:
        return True
    return False


def can_call_admin_mcp_tool(ctx: AuthContext) -> bool:
    """Return True if ctx may call an admin-level MCP tool.

    Only master-scope callers may use admin tools.
    """
    return ctx.scope == "master"


def can_run_sandbox(ctx: AuthContext) -> bool:
    """Return True if ctx may execute sandbox runs.

    Access rules:
    - Master scope: always allowed
    - is_sandbox_operator flag: allowed regardless of scope
    - All other cases: False
    """
    if ctx.scope == "master":
        return True
    if ctx.is_sandbox_operator:
        return True
    return False
